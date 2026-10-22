"""Search engines (P5.7): the text index and in-process vectors today, Atlas Search and Atlas Vector Search as
candidates (app/ai/atlas_search.py; docs/search/engine-decision.md)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace
from datetime import datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymongo import MongoClient

from app.ai import atlas_search, search_eval, semantic_index
from app.core import cache_version
from app.core import database as database_module
from app.core.config import EnginePair, Settings, settings
from app.core.database import Database, utcnow
from app.main import app
from app.models import CatalogItem, Category, Product, Role, Shop, User
from app.services import search as search_module
from app.services import search_events as se
from app.services import synonyms
from app.services.search import SearchParams, run_search, suggestions
from database import admin_tasks, schema
from tests.conftest import CENTER as TEST_CENTER
from tests.conftest import login, verified_shop_fields

NOW = datetime(2026, 10, 20, 6, 0, 0)
CENTER = (15.1394, 76.9214)


class HashProvider:
    """A deterministic embedding provider: hashed character trigrams, L2-normalised. It makes the snapshot
    independent of which corpus the process's TF-IDF fallback was fitted on."""

    name = "test-hash-trigrams"
    dim = 64

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        if kind == "passage":
            self.embedded.extend(texts)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            padded = f"  {text.lower()}  "
            for i in range(len(padded) - 2):
                h = int(hashlib.sha256(padded[i:i + 3].encode()).hexdigest()[:8], 16)
                out[row, h % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


@pytest.fixture
def provider(monkeypatch):
    fake = HashProvider()
    monkeypatch.setattr(semantic_index, "get_provider", lambda fallback_corpus=None: fake)
    semantic_index.index.mark_dirty()
    yield fake
    semantic_index.index.mark_dirty()


@pytest.fixture(scope="module")
def snap_db(ids):
    """Its own small database, so the snapshot does not depend on what other tests changed."""
    name = f"{settings.mongodb_db}_engines"
    db = Database(name)
    schema.reset(db.mongo, confirm=name)
    mobile = db.categories.insert(Category(slug="mobile-accessories", name="Mobile Accessories", icon="Smartphone"))
    plumbing = db.categories.insert(Category(slug="plumbing", name="Plumbing", icon="Droplets", sort_order=1))
    items = db.catalog_items.insert_many([
        CatalogItem(slug="samsung-25w-charger", name="Samsung 25W USB-C Fast Travel Charger", brand="Samsung",
                    category_id=mobile.id, tags=["phone charger", "adapter", "fast charger"], icon="PlugZap"),
        CatalogItem(slug="boat-type-c-cable", name="boAt Type-C to Type-C Charging Cable 1.5m", brand="boAt",
                    category_id=mobile.id, tags=["usb cable", "charging cable", "data cable"]),
        CatalogItem(slug="teflon-tape", name="Astral PTFE Thread Seal Tape", brand="Astral",
                    category_id=plumbing.id, tags=["teflon tape", "leak", "tap leak"]),
        CatalogItem(slug="pipe-wrench", name="Taparia Pipe Wrench 14 inch", brand="Taparia",
                    category_id=plumbing.id, tags=["pipe spanner", "wrench"]),
    ])
    db.catalog_items.raw.update_one({"_id": items[0].id}, {"$set": {"local_terms": ["ಚಾರ್ಜರ್", "charger"]}})
    owners = db.users.insert_many([User(name=f"Owner {n}", email=f"engines{n}@example.com", password_hash="x",
                                        role=Role.OWNER) for n in range(3)])
    shops = db.shops.insert_many([
        Shop(owner_id=owners[n].id, name=f"Shop {n}", slug=f"engines-shop-{n}", address_line="Road", city="Ballari",
             lat=CENTER[0] + 0.01 * n, lng=CENTER[1], category_ids=[mobile.id, plumbing.id], **verified_shop_fields(n))
        for n in range(3)])

    def listing(shop, item, price, qty, **extra):
        return Product(shop_id=shop.id, catalog_item_id=item.id, category_id=item.category_id, name=item.name,
                       brand=item.brand, keywords=" ".join(item.tags), price=price, quantity=qty,
                       stock_updated_at=NOW, **extra)

    products = db.products.insert_many([
        listing(shops[0], items[0], 1249, 3), listing(shops[1], items[0], 1199, 0), listing(shops[2], items[0], 1299, 5),
        listing(shops[0], items[1], 299, 10), listing(shops[1], items[2], 40, 50), listing(shops[2], items[2], 38, 4),
        listing(shops[0], items[3], 450, 2), listing(shops[2], items[3], 430, 0, is_active=False),
        Product(shop_id=shops[1].id, category_id=mobile.id, name="Mobile back cover for Redmi Note 13",
                keywords="cover case", price=199, quantity=7, stock_updated_at=NOW),
    ])
    db.products.raw.update_one({"_id": products[0].id}, {"$set": {"local_terms": ["ಚಾರ್ಜರ್", "charger"]}})
    yield db
    db.mongo.client.drop_database(name)


# ---------------------------------------------------------------- default engines: today's results, no Atlas query

QUERIES = [("charger", {}), ("samsung charger", {}), ("fast chargr", {}), ("chargr", {}), ("teflon", {}),
           ("leak", {}), ("ಚಾರ್ಜರ್", {}), ("cable", {"sort": "price_asc"}), ("", {}), ("wrench", {"radius_km": 1.0}),
           ("cover", {"in_stock_only": True}), ("xyzzy", {})]

# Captured with the code before P5.7 (commit 73002d1) on snap_db with HashProvider: key, score, relevance and
# shop count of each result group.
SNAPSHOT = {
    'charger|{}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c1', 1.0, 1.0, 3)]},
    'samsung charger|{}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c1', 1.0, 1.0, 3)]},
    'fast chargr|{}': {
        "total": 2, "did_you_mean": 'fast charger', "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c1', 1.0, 1.0, 3), ('c2', 0.6164, 0.489, 1)]},
    'chargr|{}': {
        "total": 3, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c2', 0.6705, 0.561, 1), ('c1', 0.6678, 0.557, 3), ('c4', 0.4618, 0.282, 1)]},
    'teflon|{}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c3', 0.7244, 0.677, 2)]},
    'leak|{}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": ['teflon tape', 'sealant', 'm-seal'], "keyword": 'mongodb-text-index',
        "groups": [('c3', 0.8353, 0.825, 2)]},
    'ಚಾರ್ಜರ್|{}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c1', 0.55, 0.4, 3)]},
    'cable|{"sort": "price_asc"}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c2', 1.0, 1.0, 1)]},
    '|{}': {
        "total": 5, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c1', 1.0, 1.0, 3), ('c4', 1.0, 1.0, 1), ('c2', 1.0, 1.0, 1), ('c3', 0.9666, 1.0, 2), ('p9', 0.9666, 1.0, 1)]},
    'wrench|{"radius_km": 1.0}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": [('c4', 1.0, 1.0, 1)]},
    'cover|{"in_stock_only": true}': {
        "total": 1, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": ['case'], "keyword": 'mongodb-text-index',
        "groups": [('p9', 0.8304, 0.818, 1)]},
    'xyzzy|{}': {
        "total": 0, "did_you_mean": None, "corrected_from": None,
        "expanded_terms": [], "keyword": 'mongodb-text-index',
        "groups": []},
}


