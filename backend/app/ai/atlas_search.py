"""Atlas Search and Atlas Vector Search: the index definitions and the queries (P5.7; plan section 8.6,
docs/search/engine-decision.md).

Search has two stages that fetch candidates, and each can run on one of two engines (app/core/config.py,
EnginePair; NEARSHOP_SEARCH_KEYWORD_ENGINE, NEARSHOP_SEARCH_VECTOR_ENGINE, NEARSHOP_SEARCH_SHADOW_ENGINES):

  keyword  text: the products text index (database/schema.py, text_search), as before P5.7.
           atlas_search: the PRODUCTS_INDEX Atlas Search index, with typo tolerance, synonyms and language
           analyzers.
  vector   in_process: the NumPy matrix in each API process (app/ai/semantic_index.py), as before P5.7.
           atlas_vector: the VECTOR_INDEX Atlas Vector Search index on listing_vectors.

Both Atlas queries are restricted to the shops within the customer's radius, and to active listings. Fusion of the
two stages, grouping, filters and the ranking config stay NearShop's own (app/services/search.py) whichever
engines run. P5.2's exact local-term match (local_term_scores) also stays NearShop's own, so it gives the same
listings and scores with every engine; local_terms is mapped as a token field so a later step can move it into
the Atlas query.

Three indexes in all, so staging on M0 (at most 3 search and vector indexes) can hold the same set as
production on Flex (at most 10):

  PRODUCTS_INDEX  products: name, brand, keywords and description with today's weights (10, 6, 4, 1) as score
                  boosts, each analysed in English, with a Hindi analyzer for Devanagari and a custom analyzer
                  for Kannada and Telugu (standard tokenizer and icuNormalizer; never icuFolding, which may strip
                  vowel signs); local_terms as whole tokens; shop_id and is_active as filters; one synonym
                  mapping, read from the search_synonyms collection (app/services/synonyms.py).
  CATALOG_INDEX   catalog_items: autocomplete on name and brand, for suggestions while typing.
  VECTOR_INDEX    listing_vectors: the active embedding model's dimensions, cosine similarity, and shop_id,
                  is_active and model as filters. Measured without quantization and with scalar quantization.

The indexes are created, listed and dropped only through the operator CLI (database/admin_tasks.py,
search-index plan, create, status and drop). Development and CI run a MongoDB without a search process: the
query paths are tested against a fake runner (tests/test_search_engines.py), and against MongoDB's Atlas local
deployment when NEARSHOP_TEST_ATLAS_URI names one.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pymongo.collection import Collection as MongoCollection

from app.core.config import settings
from app.core.database import Database

PRODUCTS_INDEX = "nearshop_products_search"
CATALOG_INDEX = "nearshop_catalog_autocomplete"
VECTOR_INDEX = "nearshop_listing_vectors"
SYNONYM_MAPPING = "nearshop_synonyms"
SYNONYM_COLLECTION = "search_synonyms"
VECTOR_COLLECTION = "listing_vectors"
INDIC_ANALYZER = "nearshop_indic"

# Atlas limits (MongoDB docs, 18 Oct 2026; plan section 11).
TIER_INDEX_LIMITS = {"M0": 3, "Flex": 10}  # search and vector indexes together, per cluster
PLAN_MAX_INDEXES = 3  # NearShop's own limit, so staging (M0) can mirror production
MAX_DEFINITION_BYTES = 3 * 1024
MAX_SYNONYM_MAPPINGS = 1  # per index definition
MAX_SYNONYM_DOCUMENTS = 10_000  # per synonym source collection

# The text index's weights (database/schema.py, text_search), as score boosts.
FIELD_WEIGHTS: dict[str, int] = {"name": 10, "brand": 6, "keywords": 4, "description": 1}
FUZZY_FIELDS = ("name", "brand", "keywords")  # description is long free text: typos there match too loosely
# Typo tolerance: at most 1 edit for words of 4 to 7 characters, 2 for longer words, none for shorter ones.
FUZZY_ONE_EDIT = range(4, 8)
FUZZY_TWO_EDITS_FROM = 8
KEYWORD_LIMIT = 2000  # the text index returns every match; Atlas returns at most this many, best first
VECTOR_LIMIT = 1000  # listings returned by the vector query, most similar first
VECTOR_NUM_CANDIDATES = 10_000  # Atlas's maximum: the approximate search looks at this many before the limit
AUTOCOMPLETE_FUZZY_FROM = 4

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
KANNADA_TELUGU = re.compile(r"[ఀ-౿ಀ-೿]")

Quantization = Literal["none", "scalar"]
QUANTIZATIONS: tuple[str, ...] = ("none", "scalar")


# ---------------------------------------------------------------- index definitions


def _text_field() -> dict:
    return {"type": "string", "analyzer": "lucene.english", "searchAnalyzer": "lucene.english",
            "multi": {"hi": {"type": "string", "analyzer": "lucene.hindi"},
                      "indic": {"type": "string", "analyzer": INDIC_ANALYZER}}}


def products_definition() -> dict:
    return {
        "analyzer": "lucene.english",
        "searchAnalyzer": "lucene.english",
        "mappings": {"dynamic": False, "fields": {
            **{field: _text_field() for field in FIELD_WEIGHTS},
            "local_terms": {"type": "token"},
            "shop_id": {"type": "number"},
            "is_active": {"type": "boolean"},
        }},
        # Kannada and Telugu: Atlas has no analyzer for either. icuNormalizer composes characters (NFC) without
        # removing anything; icuFolding is never used, as it may strip vowel signs.
        "analyzers": [{"name": INDIC_ANALYZER, "tokenizer": {"type": "standard"},
                       "tokenFilters": [{"type": "icuNormalizer", "normalizationForm": "nfc"}]}],
        "synonyms": [{"name": SYNONYM_MAPPING, "analyzer": "lucene.english",
                      "source": {"collection": SYNONYM_COLLECTION}}],
    }


def catalog_definition() -> dict:
    autocomplete = {"type": "autocomplete", "tokenization": "edgeGram", "minGrams": 2, "maxGrams": 15,
                    "foldDiacritics": False}
    return {"mappings": {"dynamic": False, "fields": {"name": autocomplete, "brand": autocomplete}}}


def vector_dimensions(model_name: str | None = None) -> int:
    from app.core.config import embedding_spec

    return embedding_spec(model_name or settings.embedding_model).dim


def vector_definition(dimensions: int | None = None, quantization: str = "none") -> dict:
    if quantization not in QUANTIZATIONS:
        raise ValueError(f"quantization is one of {', '.join(QUANTIZATIONS)}; got {quantization!r}")
    return {"fields": [
        {"type": "vector", "path": "vector", "numDimensions": dimensions or vector_dimensions(),
         "similarity": "cosine", "quantization": quantization},
        {"type": "filter", "path": "shop_id"},
        {"type": "filter", "path": "is_active"},
        {"type": "filter", "path": "model"},
    ]}


@dataclass(frozen=True)
class IndexSpec:
    name: str
    collection: str
    type: str  # search | vectorSearch
    definition: dict

    @property
    def size(self) -> int:
        """The definition's size in bytes, as compact JSON."""
        return definition_size(self.definition)

    @property
    def synonym_mappings(self) -> int:
        return len(self.definition.get("synonyms") or [])


