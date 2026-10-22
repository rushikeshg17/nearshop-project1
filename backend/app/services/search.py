"""NearShop search pipeline.

    query -> normalise + synonyms -> keyword (MongoDB text index) + semantic (sentence embeddings)
          -> location filter (2dsphere index + Haversine) -> availability / price / fulfilment filters
          -> hybrid relevance + proximity ranking -> group listings of the same item -> results

The keyword and semantic stages each run on the engine settings choose (P5.7; app/ai/atlas_search.py): the text
index or Atlas Search, and the in-process vector index or Atlas Vector Search. The defaults are today's engines;
fusion, grouping, filters and the ranking config are NearShop's own with every engine.

Results are grouped by catalog item so a customer sees "Samsung 25W charger: 4 shops nearby
from Rs 1,249" and can compare shops, instead of four near-identical cards.

Cost at city scale (P8.2; docs/perf/baseline.md)
------------------------------------------------
A search reads the city in proportion to its radius, never more:

  - the radius is capped at MAX_RADIUS_KM (25 km); a wider one is searched at 25 km, and the response's
    radius_km says so;
  - the keyword query (the text index, and the local-term match) asks only about the listings of the shops in
    radius, and the in-process semantic index scores only the listings in radius. The Atlas stages (P5.7) already
    filter by those shops;
  - every listing in radius that passes the filters is read with RANK_FIELDS only: what relevance, grouping,
    the group scores, the counts and the map pins need. Ranking, grouping and counting run on those rows, so every
    group, its rank and the totals are exactly what they would be with whole documents;
  - only the listings a page shows (each group's best offer and up to OFFERS_SHOWN offers) are then read in full,
    with DISPLAY_FIELDS. A page holds at most MAX_PAGE_SIZE (50) groups, so one search reads at most
    MAX_FULL_READS (50 x 7 = 350) listings in full. A candidate listing is never built into a model or serialised
    unless it is shown.

Deviation from P8.2 item 5 ("cap candidates at 2,000 by relevance before grouping"): there is no cut of the
candidate rows before grouping. On the 50,000-listing load-test database 5 of the 50 load-test queries pass the
relevance cutoff with more than 2,000 listings ("sports shoes": 3,219 listings in 19 groups), and a cut there drops
whole groups from the top 20 or shrinks their shop counts and price ranges, which the prompt's own test (the top
20 equal the uncapped version) forbids. Every listing in radius is grouped from its RANK_FIELDS row instead, so the
grouping cost still grows with the listings in radius; the bound above is on the full reads. docs/perf/baseline.md
records this for sign-off.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import lru_cache

import numpy as np
from rapidfuzz import fuzz, process

from app.ai import atlas_search, semantic_index
from app.ai.semantic_index import OVERLAP, index, listing_sync, nightly_slot
from app.core import cache_version
from app.core.config import SEARCH_SIMILARITY_SCALE, EnginePair, RankingConfig, settings
from app.core.database import Database, utcnow
from app.models import Product, SearchEvent, Shop
from app.models.core import live_shop_filter
from app.schemas.serializers import STALE_DAYS, freshness, media_url, public_listing, shop_brief, thumbnail_url
from app.services.loaders import categories, category_by_slug, with_categories, with_category
from app.services.privacy import consent_granted
from app.utils.geo import haversine_km, within_km
from app.utils.text import expand_synonyms, is_word, normalize_query, search_tokens, word_parts

# Every ranking constant and group weight lives in a versioned RankingConfig (app/core/config.py, P5.5): the
# version NEARSHOP_SEARCH_RANKING_VERSION names, r1 by default (the hand-set values used before P5.5). An exact
# match on a listing's local_terms (Kannada, Telugu, Hindi or a transliteration; P5.2) adds w_local_term, the
# text index's brand weight, to its keyword score once per matched term: it counts as much as a brand match.
# W_LOCAL_TERM is the active config's value, read once at import, for code that checks it against the text
# index's brand weight (database/schema.py); ranking reads the config itself.
W_LOCAL_TERM = settings.ranking.w_local_term
# The similarity scale the semantic index returns (config.SEARCH_SIMILARITY_SCALE, the all-MiniLM-L6-v2
# calibration); r1's sem_floor and sem_ceil equal it, and a tuned config maps from this scale with its own.
SEM_FLOOR, SEM_CEIL = SEARCH_SIMILARITY_SCALE.sem_floor, SEARCH_SIMILARITY_SCALE.sem_ceil
LOCATION_DECIMALS = 2  # search_history locations: about 1.1 km (plan section 8.4)
# City scale (P8.2; module docstring). A wider radius is searched at MAX_RADIUS_KM.
MAX_RADIUS_KM = 25.0
# A page holds at most MAX_PAGE_SIZE groups (a larger page_size is served as MAX_PAGE_SIZE, and the response's
# page_size says so); each shows its best offer and up to OFFERS_SHOWN offers. So one search reads at most
# MAX_FULL_READS listings in full (DISPLAY_FIELDS), and paging is never cut short by that bound.
MAX_PAGE_SIZE = 50
OFFERS_SHOWN = 6
MAX_FULL_READS = MAX_PAGE_SIZE * (1 + OFFERS_SHOWN)
# Read for every listing in radius that passes the filters: relevance, grouping, group scores (stock, distance,
# freshness), prices, category counts, map pins and the group's photo.
RANK_FIELDS = {"shop_id": 1, "catalog_item_id": 1, "category_id": 1, "price": 1, "quantity": 1,
               "stock_updated_at": 1, "image_path": 1}
# Read, on top of RANK_FIELDS, only for the listings a page shows. Search results never show the description,
# specs, keywords or local terms, so those are never fetched.
DISPLAY_FIELDS = {"name": 1, "brand": 1, "unit": 1, "icon": 1, "mrp": 1, "low_stock_threshold": 1, "is_active": 1}
# What run_search reports as engine.keyword, per keyword engine (P5.7); the P5.6 shadow labels use the same names.
KEYWORD_ENGINE_LABELS = {"text": "mongodb-text-index", "atlas_search": "atlas-search"}
ATLAS_VECTOR_LABEL = "atlas-vector-search"

# Stock freshness (P7.4; bands and multipliers in app/schemas/serializers.py): a group's score is multiplied by
# the freshness multiplier of its freshest offer, and the in-stock-only filter leaves out listings whose stock was
# last confirmed more than STALE_DAYS ago. Freshness is judged at search_now(): the current time, unless the
# evaluation harness fixed the clock to its snapshot's seeding time (set_clock), so a reused snapshot's listings
# do not go stale as it ages.
_clock: datetime | None = None


def set_clock(at: datetime | None) -> None:
    """Fix the clock freshness is judged at (None: the current time again). For the evaluation harness only."""
    global _clock
    _clock = at


def search_now() -> datetime:
    return _clock or utcnow()


@dataclass
class SearchParams:
    q: str = ""
    lat: float = 0.0
    lng: float = 0.0
    radius_km: float = 5.0
    category: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    in_stock_only: bool = False
    fulfillment: str | None = None  # pickup | delivery
    sort: str = "relevance"  # relevance | distance | price_asc | price_desc
    shop_id: int | None = None
    page: int = 1
    page_size: int = 20


def nearby_shops(db: Database, lat: float, lng: float, radius_km: float, fulfillment: str | None = None,
                 shop_id: int | None = None) -> dict[int, tuple[Shop, float]]:
    # Only shops customers may see (P3.3): active, and verified with the approval in force.
    query: dict = {**live_shop_filter(), "location": within_km(lat, lng, radius_km)}
    if shop_id:
        query["_id"] = shop_id
    out = {}
    for s in with_categories(db, db.shops.find(query)):
        d = haversine_km(lat, lng, s.lat, s.lng)
        if d > radius_km:
            continue
        if fulfillment == "delivery" and not (s.offers_delivery and d <= s.delivery_radius_km):
            continue
        if fulfillment == "pickup" and not s.offers_pickup:
            continue
        out[s.id] = (s, d)
    return out


def _completions(db: Database, tokens: list[str]) -> list[str]:
    """Prefix-match the word being typed, so "chargi" still finds "charger": the text index matches whole (stemmed)
    words, so the partial word is completed from the catalogue vocabulary."""
    last = tokens[-1] if tokens else ""
    if len(last) < 3:
        return []
    return sorted(w for w in _vocabulary(db) if w.startswith(last) and w != last)[:8]


def keyword_scores(db: Database, terms: list[str], shop_ids: list[int] | None = None) -> dict[int, float]:
    """Text-index relevance over name/brand/keywords/description (weights 10/6/4/1). Higher = better. With
    shop_ids, only the listings of those shops (search: the shops in radius, P8.2); without, every listing."""
    tokens = search_tokens(terms)
    if not tokens or shop_ids == []:
        return {}
    tokens += _completions(db, tokens)
    query: dict = {"$text": {"$search": " ".join(dict.fromkeys(tokens))}}
    if shop_ids is not None:
        query["shop_id"] = {"$in": shop_ids}
    rows = db.products.find_raw(query, {"score": {"$meta": "textScore"}})
    return {r["_id"]: r["score"] for r in rows}


def atlas_keyword_scores(db: Database, norm: str, shop_ids: list[int]) -> dict[int, float]:
    """Atlas Search relevance (P5.7) over the same fields and weights, for the listings in shop_ids, on the text
    index's scale. Synonyms come from the index's synonym mapping (the search_synonyms source), not from the
    expanded terms; the word being typed is completed as for the text index."""
    words = search_tokens(norm.split())
    if not words:
        return {}
    return atlas_search.keyword_scores(db, words, _completions(db, words), shop_ids)


def _local_term_candidates(norm: str) -> list[str]:
    """What a query can match in local_terms, exactly: each word, each pair of adjacent words, and the
    whole query (local terms may be phrases, such as the Kannada for "bike oil"). A hyphenated query
    is also tried with spaces, as people join or split the two words of a name either way."""
    out: list[str] = []
    for text in dict.fromkeys([norm, norm.replace("-", " ")]):
        words = text.split()
        out += [*words, *(f"{a} {b}" for a, b in zip(words, words[1:], strict=False)), " ".join(words)]
    return list(dict.fromkeys(w for w in out if w))


def local_term_scores(db: Database, norm: str, w_local_term: float | None = None,
                      shop_ids: list[int] | None = None) -> dict[int, float]:
    """Listings whose local_terms contain a word, adjacent pair or the whole of the normalised query, scored
    w_local_term (the ranking config's, by default) per matched term. Listings created from the catalogue after
    migration 8 copied the terms are matched through their catalog item's own local_terms. With shop_ids, only
    the listings of those shops (P8.2); the catalogue match stays city-wide, as the catalogue is shared."""
    candidates = _local_term_candidates(norm)
    if not norm or not candidates or shop_ids == []:
        return {}
    weight = settings.ranking.w_local_term if w_local_term is None else w_local_term
    wanted = set(candidates)
    by_item = {c["_id"]: len(wanted.intersection(c["local_terms"]))
               for c in db.catalog_items.find_raw({"local_terms": {"$in": candidates}}, {"local_terms": 1})}
    clauses: list[dict] = [{"local_terms": {"$in": candidates}}]
    if by_item:
        clauses.append({"catalog_item_id": {"$in": list(by_item)}})
    query: dict = {"$or": clauses}
    if shop_ids is not None:
        query["shop_id"] = {"$in": shop_ids}
    out: dict[int, float] = {}
    for r in db.products.find_raw(query, {"local_terms": 1, "catalog_item_id": 1}):
        own = len(wanted.intersection(r.get("local_terms") or []))
        matched = max(own, by_item.get(r.get("catalog_item_id"), 0))
        if matched:
            out[r["_id"]] = weight * matched
    return out


def _keyword_stage(db: Database, norm: str, expanded: list[str], engine: str = "text",
                   shop_ids: list[int] | None = None) -> tuple[dict[int, float], dict[int, int]]:
    """Keyword scores from the chosen engine, and the number of local terms each listing matched (weighted later,
    by the ranking config). Every engine is asked only about the listings of shop_ids (P8.2)."""
    shop_ids = shop_ids or []
    if engine == "atlas_search":
        kw = atlas_keyword_scores(db, norm, shop_ids)
    else:
        kw = keyword_scores(db, norm.split() + expanded, shop_ids)
    return kw, {pid: round(bonus) for pid, bonus in local_term_scores(db, norm, 1.0, shop_ids).items()}


Scorer = Callable[[list[int]], dict[int, float]]


def nearby_similarities(db: Database, norm: str) -> Scorer:
    """The in-process index's query (semantic_index.SemanticIndex.query) split in two, so that only the listings in
    radius are scored (P8.2): this brings the index up to date and embeds the query, side by side with the listing
    fetch; the function it returns scores the given listing ids only, with the same similarities query gives them.
    Ids the index does not hold (inactive, or added since its last refresh) get no score, as with query."""
    index.ensure(db)
    provider = semantic_index.get_provider()
    if provider.name != index.snapshot.provider_name:  # the model setting changed since the build
        index.mark_dirty()
        index.ensure(db)
        provider = semantic_index.get_provider()
    snap = index.snapshot  # one consistent state, whatever another thread installs meanwhile
    if len(snap.product_ids) == 0:
        return lambda ids: {}
    q = provider.embed([norm], "query")[0]

    def score(ids: list[int]) -> dict[int, float]:
        if not ids:
            return {}
        wanted = np.unique(np.asarray(ids, dtype=np.int64))
        at = np.minimum(np.searchsorted(snap.product_ids, wanted), len(snap.product_ids) - 1)
        rows = at[snap.product_ids[at] == wanted]
        sims = semantic_index.to_search_scale(provider.name, snap.matrix[rows] @ q)
        return dict(zip(snap.product_ids[rows].tolist(), sims.tolist(), strict=True))

    return score


def _vector_stage(db: Database, norm: str, engine: str, shop_ids: list[int]) -> Scorer:
    """The vector stage, as a function of the listing ids to score. Atlas Vector Search is asked about the listings
    of shop_ids only, so its scores need no further narrowing."""
    if engine == "atlas_vector":  # listing_vectors first catches up with the listings, as the matrix would
        listing_sync.ensure_quietly(db)
        scores = atlas_search.vector_scores(db, norm, shop_ids)
        return lambda ids: scores
    return nearby_similarities(db, norm)


def engine_labels(engines: EnginePair) -> tuple[str, str]:
    """engine.keyword and engine.semantic as a search with these engines reports them."""
    keyword = KEYWORD_ENGINE_LABELS[engines.keyword]
    if engines.vector == "atlas_vector":
        from app.ai.semantic_index import get_provider

        return keyword, f"{ATLAS_VECTOR_LABEL}:{get_provider().name}"
    return keyword, index.provider_name or "loading"


def _norm_sem(sim: float, cfg: RankingConfig) -> float:
    return min(max((sim - cfg.sem_floor) / (cfg.sem_ceil - cfg.sem_floor), 0.0), 1.0)


@dataclass
class Fetched:
    """What run_search fetches, none of it touched by the ranking config: the shops in radius, RANK_FIELDS of the
    listings that pass the filters (in fetch order), keyword scores, local-term match counts and semantic
    similarities. docs collects the DISPLAY_FIELDS of the listings shown so far, so that a re-ranking from a cache
    reads only the listings it shows that no earlier ranking did; docs_ms is the time those reads took."""

    shops: dict[int, tuple[Shop, float]]
    rows: list[dict]
    kw: dict[int, float]
    local: dict[int, int]
    sem: dict[int, float]
    docs: dict[int, dict] = field(default_factory=dict)
    docs_ms: float = 0.0


def _candidates(db: Database, p: SearchParams, norm: str, expanded: list[str], now: datetime,
                engines: EnginePair | None = None) -> Fetched:
    """Everything run_search fetches before ranking (Fetched), the scores from the chosen engines
    (settings.search_engines by default). Every stage is limited to the shops in radius (P8.2)."""
    engines = engines or settings.search_engines
    category = category_by_slug(db, p.category)
    shops = nearby_shops(db, p.lat, p.lng, p.radius_km, p.fulfillment, p.shop_id)

    query: dict = {"is_active": True, "shop_id": {"$in": list(shops)}}
    if p.category:
        query["category_id"] = category.id if category else -1
    if p.min_price is not None or p.max_price is not None:
        query["price"] = {**({"$gte": p.min_price} if p.min_price is not None else {}),
                          **({"$lte": p.max_price} if p.max_price is not None else {})}
    if p.in_stock_only:  # in stock, and confirmed within STALE_DAYS (P7.4)
        query["quantity"] = {"$gt": 0}
        query["stock_updated_at"] = {"$gte": now - timedelta(days=STALE_DAYS)}
    # The listing rows, the keyword scores and the vector stage's query are independent: fetched side by side. The
    # semantic similarities are then computed for the listings fetched, and only for them.
    shop_ids = list(shops)
    rows, (kw, local), scorer = db.gather(
        lambda: db.products.find_raw(query, RANK_FIELDS) if shops else [],
        lambda: _keyword_stage(db, norm, expanded, engines.keyword, shop_ids) if norm else ({}, {}),
        lambda: _vector_stage(db, norm, engines.vector, shop_ids) if norm else None,
    )
    sem = scorer([r["_id"] for r in rows]) if scorer is not None and rows else {}
    return Fetched(shops, rows, kw, local, sem)


def fetch_ms(cache: dict) -> float:
    """The milliseconds the fetches memoised in a run_search cache took. The P5.6 shadow ranking with an engine pair
    (app/services/search_events.py) times production with it instead of fetching production again."""
    return sum(ms + fetched.docs_ms for fetched, ms in cache.values())


def _freshness_multiplier(pool: list[dict], now: datetime) -> float:
    """The freshness multiplier of the freshest offer in pool (P7.4): the multiplier only falls with age, so it is
    the multiplier of the latest stock confirmation; an offer never confirmed counts as stale."""
    latest = max((x["stock_updated_at"] for x in pool if x.get("stock_updated_at")), default=None)
    return freshness(latest, now)["multiplier"]


def _shown(db: Database, fetched: Fetched, rows: list[dict]) -> dict[int, Product]:
    """The listings a page shows (candidate rows), as models: RANK_FIELDS from the rows, so they show what was
    ranked, and DISPLAY_FIELDS read now for the listings no earlier ranking from the same fetch has shown. A listing
    deleted meanwhile is left out."""
    missing = [r["_id"] for r in rows if r["_id"] not in fetched.docs]
    if missing:
        started = time.perf_counter()
        for doc in db.products.find_raw({"_id": {"$in": missing}}, DISPLAY_FIELDS):
            fetched.docs[doc["_id"]] = doc
        fetched.docs_ms += (time.perf_counter() - started) * 1000
    out = [Product.model_validate({**fetched.docs[r["_id"]], **r}) for r in rows if r["_id"] in fetched.docs]
    return {pr.id: pr for pr in with_category(db, out)}


def run_search(db: Database, p: SearchParams, user_id: int | None = None, log: bool = True,
               ranking: RankingConfig | None = None, cache: dict | None = None, now: datetime | None = None,
               engines: EnginePair | None = None) -> dict:
    """Search. `ranking` overrides the configured ranking config (the evaluation and tuning harnesses).
    `cache`, a dict kept by the caller across calls, memoises the fetch stage per query and filters, so a
    tuning run can re-rank the same candidates under many configs. The live API passes one only for a search it
    shadows (P5.6, app/services/search_events.py), together with `now`, the time freshness is judged at
    (search_now() by default), so the shadow ranking re-ranks exactly the listings and scores the customer's
    search fetched. `engines` overrides the configured engines (P5.7: the evaluation harness and the shadow engine
    pair); the cache keeps each engine pair's fetch apart, with the time it took (fetch_ms). A radius above
    MAX_RADIUS_KM is searched at MAX_RADIUS_KM, and a page_size above MAX_PAGE_SIZE is served as MAX_PAGE_SIZE
    (P8.2); the response's radius_km and page_size are the values searched."""
    cfg = ranking or settings.ranking
    engines = engines or settings.search_engines
    if p.radius_km > MAX_RADIUS_KM:
        p = replace(p, radius_km=MAX_RADIUS_KM)
    if p.page_size > MAX_PAGE_SIZE:
        p = replace(p, page_size=MAX_PAGE_SIZE)
    norm = normalize_query(p.q) if p.q else ""
    expanded = expand_synonyms(norm) if norm else []
    now = now or search_now()
    if cache is None:
        fetched = _candidates(db, p, norm, expanded, now, engines)
    else:
        key = (norm, p.lat, p.lng, p.radius_km, p.category, p.min_price, p.max_price, p.in_stock_only,
               p.fulfillment, p.shop_id, now if p.in_stock_only else None, engines)
        if key not in cache:
            started = time.perf_counter()
            fresh = _candidates(db, p, norm, expanded, now, engines)
            cache[key] = (fresh, (time.perf_counter() - started) * 1000)
        fetched, _ = cache[key]
    shops, rows, kw, local, sem = fetched.shops, fetched.rows, fetched.kw, fetched.local, fetched.sem
    if local:
        kw = dict(kw)
        for pid, matched in local.items():
            kw[pid] = kw.get(pid, 0.0) + cfg.w_local_term * matched

    relevance: dict[int, float] = {}
    if norm:
        kw_max = max((kw.get(r["_id"], 0.0) for r in rows), default=0.0) or 1.0
        for r in rows:
            k = kw.get(r["_id"], 0.0) / kw_max
            s = sem.get(r["_id"], 0.0)
            if k <= 0 and s < cfg.sem_min_match:
                continue
            relevance[r["_id"]] = cfg.w_semantic * _norm_sem(s, cfg) + cfg.w_keyword * k
        if relevance:
            best = max(relevance.values())
            relevance = {k: v for k, v in relevance.items() if v >= best * cfg.relative_cutoff}
        rows = [r for r in rows if r["_id"] in relevance]
    else:
        relevance = {r["_id"]: 1.0 for r in rows}

    shop_cache: dict[int, dict] = {}

    def shop_json(sid: int) -> dict:
        if sid not in shop_cache:
            s, d = shops[sid]
            shop_cache[sid] = shop_brief(s, d)
        return shop_cache[sid]

    def km(x: dict) -> float:
        return shops[x["shop_id"]][1]

    # Group listings of the same catalog item, and rank the groups, on the candidate rows (RANK_FIELDS).
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[f"c{r['catalog_item_id']}" if r.get("catalog_item_id") else f"p{r['_id']}"].append(r)

    cats = categories(db)
    group_rows = []
    category_counts: dict[str, int] = defaultdict(int)
    for key, items in groups.items():
        in_stock = [x for x in items if x["quantity"] > 0]
        pool = in_stock or items
        nearest = min(km(x) for x in pool)
        rel = max(relevance[x["_id"]] for x in items)
        proximity = max(0.0, 1 - nearest / max(p.radius_km, 0.5))
        score = cfg.w_relevance * rel + cfg.w_proximity * proximity + (cfg.w_in_stock if in_stock else 0.0)
        if not in_stock:
            score *= cfg.out_of_stock_factor
        score *= _freshness_multiplier(pool, now)  # P7.4
        prices = [float(x["price"]) for x in pool]
        # Best offer: in stock, balancing price and distance.
        lo, hi = min(prices), max(prices)
        best = min(pool, key=lambda x: ((float(x["price"]) - lo) / (hi - lo) if hi > lo else 0)
                   + km(x) / max(p.radius_km, 1))
        offers = sorted(items, key=lambda x: (x["quantity"] <= 0, km(x)))[:OFFERS_SHOWN]
        category_counts[cats[best["category_id"]].slug] += 1
        group_rows.append({
            "key": key, "relevance": round(rel, 3), "score": round(score, 4), "nearest_km": round(nearest, 2),
            "shop_count": len(items), "in_stock_count": len(in_stock), "min_price": lo, "max_price": hi,
            "best": best, "offers": offers,
            "image_path": next((x["image_path"] for x in items if x.get("image_path")), None),  # any offer's photo
        })

    sorters = {
        "relevance": lambda g: -g["score"],
        "distance": lambda g: (g["in_stock_count"] == 0, g["nearest_km"]),
        "price_asc": lambda g: (g["in_stock_count"] == 0, g["min_price"]),
        "price_desc": lambda g: (g["in_stock_count"] == 0, -g["min_price"]),
    }
    if not norm and p.sort == "relevance":
        # Browsing without a query: in-stock items near you first.
        group_rows.sort(key=lambda g: (g["in_stock_count"] == 0, g["nearest_km"], -g["shop_count"]))
    else:
        group_rows.sort(key=sorters.get(p.sort, sorters["relevance"]))

    total = len(group_rows)
    start = (max(p.page, 1) - 1) * p.page_size
    page_rows = group_rows[start : start + p.page_size]

    # Only the page's offers are read in full: at most MAX_FULL_READS listings (page_size <= MAX_PAGE_SIZE).
    to_show = {x["_id"]: x for g in page_rows for x in [g["best"], *g["offers"]]}
    shown = _shown(db, fetched, list(to_show.values()))
    page_groups = []
    for g in page_rows:
        best_offer = shown.get(g["best"]["_id"])
        if best_offer is None:  # deleted since the fetch
            continue
        page_groups.append({
            "key": g["key"],
            "catalog_item_id": best_offer.catalog_item_id,
            "name": best_offer.name,
            "brand": best_offer.brand,
            "unit": best_offer.unit,
            "icon": best_offer.icon,
            "image_url": media_url(g["image_path"]),
            "thumbnail_url": thumbnail_url(g["image_path"]),
            "category": best_offer.category.slug,
            "min_price": g["min_price"],
            "max_price": g["max_price"],
            "mrp": best_offer.mrp,
            "nearest_km": g["nearest_km"],
            "shop_count": g["shop_count"],
            "in_stock_count": g["in_stock_count"],
            "relevance": g["relevance"],
            "score": g["score"],
            "best_offer": public_listing(best_offer, shop_json(best_offer.shop_id), now,
                                         seller=shops[best_offer.shop_id][0]),
            "offers": [public_listing(x, shop_json(x.shop_id), now, seller=shops[x.shop_id][0])
                       for o in g["offers"] if (x := shown.get(o["_id"]))],
        })

    # Map pins: every shop with at least one matching listing.
    pins: dict[int, dict] = {}
    for r in rows:
        pin = pins.setdefault(r["shop_id"], {**shop_json(r["shop_id"]), "match_count": 0, "in_stock_count": 0,
                                             "min_price": None})
        pin["match_count"] += 1
        if r["quantity"] > 0:
            price = float(r["price"])
            pin["in_stock_count"] += 1
            pin["min_price"] = price if pin["min_price"] is None else min(pin["min_price"], price)

    did_you_mean = suggest_correction(db, norm) if norm and total < 3 else None

    # Nothing found but the query looks misspelled: show results for the correction instead of an empty page.
    if total == 0 and did_you_mean:
        corrected = run_search(db, replace(p, q=did_you_mean), user_id=user_id, log=log, ranking=ranking,
                               cache=cache, now=now, engines=engines)
        if corrected["total"]:
            return {**corrected, "query": p.q, "corrected_from": p.q, "did_you_mean": None}

    if log and norm:
        # Logged after the response is on its way: analytics must not make search slower. in_stock_results (P9.7):
        # the matching listings inside the radius that are in stock as the in-stock filter means it (P7.4).
        fresh_since = now - timedelta(days=STALE_DAYS)
        in_stock = sum(1 for r in rows if r["quantity"] > 0 and r.get("stock_updated_at")
                       and r["stock_updated_at"] >= fresh_since)
        db.background(lambda: log_search(Database(db.mongo.name), user_id, p, norm, total, in_stock))

    keyword_label, semantic_label = engine_labels(engines)
    return {
        "query": p.q,
        "normalized_query": norm,
        "expanded_terms": expanded,
        "did_you_mean": did_you_mean,
        "corrected_from": None,
        "total": total,
        "page": p.page,
        "page_size": p.page_size,
        "radius_km": p.radius_km,
        "has_more": start + p.page_size < total,
        "shops_in_radius": len(shops),
        "groups": page_groups,
        "shops": sorted(pins.values(), key=lambda s: s["distance_km"]),
        "category_counts": dict(category_counts),
        "engine": {"semantic": semantic_label, "keyword": keyword_label, "ranking": cfg.version},
    }


def demand_key_of(user_id: int | None) -> str | None:
    """A user's demand key (P9.7; plan section 8.19): HMAC-SHA256 of the user id with NEARSHOP_DEMAND_KEY_SECRET, as
    64 hex characters. None without a user or while the secret is unset. Never shown, exported or joined back."""
    secret = settings.demand_key_secret.get_secret_value()
    if user_id is None or not secret:
        return None
    return hmac.new(secret.encode(), f"user:{user_id}".encode(), hashlib.sha256).hexdigest()


def demand_key(db: Database, user_id: int | None) -> str | None:
    """The demand key stored on a search: demand_key_of the signed-in user, and, while demand_key_needs_consent is
    on, only when the user's latest consent for demand_key_consent_purpose is granted. Signed-out searches: None."""
    if user_id is None or not settings.demand_key_secret.get_secret_value():
        return None
    if settings.demand_key_needs_consent and not consent_granted(db, user_id, settings.demand_key_consent_purpose):
        return None
    return demand_key_of(user_id)


def log_search(db: Database, user_id: int | None, p: SearchParams, norm: str, total: int,
               in_stock_results: int = 0) -> None:
    """Write one search_history row (retention: plan section 8.4, docs/legal/retention.md). The location is
    rounded to 2 decimals (about 1.1 km), and user_id is kept only while the user's personalised-suggestions
    consent is in force; the nightly retention job removes it and the demand key (P9.7) after 30 days, and the TTL
    index deletes the row after 180."""
    key = demand_key(db, user_id)
    if user_id is not None and not consent_granted(db, user_id, "personalised_suggestions"):
        user_id = None
    db.search_history.insert(SearchEvent(
        user_id=user_id, query=p.q[:200], normalized_query=norm[:200], category_slug=p.category,
        results_count=total, in_stock_results=max(in_stock_results, 0), demand_key=key,
        lat=round(p.lat, LOCATION_DECIMALS), lng=round(p.lng, LOCATION_DECIMALS)))


# ---------------------------------------------------------------- "did you mean"

# Per database: the words, and the shared listings version they were built for (app/core/cache_version.py,
# P8.1), so every process's corrections follow the listings within 5 seconds. The catalogue and category
# words are read in full only when the vocabulary is first built, after a version that went down (a reset
# database) and once a night: the app never writes catalogue items or categories (seeds and migrations do).
# A new listings version re-reads only the listings changed since the last refresh (by updated_at, as the
# semantic index does), so a bump that changed no listing (an admin verifying a shop) reads no words at all.
@dataclass(frozen=True)
class _Vocab:
    version: int
    base: frozenset[str]  # catalogue names, brands, tags and local terms, and category names
    listings: dict[int, frozenset[str]]  # active shop-specific listings (no catalogue item): id -> words
    words: frozenset[str]  # base and every listing's words
    since: datetime  # the next refresh re-reads listings with updated_at at or after this
    full_at: datetime  # when the catalogue and every listing were last read


_vocab: dict[str, _Vocab] = {}
# Vocabulary words: Latin letters (lower-cased), or Kannada, Telugu or Devanagari letters with their vowel
# signs and viramas (P5.2). Three characters or more, as before.
_VOCAB_WORD = re.compile(r"[a-z]{3,}|[\u0900-\u097f\u0c00-\u0c7f\u0c80-\u0cff]{3,}")
_LISTING_FIELDS = {"name": 1, "keywords": 1, "catalog_item_id": 1, "is_active": 1}
# Full builds and incremental refreshes of the vocabulary, for tests.
vocab_builds = {"full": 0, "refresh": 0}


def _vocab_words(text: str) -> list[str]:
    return [w for part in word_parts(text.lower()) for w in _VOCAB_WORD.findall(part)]


def _listing_words(row: dict) -> frozenset[str] | None:
    """A listing's vocabulary words, or None when it adds none (inactive, or a catalogue item's listing)."""
    if not row.get("is_active") or row.get("catalog_item_id") is not None:
        return None
    return frozenset(_vocab_words(f"{row['name']} {row.get('keywords') or ''}"))


def _union(base: frozenset[str], listings: dict[int, frozenset[str]]) -> frozenset[str]:
    return base.union(*listings.values())


def _full_vocabulary(db: Database, version: int) -> _Vocab:
    started = utcnow()
    base: set[str] = set()
    for c in db.catalog_items.find_raw({}, {"name": 1, "brand": 1, "tags": 1, "local_terms": 1}):
        for chunk in [c["name"], c.get("brand") or "", *(c.get("tags") or []), *(c.get("local_terms") or [])]:
            base.update(_vocab_words(chunk))
    for c in categories(db).values():
        base.update(_vocab_words(c.name))
    listings = {r["_id"]: w for r in db.products.find_raw({"catalog_item_id": None, "is_active": True}, _LISTING_FIELDS)
                if (w := _listing_words(r)) is not None}
    vocab_builds["full"] += 1
    frozen = frozenset(base)
    return _Vocab(version, frozen, listings, _union(frozen, listings), started - OVERLAP, started)


def _refreshed_vocabulary(db: Database, old: _Vocab, version: int) -> _Vocab:
    started = utcnow()
    rows = db.products.find_raw({"updated_at": {"$gte": old.since}}, _LISTING_FIELDS)
    vocab_builds["refresh"] += 1
    if not rows:
        return replace(old, version=version, since=started - OVERLAP)
    listings = dict(old.listings)
    for r in rows:
        words = _listing_words(r)
        if words is None:
            listings.pop(r["_id"], None)
        else:
            listings[r["_id"]] = words
    return replace(old, version=version, listings=listings, words=_union(old.base, listings),
                   since=started - OVERLAP)


def _vocabulary(db: Database) -> frozenset[str]:
    """Every word customers could mean: catalogue names, brands, tags, category names and the
    names of shop-specific listings, and the catalogue's local terms (Kannada, Telugu, Devanagari and Latin
    transliterations). Cached per process, and refreshed when the shared listings version changes."""
    key = db.mongo.name
    version = cache_version.watcher.version(db.mongo, cache_version.LISTINGS)  # read before the words
    hit = _vocab.get(key)
    if hit is None or version < hit.version or hit.full_at < nightly_slot(utcnow()):
        hit = _vocab[key] = _full_vocabulary(db, version)
    elif hit.version != version:
        hit = _vocab[key] = _refreshed_vocabulary(db, hit, version)
    return hit.words


def suggest_correction(db: Database, norm: str) -> str | None:
    vocab = _vocabulary(db)
    if not vocab:
        return None
    changed = False
    out = []
    for word in norm.split():
        if len(word) < 4 or word in vocab or not is_word(word):
            out.append(word)
            continue
        match = process.extractOne(word, vocab, scorer=fuzz.ratio, score_cutoff=78)
        if match:
            out.append(match[0])
            changed = True
        else:
            out.append(word)
    return " ".join(out) if changed else None


# ---------------------------------------------------------------- suggestions

# Suggestion hygiene (P5.5): what may never appear in suggestions or popular searches, whoever searched it.
# The patterns run on normalised queries, which keep digits, dots and slashes but drop @ and colons.
# A phone number: seven or more digits joined only by dashes, dots or brackets ("080-2345-6789", "9845012345"),
# or, when spaces split the digits, ten or more digits starting with 0 or 6 to 9, the way Indian mobile and
# STD numbers start ("98450 12345", "080 2345 6789"). So "iphone 15 256 2023" (a model, a size and a year)
# is not a phone number.
_PHONE = re.compile(r"(?<!\d)\d(?:[().-]*\d){6,}|(?<!\d)[06-9](?:[\s().-]*\d){9,}")
# An email address, or a mail provider's domain ("ravi gmail.com" once the @ is dropped); a provider's bare
# name ("yahoo", "outlook diary") is fine.
_MAIL_PROVIDERS = r"(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|rediffmail|icloud|protonmail)"
_EMAIL = re.compile(rf"[^\s@]+@[^\s@]+|\b{_MAIL_PROVIDERS}\s*(?:\.|\bdot\b|\bcom\b)", re.IGNORECASE)
# A link: a scheme, www., a messaging short link, a domain followed by a path, or a domain on a spam-prone
# top-level domain. A shop or brand domain on its own ("amazon.in gift card") is fine.
_LINK = re.compile(r"\bhttps?\b|\bwww\.|\bt\.me\b|\bwa\.me\b|[a-z0-9]\.[a-z]{2,6}/|"
                   r"[a-z0-9]\.(?:xyz|ly|link|me|io|info|biz|top|club|site|online|click)\b", re.IGNORECASE)
# Abusive words in English, and in Hindi, Kannada and Telugu, in Latin letters and in their own scripts.
# A Latin word matches as a whole word, with or without a common ending ("fucking", "bastards"), so
# "rapeseed" or "dickies" are not caught. A word in _ABUSIVE_EXACT matches only as itself, as its endings make
# ordinary words ("sexy dress", "lunda"). Words that are also ordinary shopping terms ("nude" lipstick, "dengue"
# test kit) are left out, and _SAFE_PHRASES ("rape seed oil") are removed before matching. A word in an Indian
# script matches anywhere, as those scripts join suffixes to the word. NEARSHOP_SUGGESTION_BLOCKLIST adds
# words without a deploy.
_ABUSIVE_LATIN = (
    "fuck", "fucker", "motherfuck", "motherfucker", "shit", "bitch", "bastard", "asshole", "dick", "cunt", "slut",
    "whore", "porn", "rape", "rapist", "nigger", "faggot",
    "chutiya", "chutiye", "chutia", "madarchod", "maderchod", "behenchod", "bhenchod", "benchod", "bhosdi",
    "bhosdike", "bhosad", "bhosadi", "gandu", "randi", "harami", "lavda", "lauda", "kamina", "kamine",
    "sule", "soole", "bolimaga", "boli maga", "sulemaga", "soolemaga", "tunne", "nin amman", "ninamman",
    "lanja", "dengey", "dengu ra", "puku", "modda", "kojja",
)
_ABUSIVE_EXACT = ("sex", "lund")
_SAFE_PHRASES = re.compile(r"(?<![a-z0-9])rape\s*seeds?(?![a-z0-9])", re.IGNORECASE)
_ABUSIVE_SCRIPT = (
    "चूतिया", "चुतिया", "मादरचोद", "बहनचोद", "भेनचोद", "भोसड", "गांडू", "रंडी", "हरामी", "लौड़ा",
    "ಸೂಳೆ", "ಬೋಳಿಮಗ", "ತುಣ್ಣೆ", "ಸೂಳೆಮಗ",
    "లంజ", "దెంగ", "పూకు", "మొడ్డ", "కొజ్జ",
)
_ENDINGS = r"(?:s|es|er|ers|ing|in|ed|y|a|e|i|o)?"


@lru_cache(maxsize=4)
def _blocklist(configured: str) -> tuple[re.Pattern[str], tuple[str, ...]]:
    """The abusive-word matchers, with the configured extra words (NEARSHOP_SUGGESTION_BLOCKLIST)."""
    extra = [w.strip().lower() for w in configured.split(",") if w.strip()]
    latin = sorted({w for w in (*_ABUSIVE_LATIN, *extra) if w.isascii()}, key=len, reverse=True)
    script = (*_ABUSIVE_SCRIPT, *(w for w in extra if not w.isascii()))
    words = "|".join(re.escape(w) for w in latin)
    exact = "|".join(re.escape(w) for w in _ABUSIVE_EXACT)
    return re.compile(rf"(?<![a-z0-9])(?:(?:{words}){_ENDINGS}|{exact})(?![a-z0-9])", re.IGNORECASE), script


def blocklisted(query: str) -> bool:
    """Whether a query's text holds a phone number, an email address, a link or an abusive word (the P5.5
    blocklist, with NEARSHOP_SUGGESTION_BLOCKLIST's words). The demand report (P9.7) drops such queries too."""
    text = (query or "").strip()
    if _PHONE.search(text) or _EMAIL.search(text) or _LINK.search(text):
        return True
    latin, script = _blocklist(settings.suggestion_blocklist)
    folded = _SAFE_PHRASES.sub(" ", text.lower())
    return bool(latin.search(folded)) or any(w in folded for w in script)


def suggestion_allowed(query: str) -> bool:
    """Whether a query's text may appear in public suggestions: at most suggestion_max_chars characters, and no
    phone number, email address, link or abusive word."""
    text = (query or "").strip()
    if not text or len(text) > settings.suggestion_max_chars:
        return False
    return not blocklisted(text)


def _top_queries(db: Database, match: dict, limit: int) -> list[str]:
    """The most searched normalised queries that some result answered, counting distinct signed-in users only:
    a query qualifies when suggestion_min_users or more users (by user_id, never by session or device)
    searched it within suggestion_window_days, and its text passes suggestion_allowed."""
    since = utcnow() - timedelta(days=settings.suggestion_window_days)
    rows = db.search_history.aggregate([
        {"$match": {**match, "results_count": {"$gt": 0}, "user_id": {"$ne": None}, "created_at": {"$gte": since}}},
        {"$group": {"_id": "$normalized_query", "users": {"$addToSet": "$user_id"}, "n": {"$sum": 1}}},
        {"$project": {"n": 1, "u": {"$size": "$users"}}},
        {"$match": {"u": {"$gte": settings.suggestion_min_users}}},
        {"$sort": {"u": -1, "n": -1, "_id": 1}},
        {"$limit": limit * 5},  # room for the ones the blocklist drops
    ])
    return [r["_id"] for r in rows if isinstance(r["_id"], str) and suggestion_allowed(r["_id"])][:limit]


def _suggested_items(db: Database, norm: str, limit: int, keyword_engine: str) -> list[dict]:
    """Catalogue items for suggestions: the Atlas autocomplete index when the keyword engine is atlas_search (P5.7),
    otherwise a substring match on the name and a prefix match on the brand. Raw documents."""
    if keyword_engine == "atlas_search":
        return atlas_search.autocomplete_items(db, norm, limit)
    contains = {"$regex": re.escape(norm), "$options": "i"}
    starts = {"$regex": f"^{re.escape(norm)}", "$options": "i"}
    return db.catalog_items.find_raw({"$or": [{"name": contains}, {"brand": starts}]},
                                     {"name": 1, "brand": 1, "icon": 1, "category_id": 1}, limit=limit)


def suggestions(db: Database, q: str, limit: int = 8, engines: EnginePair | None = None) -> dict:
    norm = normalize_query(q)
    if len(norm) < 2:
        return {"queries": [], "items": [], "categories": []}
    keyword_engine = (engines or settings.search_engines).keyword
    cats = categories(db)
    queries, items = db.gather(
        # Anchored, case-sensitive prefix on normalised (lower-case) text: served by the index.
        lambda: _top_queries(db, {"normalized_query": {"$regex": f"^{re.escape(norm)}"}}, 4),
        lambda: _suggested_items(db, norm, limit, keyword_engine),
    )
    return {
        "queries": queries,
        "items": [{"id": i["_id"], "name": i["name"], "brand": i.get("brand"), "icon": i.get("icon", "Package"),
                   "category": cats[i["category_id"]].slug if i.get("category_id") in cats else None}
                  for i in items],
        "categories": [{"slug": c.slug, "name": c.name, "icon": c.icon} for c in cats.values()
                       if norm in c.name.lower()],
    }


def popular_searches(db: Database, limit: int = 8) -> list[str]:
    return _top_queries(db, {}, limit)


def recent_searches(db: Database, user_id: int, limit: int = 6) -> list[str]:
    return [r["_id"] for r in db.search_history.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$normalized_query", "t": {"$max": "$created_at"}}},
        {"$sort": {"t": -1}},
        {"$limit": limit},
    ])]


def reset_vocabulary() -> None:
    """Forget this process's vocabularies (tests and the evaluation harness): rebuilt on the next search."""
    _vocab.clear()