def snapshot(db: Database) -> dict:
    out = {}
    for q, extra in QUERIES:
        r = run_search(db, SearchParams(q=q, lat=CENTER[0], lng=CENTER[1], **extra), log=False, now=NOW)
        out[f"{q}|{json.dumps(extra, sort_keys=True)}"] = {
            "total": r["total"], "did_you_mean": r["did_you_mean"], "corrected_from": r["corrected_from"],
            "expanded_terms": r["expanded_terms"], "keyword": r["engine"]["keyword"],
            "groups": [(g["key"], g["score"], g["relevance"], g["shop_count"]) for g in r["groups"]]}
    return out


@pytest.fixture
def no_atlas(monkeypatch):
    """Any Atlas query fails the test."""
    sent: list = []

    def refuse(collection, pipeline):
        sent.append((collection.name, pipeline))
        raise AssertionError(f"an Atlas query was sent to {collection.name}")

    monkeypatch.setattr(atlas_search, "runner", refuse)
    return sent


def test_default_engines_give_todays_results_and_send_no_atlas_query(snap_db, provider, no_atlas):
    assert settings.search_engines == EnginePair("text", "in_process")
    assert snapshot(snap_db) == SNAPSHOT
    items = suggestions(snap_db, "char")["items"]
    assert [i["name"] for i in items][:1] == ["Samsung 25W USB-C Fast Travel Charger"]
    assert no_atlas == []
    assert snap_db.mongo[atlas_search.VECTOR_COLLECTION].count_documents({}) == 0  # nothing kept in step


# ---------------------------------------------------------------- the settings choose the engines


class FakeAtlas:
    """Fixed hits per collection, and every pipeline it was sent."""

    def __init__(self, hits: dict[str, list[dict]]):
        self.hits = hits
        self.sent: list[tuple[str, list[dict]]] = []

    def __call__(self, collection, pipeline):
        self.sent.append((collection.name, pipeline))
        return [dict(h) for h in self.hits.get(collection.name, [])]

    def pipelines(self, collection: str) -> list[list[dict]]:
        return [p for c, p in self.sent if c == collection]


def _listing(db: Database, name_part: str, shop_slug: str | None = None) -> dict:
    query: dict = {"name": {"$regex": name_part}}
    if shop_slug:
        query["shop_id"] = db.shops.find_one({"slug": shop_slug}).id
    return db.products.find_raw(query, sort=[("_id", 1)])[0]