def definition_size(definition: dict) -> int:
    return len(json.dumps(definition, separators=(",", ":"), ensure_ascii=False).encode())


def index_plan(dimensions: int | None = None, quantization: str = "none") -> list[IndexSpec]:
    """The three indexes, in the order create builds them."""
    return [
        IndexSpec(PRODUCTS_INDEX, "products", "search", products_definition()),
        IndexSpec(CATALOG_INDEX, "catalog_items", "search", catalog_definition()),
        IndexSpec(VECTOR_INDEX, VECTOR_COLLECTION, "vectorSearch", vector_definition(dimensions, quantization)),
    ]


def plan_problems(plan: list[IndexSpec]) -> list[str]:
    """What breaks the Atlas limits or NearShop's plan: more than 3 indexes, a definition over 3 KB, more than one
    synonym mapping, or an icuFolding filter in any analyzer."""
    out = []
    if len(plan) > PLAN_MAX_INDEXES:
        out.append(f"{len(plan)} indexes; the plan allows {PLAN_MAX_INDEXES}")
    for spec in plan:
        if spec.size > MAX_DEFINITION_BYTES:
            out.append(f"{spec.name}: definition is {spec.size} bytes, over {MAX_DEFINITION_BYTES}")
        if spec.synonym_mappings > MAX_SYNONYM_MAPPINGS:
            out.append(f"{spec.name}: {spec.synonym_mappings} synonym mappings; at most {MAX_SYNONYM_MAPPINGS}")
        for analyzer in spec.definition.get("analyzers") or []:
            if any(f.get("type") == "icuFolding" for f in analyzer.get("tokenFilters") or []):
                out.append(f"{spec.name}: analyzer {analyzer['name']} uses icuFolding")
    return out