def test_settings_choose_the_engines(snap_db, provider, monkeypatch):
    tape = _listing(snap_db, "PTFE", "engines-shop-1")
    cable = _listing(snap_db, "Charging Cable")
    fake = FakeAtlas({"products": [{"_id": tape["_id"], "score": 4.0}],
                      "listing_vectors": [{"_id": cable["_id"], "score": 0.9}],
                      "catalog_items": [{"_id": 3, "name": "Astral PTFE Thread Seal Tape", "brand": "Astral",
                                         "category_id": 2}]})
    monkeypatch.setattr(atlas_search, "runner", fake)
    p = SearchParams(q="seal tape", lat=CENTER[0], lng=CENTER[1], radius_km=1.5)
    shops_in_radius = sorted(s.id for s in snap_db.shops.find() if abs(s.lat - CENTER[0]) < 0.0135)

    monkeypatch.setattr(settings, "search_keyword_engine", "atlas_search")
    res = run_search(snap_db, p, log=False, now=NOW)
    assert res["engine"]["keyword"] == "atlas-search" and fake.pipelines("listing_vectors") == []
    [pipeline] = fake.pipelines("products")
    compound = pipeline[0]["$search"]["compound"]
    assert pipeline[0]["$search"]["index"] == atlas_search.PRODUCTS_INDEX
    assert sorted(compound["filter"][0]["in"]["value"]) == shops_in_radius  # only the nearby shops
    assert {"equals": {"path": "is_active", "value": True}} in compound["filter"]
    assert "c3" in [g["key"] for g in res["groups"]]
    assert suggestions(snap_db, "astr")["items"][0]["name"] == "Astral PTFE Thread Seal Tape"
    assert fake.pipelines("catalog_items")[0][0]["$search"]["index"] == atlas_search.CATALOG_INDEX

    monkeypatch.setattr(settings, "search_keyword_engine", "text")
    monkeypatch.setattr(settings, "search_vector_engine", "atlas_vector")
    before = len(fake.sent)
    res = run_search(snap_db, p, log=False, now=NOW)
    assert res["engine"]["keyword"] == "mongodb-text-index"
    assert res["engine"]["semantic"] == f"atlas-vector-search:{provider.name}"
    [vector] = [pl for c, pl in fake.sent[before:] if c == "listing_vectors"]
    stage = vector[0]["$vectorSearch"]
    assert stage["index"] == atlas_search.VECTOR_INDEX and stage["filter"]["model"] == provider.name
    assert sorted(stage["filter"]["shop_id"]["$in"]) == shops_in_radius and stage["filter"]["is_active"] is True
    assert [c for c, _ in fake.sent[before:]] == ["listing_vectors"]  # the keyword stage stayed on the text index
    suggestions(snap_db, "astr")
    assert [c for c, _ in fake.sent[before:]] == ["listing_vectors"]  # suggestions follow the keyword engine


def test_engine_settings_are_validated():
    assert Settings(_env_file=None, environment="development", search_keyword_engine="atlas_search",
                    search_vector_engine="atlas_vector").search_engines == EnginePair("atlas_search", "atlas_vector")
    for environment in ("staging", "production"):
        with pytest.raises(ValidationError, match="keep their defaults"):
            Settings(_env_file=None, environment=environment, search_keyword_engine="atlas_search")
        with pytest.raises(ValidationError, match="keep their defaults"):
            Settings(_env_file=None, environment=environment, search_vector_engine="atlas_vector")
        shadowed = Settings(_env_file=None, environment=environment, search_shadow_engines="atlas_search,atlas_vector",
                            search_shadow_share=0.2)
        assert shadowed.search_engines == EnginePair() and shadowed.shadow_engines == EnginePair("atlas_search",
                                                                                                 "atlas_vector")
        assert shadowed.shadow_ranking is not None and shadowed.shadow_ranking.version == "r1"
    with pytest.raises(ValidationError, match="SHADOW_ENGINES"):
        Settings(_env_file=None, search_shadow_engines="atlas_search")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_keyword_engine="lucene")
    off = Settings(_env_file=None, search_shadow_engines="atlas_search,in_process", search_shadow_share=0.0)
    assert off.shadow_engines is None and off.shadow_ranking is None


# ---------------------------------------------------------------- shadow engine pair (P5.6 shadow mode)


@pytest.fixture
def consented(ids, monkeypatch):
    def inline(fn):
        try:
            fn()
        except Exception:
            pass

    monkeypatch.setattr(Database, "background", staticmethod(inline))
    db = Database()
    db.mongo[se.COLLECTION].delete_many({})
    user = db.users.find_one({"email": "cust@example.com"})
    db.mongo["consents"].insert_one({"_id": db.next_id("consents"), "user_id": user.id, "purpose": se.CONSENT_PURPOSE,
                                     "notice_version": "v1", "granted": True, "at": utcnow(), "source": "web"})
    with TestClient(app) as c:
        yield login(c, "cust@example.com")
    db.mongo[se.COLLECTION].delete_many({})
    db.mongo["consents"].delete_many({"user_id": user.id})
    se.reset_terms()


def test_a_shadow_engine_pair_never_changes_the_response(consented, ids, monkeypatch):
    fake = FakeAtlas({"products": [{"_id": ids["tape"], "score": 2.5}]})
    monkeypatch.setattr(atlas_search, "runner", fake)
    params = {"q": "samsung astral tape charger", "lat": TEST_CENTER[0], "lng": TEST_CENTER[1], "radius_km": 50}
    plain = consented.get("/api/search", params=params).json()
    assert fake.sent == []
    monkeypatch.setattr(settings, "search_shadow_engines", "atlas_search,in_process")
    monkeypatch.setattr(settings, "search_shadow_share", 1.0)
    fetched: list[EnginePair | None] = []
    real = search_module._candidates

    def counted(db, p, norm, expanded, now, engines=None):
        fetched.append(engines)
        return real(db, p, norm, expanded, now, engines)

    monkeypatch.setattr(search_module, "_candidates", counted)
    shadowed = consented.get("/api/search", params=params).json()
    # One fetch for the customer and one for the candidate: production is re-ranked from the customer's fetch.
    assert fetched == [EnginePair(), EnginePair("atlas_search", "in_process")]
    strip = lambda b: {k: v for k, v in b.items() if k != "search_id"}  # noqa: E731
    assert strip(shadowed) == strip(plain)  # customers see the production engines only
    assert [c for c, _ in fake.sent] == ["products"]  # the candidate ran, after the response
    event = Database().mongo[se.COLLECTION].find_one(sort=[("created_at", -1)])
    shadow = event["shadow"]
    assert shadow["keyword"] == "atlas-search" and shadow["ranking"] == "r1"
    assert shadow["production_label"] == f"r1 + mongodb-text-index + {shadow['vector']}"
    assert shadow["label"] == f"r1 + atlas-search + {shadow['vector']}"
    assert shadow["production_top"] == [g["key"] for g in plain["groups"]][:20]
    assert shadow["production_ms"] > 0 and shadow["ms"] > 0  # each a fetch and a ranking
    assert shadow["top"] and set(shadow["top"]) <= {g["key"] for g in plain["groups"]} | {"c2"}
    [row] = se.shadow_comparison(Database())
    assert row["engine_candidate"] is True and row["keyword"] == "atlas-search"


# ---------------------------------------------------------------- the index plan


def test_the_index_plan_fits_the_atlas_limits():
    for quantization in atlas_search.QUANTIZATIONS:
        plan = atlas_search.index_plan(quantization=quantization)
        assert len(plan) <= 3 and atlas_search.plan_problems(plan) == []
        assert all(spec.size <= 3 * 1024 for spec in plan)
        assert sum(spec.synonym_mappings for spec in plan) == 1 and all(s.synonym_mappings <= 1 for s in plan)
        [vector] = [s for s in plan if s.type == "vectorSearch"]
        field = vector.definition["fields"][0]
        assert field["numDimensions"] == 384 and field["similarity"] == "cosine"
        assert field["quantization"] == quantization
        assert {f["path"] for f in vector.definition["fields"][1:]} == {"shop_id", "is_active", "model"}
    products = atlas_search.products_definition()
    [indic] = products["analyzers"]
    filters = [f["type"] for f in indic["tokenFilters"]]
    assert "icuFolding" not in filters and "icuNormalizer" in filters and indic["tokenizer"]["type"] == "standard"
    assert products["mappings"]["fields"]["name"]["multi"]["hi"]["analyzer"] == "lucene.hindi"
    assert products["mappings"]["fields"]["name"]["multi"]["indic"]["analyzer"] == indic["name"]
    assert products["mappings"]["fields"]["local_terms"] == {"type": "token"}
    assert products["synonyms"][0]["source"]["collection"] == "search_synonyms"
    [text_index] = [m.document for m in schema.INDEXES["products"] if m.document["name"] == "text_search"]
    assert atlas_search.FIELD_WEIGHTS == text_index["weights"]  # today's weights, as boosts
    folded = json.loads(json.dumps(products))
    folded["analyzers"][0]["tokenFilters"].append({"type": "icuFolding"})
    bad = [atlas_search.IndexSpec("x", "products", "search", folded)] * 4
    problems = atlas_search.plan_problems(bad)
    assert any("4 indexes" in p for p in problems) and any("icuFolding" in p for p in problems)
    big = atlas_search.IndexSpec("big", "products", "search", {"pad": "x" * 4000})
    assert any("over 3072" in p for p in atlas_search.plan_problems([big]))


# ---------------------------------------------------------------- the Atlas query paths, against a fake


def test_the_keyword_query_keeps_fuzzy_and_synonyms_in_separate_clauses():
    pipeline = atlas_search.keyword_pipeline(["usb", "chargr", "flashlight"], ["chargers"], [3, 1])
    should = pipeline[0]["$search"]["compound"]["should"]
    assert all(not ("fuzzy" in c["text"] and "synonyms" in c["text"]) for c in should)
    exact = [c["text"] for c in should if "synonyms" in c["text"]]
    assert {c["path"]: c["score"]["boost"]["value"] for c in exact} == {"name": 10, "brand": 6, "keywords": 4,
                                                                         "description": 1}
    assert exact[0]["query"] == "usb chargr flashlight chargers"
    fuzzy = {(c["text"]["query"], c["text"]["fuzzy"]["maxEdits"]) for c in should if "fuzzy" in c["text"]}
    assert fuzzy == {("chargr", 1), ("flashlight", 2)}  # "usb" (3 letters) matches exactly only
    assert not any(isinstance(c["text"]["path"], dict) for c in should)  # Latin: no Hindi or Indic clauses
    assert pipeline[0]["$search"]["compound"]["minimumShouldMatch"] == 1
    assert pipeline[1] == {"$limit": atlas_search.KEYWORD_LIMIT}
    kn = atlas_search.keyword_pipeline(["ಚಾರ್ಜರ್"], [], [1])[0]["$search"]["compound"]["should"]
    multis = {c["text"]["path"]["multi"] for c in kn if isinstance(c["text"]["path"], dict)}
    assert multis == {"indic"}
    hi = atlas_search.keyword_pipeline(["चार्जर"], [], [1])[0]["$search"]["compound"]["should"]
    assert {c["text"]["path"]["multi"] for c in hi if isinstance(c["text"]["path"], dict)} == {"hi"}
    assert atlas_search.fuzzy_groups(["abc", "abcd", "abcdefg", "abcdefgh"]) == [(1, ["abcd", "abcdefg"]),
                                                                                 (2, ["abcdefgh"])]


def test_atlas_scores_are_scaled_like_the_text_index(snap_db, provider, monkeypatch):
    fake = FakeAtlas({"products": [{"_id": 7, "score": 2.0}, {"_id": 8, "score": 1.0}],
                      "listing_vectors": [{"_id": 7, "score": 0.9}, {"_id": 9, "score": 0.5}]})
    monkeypatch.setattr(atlas_search, "runner", fake)
    assert atlas_search.keyword_scores(snap_db, ["seal", "tape"], [], [1, 2]) == {7: 20.0, 8: 10.0}
    assert atlas_search.keyword_scores(snap_db, ["tape"], [], []) == {}  # no shop nearby: nothing is sent
    sims = atlas_search.vector_scores(snap_db, "seal tape", [1, 2])
    assert sims == pytest.approx({7: 0.8, 9: 0.0})  # Atlas's (1 + cosine) / 2, mapped back to the cosine
    assert len(fake.sent) == 2
    assert atlas_search.autocomplete_pipeline("cha", 8)[0]["$search"]["compound"]["should"][0]["autocomplete"].get(
        "fuzzy") is None
    assert atlas_search.autocomplete_pipeline("char", 8)[0]["$search"]["compound"]["should"][0]["autocomplete"][
        "fuzzy"] == {"maxEdits": 1}