# ---------------------------------------------------------------- queries

# How an aggregation reaches the database. Tests replace it with a fake that returns fixed hits.
Runner = Callable[[MongoCollection, list[dict]], list[dict]]


def _aggregate(collection: MongoCollection, pipeline: list[dict]) -> list[dict]:
    # Every hit in the first batch: a getMore would be one more database operation per search (Flex bills by
    # operations per second). The projected hits are an id and a score, about 30 bytes each.
    return list(collection.aggregate(pipeline, batchSize=max(KEYWORD_LIMIT, VECTOR_LIMIT)))


runner: Runner = _aggregate


def fuzzy_groups(words: list[str]) -> list[tuple[int, list[str]]]:
    """The words that get typo tolerance, by the most edits allowed: (1, words of 4 to 7 characters) and
    (2, words of 8 or more). Shorter words match exactly only."""
    one = [w for w in words if len(w) in FUZZY_ONE_EDIT]
    two = [w for w in words if len(w) >= FUZZY_TWO_EDITS_FROM]
    return [(edits, group) for edits, group in ((1, one), (2, two)) if group]


def keyword_pipeline(words: list[str], completions: list[str], shop_ids: list[int],
                     limit: int = KEYWORD_LIMIT) -> list[dict]:
    """One compound query. Exact terms with synonyms, and terms with typos, are separate clauses: Atlas Search
    does not allow fuzzy and synonyms in one text clause. Completions of the word being typed (from the
    catalogue vocabulary, as the text index stage does) match exactly, without synonyms or typos."""
    text = " ".join(dict.fromkeys([*words, *completions]))
    should: list[dict] = [
        {"text": {"query": text, "path": field, "synonyms": SYNONYM_MAPPING, "score": {"boost": {"value": weight}}}}
        for field, weight in FIELD_WEIGHTS.items()
    ]
    for edits, group in fuzzy_groups(words):
        should += [{"text": {"query": " ".join(group), "path": field, "fuzzy": {"maxEdits": edits},
                             "score": {"boost": {"value": FIELD_WEIGHTS[field]}}}} for field in FUZZY_FIELDS]
    for multi, pattern in (("hi", DEVANAGARI), ("indic", KANNADA_TELUGU)):
        if pattern.search(text):
            should += [{"text": {"query": text, "path": {"value": field, "multi": multi},
                                 "score": {"boost": {"value": weight}}}} for field, weight in FIELD_WEIGHTS.items()]
    return [
        {"$search": {"index": PRODUCTS_INDEX, "compound": {
            "filter": [{"in": {"path": "shop_id", "value": list(shop_ids)}},
                       {"equals": {"path": "is_active", "value": True}}],
            "should": should,
            "minimumShouldMatch": 1,
        }}},
        {"$limit": limit},
        {"$project": {"_id": 1, "score": {"$meta": "searchScore"}}},
    ]