def test_rank_fusion_is_reported_not_run():
    assert "8.1 or later" in atlas_search.rank_fusion_problem("8.0.4")
    assert "one collection" in atlas_search.rank_fusion_problem("8.2.2")


# ---------------------------------------------------------------- the autocomplete hit rate


def _q(qid: str, text: str, lang: str, judgments: dict[int, int]) -> search_eval.LabelledQuery:
    return search_eval.LabelledQuery(qid, text, lang, CENTER[0], CENTER[1], 5.0, judgments)


def test_the_autocomplete_hit_rate_on_a_toy_set(snap_db, no_atlas):
    charger = _q("t1", "charger", "en", {1: 3, 2: 1})
    tape = _q("t2", "teflon", "en", {3: 3})
    short = _q("t3", "led", "en", {1: 3})
    related_only = _q("t4", "cable", "kn-Latn", {2: 2})
    assert search_eval.autocomplete_cases(charger) == ["cha", "charg"]
    assert search_eval.autocomplete_cases(short) == ["led"]  # shorter than 5 characters: one prefix
    assert search_eval.autocomplete_cases(related_only) == []  # no grade-3 item
    assert search_eval.autocomplete_hit(charger, [5, 6, 7, 8, 9, 10, 11, 1])
    assert not search_eval.autocomplete_hit(charger, [5, 6, 7, 8, 9, 10, 11, 12, 1])  # 9th: outside the top 8
    rates = search_eval.autocomplete_rates([("en", True), ("en", False), ("kn", True), ("en", True)])
    assert rates["overall"] == {"prefixes": 4, "hit_rate": 0.75}
    assert list(rates["by_language"]) == ["en", "kn"] and rates["by_language"]["en"]["hit_rate"] == 0.6667
    # On snap_db: "cha" and "charg" find the charger; "tef" and "teflo" match no catalogue name or brand.
    measured = search_eval.evaluate_autocomplete(snap_db, [charger, tape, related_only])
    assert measured["overall"] == {"prefixes": 4, "hit_rate": 0.5}


def test_database_operations_per_search_are_counted(snap_db, provider, monkeypatch):
    counter = search_eval.CommandCounter()
    counted = MongoClient(os.environ["NEARSHOP_MONGODB_URI"], event_listeners=[counter], tz_aware=False)
    monkeypatch.setattr(database_module, "client", counted)
    try:
        db = Database(snap_db.mongo.name)
        queries = [_q("o1", "charger", "en", {1: 3}), _q("o2", "seal tape", "kn", {3: 3})]
        outcomes = search_eval.evaluate(db, queries, counter=counter)
        assert all(o.db_ops is not None and o.db_ops > 0 for o in outcomes)
        summary = search_eval.aggregate(outcomes)
        assert summary["overall"]["db_ops_per_search"] == round(sum(o.db_ops for o in outcomes) / 2, 4)
        assert summary["by_language"]["kn"]["db_ops_per_search"] == outcomes[1].db_ops
        before = counter.count
        db.mongo.client.admin.command("ping")
        assert counter.count == before  # pings are not operations
    finally:
        counted.close()


# ---------------------------------------------------------------- admin_tasks search-index


class FakeIndexApi:
    def __init__(self, existing: list[dict] | None = None):
        self.existing = list(existing or [])
        self.created: list[str] = []
        self.specs: list = []
        self.dropped: list[str] = []

    def list(self) -> list[dict]:
        return list(self.existing)

    def create(self, spec) -> None:
        self.created.append(spec.name)
        self.specs.append(spec)
        self.existing.append({"collection": spec.collection, "name": spec.name, "type": spec.type,
                              "status": "PENDING", "queryable": False})

    def drop(self, collection: str, name: str) -> None:
        self.dropped.append(name)
        self.existing = [ix for ix in self.existing if ix["name"] != name]


OTHER = {"collection": "shops", "name": "someone_elses", "type": "search", "status": "READY", "queryable": True}
ELSEWHERE = {"database": "another_app", "collection": "items", "name": "theirs", "type": "search", "status": "READY",
             "queryable": True}
DAYTIME_UTC = datetime(2026, 10, 22, 6, 30)  # 12:00 IST
NIGHT_UTC = datetime(2026, 10, 22, 16, 30)  # 22:00 IST


def test_create_refuses_beyond_the_tier_limit_and_production_without_the_flag(snap_db, monkeypatch):
    lines: list[str] = []
    out = lines.append
    yes = lambda _prompt: snap_db.mongo.name  # noqa: E731
    filled: list[str] = []

    def fill(db, _out):
        filled.append(db.mongo.name)
        return 48  # the dimensions of the vectors written, whatever the configured model lists

    api = FakeIndexApi([OTHER])
    assert admin_tasks.search_index_create(snap_db, api, "M0", ask=yes, out=out, fill=fill) == 1
    assert api.created == [] and any("above M0's limit of 3" in line for line in lines)
    # Flex allows 10, but NearShop's 3 counts too, over the whole cluster: two indexes elsewhere stop it.
    api = FakeIndexApi([OTHER, ELSEWHERE])
    lines.clear()
    assert admin_tasks.search_index_create(snap_db, api, "Flex", ask=yes, out=out, fill=fill) == 1
    assert api.created == [] and "above NearShop's limit of 3" in lines[-1] and "make 5" in lines[-1]
    # Our own index names in another database of the cluster are someone else's indexes.
    api = FakeIndexApi([{**ELSEWHERE, "name": atlas_search.PRODUCTS_INDEX}])
    assert admin_tasks.search_index_create(snap_db, api, "Flex", ask=yes, out=out, fill=fill) == 1
    assert api.created == [] and "make 4" in lines[-1]
    api = FakeIndexApi([])

    monkeypatch.setattr(settings, "environment", "production")
    lines.clear()
    assert admin_tasks.search_index_create(snap_db, api, "Flex", ask=yes, out=out, fill=fill, now=NIGHT_UTC) == 1
    assert api.created == [] and "add --production" in lines[0]
    assert admin_tasks.search_index_create(snap_db, api, "Flex", production=True, ask=yes, out=out, fill=fill,
                                           now=DAYTIME_UTC) == 1
    assert api.created == [] and "21:00 to 06:00 IST" in lines[-1]
    assert admin_tasks.search_index_create(snap_db, api, "Flex", production=True, ask=lambda _p: "wrong", out=out,
                                           fill=fill, now=NIGHT_UTC) == 1
    assert api.created == []  # the typed confirmation did not match
    assert admin_tasks.search_index_create(snap_db, api, "Flex", production=True, ask=yes, out=out, fill=fill,
                                           now=NIGHT_UTC) == 0
    assert api.created == [spec.name for spec in atlas_search.index_plan()] and filled == [snap_db.mongo.name]
    [vector] = [spec for spec in api.specs if spec.type == "vectorSearch"]
    assert vector.definition["fields"][0]["numDimensions"] == 48  # sized from the vectors written
    active = snap_db.mongo["synonyms"].count_documents({"active": True})
    assert snap_db.mongo[atlas_search.SYNONYM_COLLECTION].count_documents({}) == active > 0

    lines.clear()
    assert admin_tasks.search_index_status(api, "Flex", out=out) == 0
    assert lines[0] == "Search and vector indexes: 3" and "3 of Flex's limit of 10." in lines
    api.existing.append(OTHER)  # an index made outside create
    assert admin_tasks.search_index_status(api, "Flex", out=out) == 1  # 4 is above NearShop's 3
    assert admin_tasks.search_index_status(api, "M0", out=out) == 1  # and over M0's 3
    api.existing.remove(OTHER)

    assert admin_tasks.search_index_drop(snap_db, api, ask=yes, out=out, now=NIGHT_UTC) == 1  # no --production
    assert admin_tasks.search_index_drop(snap_db, api, production=True, ask=yes, out=out, now=NIGHT_UTC) == 0
    assert sorted(api.dropped) == sorted(spec.name for spec in atlas_search.index_plan())
    assert api.existing == []
    assert snap_db.mongo[atlas_search.SYNONYM_COLLECTION].count_documents({}) == 0
    assert admin_tasks.in_production_window(datetime(2026, 10, 22, 0, 29))  # 05:59 IST
    assert not admin_tasks.in_production_window(datetime(2026, 10, 22, 0, 30))  # 06:00 IST


def test_plan_and_status_on_a_mongodb_without_search(snap_db):
    lines: list[str] = []
    api = admin_tasks.SearchIndexApi(snap_db.mongo)
    assert admin_tasks.search_index_plan(api, "M0", out=lines.append) == 0
    assert lines[0].startswith("Index plan (3 indexes")
    assert any("cannot list search indexes" in line for line in lines)
    assert admin_tasks.search_index_status(api, "M0", out=lines.append) == 1
    assert admin_tasks.search_index_create(snap_db, api, "M0", ask=lambda _p: snap_db.mongo.name,
                                           out=lines.append) == 1
    assert lines[-1] == "No index was created."


# ---------------------------------------------------------------- the synonym source


def test_search_synonyms_holds_active_entries_only_and_is_regenerated_on_save(snap_db, monkeypatch):
    source = snap_db.mongo[atlas_search.SYNONYM_COLLECTION]
    source.delete_many({})
    assert synonyms.regenerate_source(snap_db) is None and source.count_documents({}) == 0  # not in use: untouched
    entry = synonyms.create_entry(snap_db, 1, "torch", ["flashlight", "led torch"], ["en"])
    assert source.count_documents({}) == 0

    monkeypatch.setattr(settings, "search_shadow_engines", "atlas_search,in_process")
    monkeypatch.setattr(settings, "search_shadow_share", 0.1)
    other = synonyms.create_entry(snap_db, 1, "headlamp", ["head torch"])
    active = list(snap_db.mongo["synonyms"].find({"active": True}))
    docs = list(source.find().sort("_id", 1))
    assert len(docs) == len(active) and {d["_id"] for d in docs} == {a["_id"] for a in active}
    torch = source.find_one({"_id": entry["id"]})
    assert torch == {"_id": entry["id"], "mappingType": "explicit", "input": ["torch"],
                     "synonyms": ["torch", "flashlight", "led torch"]}
    assert all(d["mappingType"] == "explicit" and d["synonyms"][0] == d["input"][0] for d in docs)

    monkeypatch.setattr(settings, "search_shadow_engines", "")  # no Atlas engine, but the source holds mappings
    synonyms.update_entry(snap_db, 1, other["id"], {"active": False})
    assert source.find_one({"_id": other["id"]}) is None  # inactive entries are left out
    synonyms.update_entry(snap_db, 1, entry["id"], {"expansions": ["flashlight"]})
    assert source.find_one({"_id": entry["id"]})["synonyms"] == ["torch", "flashlight"]

    monkeypatch.setattr(atlas_search, "MAX_SYNONYM_DOCUMENTS", 3)
    with pytest.raises(synonyms.SynonymInvalid, match="at most 3"):
        synonyms.source_documents(snap_db.mongo)
    snap_db.mongo["synonyms"].delete_many({"_id": {"$in": [entry["id"], other["id"]]}})
    source.delete_many({})