def scale_keyword_scores(raw: dict[int, float], n_words: int) -> dict[int, float]:
    """Atlas scores (BM25 with boosts) on the text index's scale: the best listing gets the name weight times the
    number of query words, about what the text index gives a listing whose name holds every word. Search divides
    by the best score anyway; the scale matters only against the local-term bonus (w_local_term per term)."""
    best = max(raw.values(), default=0.0)
    if best <= 0:
        return {}
    top = FIELD_WEIGHTS["name"] * max(n_words, 1)
    return {pid: top * score / best for pid, score in raw.items() if score > 0}


def keyword_scores(db: Database, words: list[str], completions: list[str], shop_ids: list[int]) -> dict[int, float]:
    """Atlas Search scores of the active listings in shop_ids, on the text index's scale."""
    if not words or not shop_ids:
        return {}
    rows = runner(db.mongo["products"], keyword_pipeline(words, completions, shop_ids))
    return scale_keyword_scores({r["_id"]: float(r["score"]) for r in rows}, len(words))


def vector_pipeline(query_vector: list[float], shop_ids: list[int], model: str, limit: int = VECTOR_LIMIT) -> list[dict]:
    return [
        {"$vectorSearch": {
            "index": VECTOR_INDEX, "path": "vector", "queryVector": query_vector,
            "numCandidates": max(VECTOR_NUM_CANDIDATES, limit), "limit": limit,
            "filter": {"shop_id": {"$in": list(shop_ids)}, "is_active": True, "model": model},
        }},
        {"$project": {"_id": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]


def vector_scores(db: Database, text: str, shop_ids: list[int]) -> dict[int, float]:
    """Similarity of `text` to the active listings in shop_ids, on the scale search ranks on, like
    semantic_index.index.query. Atlas reports cosine similarity as (1 + cosine) / 2; it is mapped back."""
    from app.ai import semantic_index

    if not text or not shop_ids:
        return {}
    provider = semantic_index.get_provider()
    query = provider.embed([text], "query")[0]
    rows = runner(db.mongo[VECTOR_COLLECTION], vector_pipeline([float(x) for x in query], shop_ids, provider.name))
    if not rows:
        return {}
    import numpy as np

    cosine = np.array([2 * float(r["score"]) - 1 for r in rows], dtype=np.float32)
    scaled = semantic_index.to_search_scale(provider.name, cosine)
    return {int(r["_id"]): float(s) for r, s in zip(rows, scaled.tolist(), strict=True)}


def autocomplete_pipeline(norm: str, limit: int) -> list[dict]:
    def clause(path: str, boost: float) -> dict:
        op: dict[str, Any] = {"query": norm, "path": path, "score": {"boost": {"value": boost}}}
        if len(norm) >= AUTOCOMPLETE_FUZZY_FROM:
            op["fuzzy"] = {"maxEdits": 1}
        return {"autocomplete": op}

    return [
        {"$search": {"index": CATALOG_INDEX, "compound": {
            "should": [clause("name", 2.0), clause("brand", 1.0)], "minimumShouldMatch": 1}}},
        {"$limit": limit},
        {"$project": {"_id": 1, "name": 1, "brand": 1, "icon": 1, "category_id": 1}},
    ]


def autocomplete_items(db: Database, norm: str, limit: int) -> list[dict]:
    """Catalogue items whose name or brand starts a word with what was typed (one typo allowed from 4
    characters), best first: raw documents with _id, name, brand, icon and category_id."""
    if not norm:
        return []
    return runner(db.mongo["catalog_items"], autocomplete_pipeline(norm, limit))


# ---------------------------------------------------------------- fusion inside the database


def rank_fusion_problem(server_version: str) -> str | None:
    """Why the candidate that fuses both Atlas stages inside the database ($rankFusion) cannot run here, or None.
    $rankFusion needs MongoDB 8.1 or later, and its input pipelines all read the collection the aggregation runs
    on, while the keyword index is on products and the vector index on listing_vectors."""
    parts = [int(p) for p in re.findall(r"\d+", server_version)[:2]] + [0, 0]
    if tuple(parts[:2]) < (8, 1):
        return f"$rankFusion needs MongoDB 8.1 or later; this server runs {server_version}."
    return ("$rankFusion's input pipelines all read one collection, but the keyword index is on products and the "
            "vector index on listing_vectors; fusing inside the database would need the vectors on products.")