# ---------------------------------------------------------------- listing_vectors


def _bump(db: Database) -> None:
    cache_version.bump(db.mongo, cache_version.LISTINGS)
    cache_version.watcher.forget(db.mongo.name)


def test_listing_vectors_follow_listing_changes_and_reuse_cached_vectors(snap_db, provider, monkeypatch):
    coll = snap_db.mongo[atlas_search.VECTOR_COLLECTION]
    coll.delete_many({})
    monkeypatch.setattr(atlas_search, "runner", FakeAtlas({}))
    sync = semantic_index.ListingVectorSync(background=False)
    monkeypatch.setattr(semantic_index, "listing_sync", sync)
    monkeypatch.setattr(search_module, "listing_sync", sync)
    p = SearchParams(q="charger", lat=CENTER[0], lng=CENTER[1], radius_km=5)
    semantic_index.index.mark_dirty()
    run_search(snap_db, p, log=False, now=NOW)  # default engines: the matrix is built and nothing is written there
    assert coll.count_documents({}) == 0 and not sync.ready

    monkeypatch.setattr(settings, "search_vector_engine", "atlas_vector")
    semantic_index.index.mark_dirty()
    builds = semantic_index.index.full_builds
    run_search(snap_db, p, log=False, now=NOW)  # the atlas_vector stage brings listing_vectors in step first
    assert semantic_index.index.full_builds == builds  # and the in-process matrix is not built for it
    active = snap_db.products.find_raw({"is_active": True}, sort=[("_id", 1)])
    docs = {d["_id"]: d for d in coll.find()}
    assert sorted(docs) == [r["_id"] for r in active]
    texts = [semantic_index._row_text(r) for r in active]
    cached = semantic_index.vectors(snap_db, provider, texts)
    for row, vec in zip(active, cached, strict=True):
        doc = docs[row["_id"]]
        assert doc["shop_id"] == row["shop_id"] and doc["is_active"] is True and doc["model"] == provider.name
        assert doc["key"] == semantic_index.cache_key(provider.name, semantic_index._row_text(row))
        assert np.allclose(np.array(doc["vector"].as_vector().data, dtype=np.float32), vec)

    provider.embedded.clear()
    stamps = {i: d["updated_at"] for i, d in docs.items()}
    gone, priced = active[0], active[1]
    snap_db.products.raw.update_one({"_id": gone["_id"]}, {"$set": {"is_active": False, "updated_at": utcnow()}})
    snap_db.products.raw.update_one({"_id": priced["_id"]}, {"$set": {"price": 1.0, "updated_at": utcnow()}})
    copy = {k: v for k, v in priced.items() if k not in ("_id", "local_terms")}
    new_id = snap_db.next_id("products")
    third_shop = snap_db.shops.find_one({"slug": "engines-shop-2"}).id
    snap_db.products.raw.insert_one({**copy, "_id": new_id, "shop_id": third_shop, "catalog_item_id": None,
                                     "updated_at": utcnow()})
    _bump(snap_db)
    run_search(snap_db, p, log=False, now=NOW)  # a later search: an incremental sync, with no matrix
    assert sync.refreshes == 1 and semantic_index.index.full_builds == builds
    after = {d["_id"]: d for d in coll.find()}
    assert gone["_id"] not in after and new_id in after and after[new_id]["shop_id"] == third_shop
    assert after[priced["_id"]]["updated_at"] == stamps[priced["_id"]]  # same text and shop: not rewritten
    assert provider.embedded == []  # the new listing's text was cached: nothing was embedded

    # The nightly full pass catches a change made without bumping the version.
    snap_db.products.raw.update_one({"_id": new_id}, {"$set": {"is_active": False}})
    sync._state = replace(sync._state, full_at=datetime(2000, 1, 1))
    sync.ensure(snap_db)
    assert coll.find_one({"_id": new_id}) is None and sync.full_syncs == 2

    snap_db.products.raw.update_one({"_id": gone["_id"]}, {"$set": {"is_active": True, "updated_at": utcnow()}})
    snap_db.products.raw.update_one({"_id": priced["_id"]}, {"$set": {"price": priced["price"], "updated_at": utcnow()}})
    snap_db.products.raw.delete_one({"_id": new_id})
    coll.delete_many({})
    semantic_index.index.mark_dirty()


def test_warm_up_builds_only_what_the_engines_use(snap_db, provider, monkeypatch):
    coll = snap_db.mongo[atlas_search.VECTOR_COLLECTION]
    coll.delete_many({})
    sync = semantic_index.ListingVectorSync(background=False)
    fresh = semantic_index.SemanticIndex(background=False)
    monkeypatch.setattr(semantic_index, "listing_sync", sync)
    monkeypatch.setattr(semantic_index, "index", fresh)
    monkeypatch.setattr(settings, "search_vector_engine", "atlas_vector")
    semantic_index.warm_up(snap_db)
    assert fresh.full_builds == 0 and fresh.built_at == 0  # an API process on atlas_vector holds no matrix
    assert sync.ready and coll.count_documents({}) == snap_db.products.count({"is_active": True})
    assert semantic_index.vector_stage_ready()

    monkeypatch.setattr(settings, "search_vector_engine", "in_process")
    assert not semantic_index.vector_stage_ready()
    semantic_index.warm_up(snap_db)
    assert fresh.full_builds == 1 and semantic_index.vector_stage_ready()
    coll.delete_many({})


def test_the_vectors_of_a_fallback_model_never_reach_listing_vectors(snap_db, provider, monkeypatch):
    coll = snap_db.mongo[atlas_search.VECTOR_COLLECTION]
    coll.delete_many({})
    monkeypatch.setattr(settings, "embedding_provider", "fastembed")  # the configured model "failed to load"
    assert semantic_index.is_fallback(provider)
    sync = semantic_index.ListingVectorSync(background=False)
    sync.ensure(snap_db)
    assert sync.ready and coll.count_documents({}) == 0
    lines: list[str] = []
    assert admin_tasks.fill_listing_vectors(snap_db, lines.append) is None
    assert coll.count_documents({}) == 0 and "did not load" in lines[-1]
    api = FakeIndexApi()
    assert admin_tasks.search_index_create(snap_db, api, "M0", ask=lambda _p: snap_db.mongo.name,
                                           out=lines.append) == 1
    assert api.created == [] and lines[-1] == "No index was created."
    monkeypatch.setattr(settings, "embedding_provider", "tfidf")
    assert not semantic_index.is_fallback(provider)
    assert admin_tasks.fill_listing_vectors(snap_db, lines.append) == provider.dim
    coll.delete_many({})
    snap_db.mongo[atlas_search.SYNONYM_COLLECTION].delete_many({})


# ---------------------------------------------------------------- the scale option


def test_the_scale_option_follows_the_seed_guard(snap_db, monkeypatch):
    have = snap_db.products.count({"is_active": True})
    shops = snap_db.shops.count()
    monkeypatch.setattr(settings, "environment", "staging")
    with pytest.raises(ValueError, match="refused"):
        search_eval.scale_listings(snap_db, have + 5)
    assert snap_db.products.count({"is_active": True}) == have
    monkeypatch.setattr(settings, "environment", "test")
    assert search_eval.scale_listings(snap_db, have + 12) == 12
    assert snap_db.products.count({"is_active": True}) == have + 12
    assert search_eval.scale_listings(snap_db, have + 12) == 0  # already that many
    synthetic = snap_db.shops.find_raw({"slug": {"$regex": "-scale-"}})
    assert len(synthetic) == snap_db.shops.count() - shops >= 2
    for shop in synthetic:
        assert "synthetic" in shop["name"]
        assert snap_db.products.count({"shop_id": shop["_id"]}) >= 1


# ---------------------------------------------------------------- a real search process (Atlas local deployment)

ATLAS_URI_VAR = "NEARSHOP_TEST_ATLAS_URI"


@pytest.mark.skipif(not os.environ.get(ATLAS_URI_VAR), reason=f"{ATLAS_URI_VAR} names no Atlas local deployment")
def test_the_indexes_and_queries_on_an_atlas_local_deployment():
    """With MongoDB's Atlas local deployment (a Docker image on localhost) named in NEARSHOP_TEST_ATLAS_URI: build the
    three indexes in a throwaway database and query each."""
    uri = os.environ[ATLAS_URI_VAR]
    assert schema.uri_hosts(uri) and all(h in schema.LOCAL_HOSTS for h in schema.uri_hosts(uri)), "local only"
    client = MongoClient(uri, tz_aware=False)
    name = f"nearshop_atlas_test_{os.getpid()}"
    mongo = client[name]
    try:
        mongo["search_synonyms"].insert_one({"_id": 1, "mappingType": "explicit", "input": ["torch"],
                                             "synonyms": ["torch", "flashlight"]})
        mongo["products"].insert_many([
            {"_id": 1, "shop_id": 1, "is_active": True, "name": "Philips LED Flashlight", "brand": "Philips",
             "keywords": "torch light", "description": ""},
            {"_id": 2, "shop_id": 2, "is_active": True, "name": "Eveready Flashlight", "brand": "Eveready",
             "keywords": "", "description": ""},
        ])
        mongo["catalog_items"].insert_one({"_id": 1, "name": "Philips LED Flashlight", "brand": "Philips",
                                           "category_id": 1})
        vec = [1.0] + [0.0] * 383
        mongo["listing_vectors"].insert_one({"_id": 1, "product_id": 1, "shop_id": 1, "is_active": True,
                                             "model": "m", "key": "k", "vector": vec})
        api = admin_tasks.SearchIndexApi(mongo)
        for spec in atlas_search.index_plan(dimensions=384):
            api.create(spec)
        deadline = time.time() + 180
        while not all(ix["queryable"] for ix in api.list()) or len(api.list()) < 3:
            assert time.time() < deadline, "indexes not queryable within 3 minutes"
            time.sleep(2)
        db = Database(name)
        db.mongo = mongo
        hits = atlas_search.runner(mongo["products"], atlas_search.keyword_pipeline(["flashlite"], [], [1]))
        assert [h["_id"] for h in hits] == [1]  # one typo, and only shop 1
        hits = atlas_search.runner(mongo["products"], atlas_search.keyword_pipeline(["torch"], [], [1, 2]))
        assert 1 in [h["_id"] for h in hits]
        assert atlas_search.runner(mongo["catalog_items"], atlas_search.autocomplete_pipeline("phil", 8))
        rows = atlas_search.runner(mongo["listing_vectors"], atlas_search.vector_pipeline(vec, [1], "m", 5))
        assert rows and rows[0]["_id"] == 1 and rows[0]["score"] == pytest.approx(1.0)
    finally:
        client.drop_database(name)
        client.close()
