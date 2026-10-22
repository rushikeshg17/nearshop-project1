"""Search cost at city scale (P8.2; app/services/search.py, docs/perf/baseline.md): the radius cap, the stages
limited to the shops in radius, the narrow listing fetch, the cap on listings read in full, and the city-scale
load-test data generator (backend/loadtest/locustfile.py generate-city). Search must rank exactly as before: the
top 20 results equal those of the uncapped version, a copy of run_search as it was before P8.2."""
from __future__ import annotations

import os
import random
import subprocess
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.ai.semantic_index import index
from app.core.config import settings
from app.core.database import Collection, Database
from app.models import CatalogItem, Category, Product, Role, Shop, User
from app.schemas.serializers import STALE_DAYS, freshness, listing, shop_brief
from app.services import search as search_module
from app.services.loaders import category_by_slug, with_category
from app.services.search import (
    MAX_RADIUS_KM,
    SearchParams,
    keyword_scores,
    local_term_scores,
    nearby_shops,
    run_search,
)
from app.utils.text import expand_synonyms, normalize_query
from database import schema
from tests.conftest import verified_shop_fields

NOW = datetime(2026, 10, 20, 6, 0, 0)
CENTER = (15.1394, 76.9214)
KM_PER_DEG_LAT = 111.2
NEAR_SHOPS = 110  # 110 shops x 20 listings: more listings in radius than the prompt's 2,000-candidate figure
SPEC_CANDIDATES = 2000  # P8.2 item 5's figure; search.py's docstring records why there is no cut before grouping
FAR_KM = (27.0, 30.0, 34.0)  # beyond the 25 km cap, inside the 50 km the route still accepts
BACKEND = Path(__file__).resolve().parents[1]

WORDS = [("Samsung", "Fast Charger 25W", "phone charger adapter"), ("boAt", "Type-C Charging Cable", "usb cable data"),
         ("Astral", "PTFE Thread Seal Tape", "teflon tape leak"), ("Taparia", "Pipe Wrench 14 inch", "pipe spanner"),
         ("Havells", "LED Bulb 9W", "light bulb led"), ("Anchor", "Switch Board 6A", "switch socket"),
         ("Supreme", "PVC Pipe 1 inch", "pipe plumbing"), ("Finolex", "Copper Wire 1.5 sq mm", "wire cable"),
         ("Bata", "Sports Shoes", "shoes running"), ("Classmate", "Notebook 200 pages", "notebook pen"),
         ("Cello", "Ball Pen Blue", "pen ballpoint"), ("Mi", "Power Bank 10000mAh", "power bank charger")]


def _item_name(n: int) -> tuple[str, str, str]:
    brand, name, tags = WORDS[n % len(WORDS)]
    return brand, f"{brand} {name} v{n // len(WORDS) + 1}", tags


@pytest.fixture(scope="module")
def city(ids):
    """Its own database: 110 shops within 10 km of the centre, each listing 20 items (catalogue and its own), and
    3 shops beyond 25 km, with mixed stock, prices, freshness, photos and delivery."""
    name = f"{settings.mongodb_db}_scale"
    db = Database(name)
    schema.reset(db.mongo, confirm=name)
    rng = random.Random(8002)
    cats = db.categories.insert_many([Category(slug=s, name=n, icon="Package", sort_order=i) for i, (s, n) in
                                      enumerate([("electronics", "Electronics"), ("hardware", "Hardware"),
                                                 ("daily", "Daily needs")])])
    items = db.catalog_items.insert_many([
        CatalogItem(slug=f"item-{n}", name=_item_name(n)[1], brand=_item_name(n)[0], category_id=cats[n % 3].id,
                    tags=_item_name(n)[2].split(), icon="Package") for n in range(36)])
    db.catalog_items.raw.update_one({"_id": items[0].id}, {"$set": {"local_terms": ["ಚಾರ್ಜರ್"]}})
    owner = db.users.insert(User(name="Scale Owner", email="scale-owner@example.com", password_hash="x",
                                 role=Role.OWNER))
    places = [(rng.uniform(-9, 9), rng.uniform(-9, 9)) for _ in range(NEAR_SHOPS)] + [(km, 0.0) for km in FAR_KM]
    shops = db.shops.insert_many([
        Shop(owner_id=owner.id, name=f"Scale Shop {n}", slug=f"scale-shop-{n}", address_line="Road", city="Ballari",
             lat=CENTER[0] + dy / KM_PER_DEG_LAT, lng=CENTER[1] + dx / KM_PER_DEG_LAT, offers_delivery=n % 3 == 0,
             delivery_radius_km=6, offers_pickup=n % 7 != 0, category_ids=[c.id for c in cats],
             **verified_shop_fields(n))
        for n, (dy, dx) in enumerate(places)])
    rows: list[Product] = []
    for n, shop in enumerate(shops):
        for item in rng.sample(items, 18):
            rows.append(Product(
                shop_id=shop.id, catalog_item_id=item.id, category_id=item.category_id, name=item.name,
                brand=item.brand, keywords=" ".join(item.tags), icon="Package", price=rng.choice([40, 99, 199, 450]),
                mrp=499, quantity=rng.choice([0, 0, 2, 5, 9]), low_stock_threshold=2,
                stock_updated_at=NOW - timedelta(hours=rng.choice([1, 30, 24 * 5, 24 * 30])),
                description="A long description search never shows.", specs={"weight": "1 kg"},
                image_path=f"products/{n}-{item.id}.jpg" if rng.random() < 0.3 else None,
                local_terms=["ಚಾರ್ಜರ್"] if item.id == items[0].id else []))
        for k in range(2):  # the shop's own items, without a catalogue item
            rows.append(Product(shop_id=shop.id, category_id=cats[k].id, name=f"Shop {n} special charger cable {k}",
                                keywords="charger cable", price=150 + k, quantity=k * 3,
                                stock_updated_at=NOW - timedelta(days=k * 3)))
    db.products.insert_many(rows)
    db.products.raw.update_many({"catalog_item_id": None, "shop_id": shops[0].id}, {"$set": {"is_active": False}})
    index.mark_dirty()
    yield db
    index.mark_dirty()
    db.mongo.client.drop_database(name)


# ---------------------------------------------------------------- the uncapped version (run_search before P8.2)


def uncapped(db: Database, p: SearchParams, now: datetime = NOW) -> dict:
    """run_search as it was before P8.2: every listing in radius read whole, the text index, the local-term match and
    the semantic index over every listing in the city, and grouping on whole documents. No radius cap."""
    cfg = settings.ranking
    norm = normalize_query(p.q) if p.q else ""
    expanded = expand_synonyms(norm) if norm else []
    category = category_by_slug(db, p.category)
    shops = nearby_shops(db, p.lat, p.lng, p.radius_km, p.fulfillment, p.shop_id)
    query: dict = {"is_active": True, "shop_id": {"$in": list(shops)}}
    if p.category:
        query["category_id"] = category.id if category else -1
    if p.min_price is not None or p.max_price is not None:
        query["price"] = {**({"$gte": p.min_price} if p.min_price is not None else {}),
                          **({"$lte": p.max_price} if p.max_price is not None else {})}
    if p.in_stock_only:
        query["quantity"] = {"$gt": 0}
        query["stock_updated_at"] = {"$gte": now - timedelta(days=STALE_DAYS)}
    products = with_category(db, db.products.find(query)) if shops else []
    kw = dict(keyword_scores(db, norm.split() + expanded)) if norm else {}
    for pid, bonus in (local_term_scores(db, norm, 1.0) if norm else {}).items():
        kw[pid] = kw.get(pid, 0.0) + cfg.w_local_term * round(bonus)
    sem = index.query(db, norm) if norm else {}
    relevance: dict[int, float] = {}
    if norm:
        kw_max = max((kw.get(pr.id, 0.0) for pr in products), default=0.0) or 1.0
        for pr in products:
            k, s = kw.get(pr.id, 0.0) / kw_max, sem.get(pr.id, 0.0)
            if k <= 0 and s < cfg.sem_min_match:
                continue
            relevance[pr.id] = cfg.w_semantic * search_module._norm_sem(s, cfg) + cfg.w_keyword * k
        if relevance:
            best = max(relevance.values())
            relevance = {k: v for k, v in relevance.items() if v >= best * cfg.relative_cutoff}
        products = [pr for pr in products if pr.id in relevance]
    else:
        relevance = {pr.id: 1.0 for pr in products}

    def shop_json(sid: int) -> dict:
        s, d = shops[sid]
        return shop_brief(s, d)

    groups: dict[str, list[Product]] = defaultdict(list)
    for pr in products:
        groups[f"c{pr.catalog_item_id}" if pr.catalog_item_id else f"p{pr.id}"].append(pr)
    rows, category_counts = [], defaultdict(int)
    for key, items in groups.items():
        in_stock = [x for x in items if x.quantity > 0]
        pool = in_stock or items
        nearest = min(shops[x.shop_id][1] for x in pool)
        rel = max(relevance[x.id] for x in items)
        proximity = max(0.0, 1 - nearest / max(p.radius_km, 0.5))
        score = cfg.w_relevance * rel + cfg.w_proximity * proximity + (cfg.w_in_stock if in_stock else 0.0)
        if not in_stock:
            score *= cfg.out_of_stock_factor
        score *= max(freshness(x.stock_updated_at, now)["multiplier"] for x in pool)
        prices = [x.price for x in pool]
        lo, hi = min(prices), max(prices)
        best = min(pool, key=lambda x: ((x.price - lo) / (hi - lo) if hi > lo else 0)
                   + shops[x.shop_id][1] / max(p.radius_km, 1))
        offers = sorted(items, key=lambda x: (x.quantity <= 0, shops[x.shop_id][1]))
        category_counts[best.category.slug] += 1
        pictured = next((listing(x, now=now) for x in items if x.image_path), None)
        rows.append({
            "key": key, "catalog_item_id": best.catalog_item_id, "name": best.name, "brand": best.brand,
            "unit": best.unit, "icon": best.icon, "image_url": pictured["image_url"] if pictured else None,
            "thumbnail_url": pictured["thumbnail_url"] if pictured else None, "category": best.category.slug,
            "min_price": min(prices), "max_price": max(prices), "mrp": best.mrp,
            "nearest_km": round(nearest, 2), "shop_count": len(items), "in_stock_count": len(in_stock),
            "relevance": round(rel, 3), "score": round(score, 4),
            "best_offer": listing(best, shop_json(best.shop_id), now),
            "offers": [listing(x, shop_json(x.shop_id), now) for x in offers[:6]]})
    sorters = {"relevance": lambda g: -g["score"], "distance": lambda g: (g["in_stock_count"] == 0, g["nearest_km"]),
               "price_asc": lambda g: (g["in_stock_count"] == 0, g["min_price"]),
               "price_desc": lambda g: (g["in_stock_count"] == 0, -g["min_price"])}
    if not norm and p.sort == "relevance":
        rows.sort(key=lambda g: (g["in_stock_count"] == 0, g["nearest_km"], -g["shop_count"]))
    else:
        rows.sort(key=sorters[p.sort])
    pins: dict[int, dict] = {}
    for pr in products:
        pin = pins.setdefault(pr.shop_id, {**shop_json(pr.shop_id), "match_count": 0, "in_stock_count": 0,
                                           "min_price": None})
        pin["match_count"] += 1
        if pr.quantity > 0:
            pin["in_stock_count"] += 1
            pin["min_price"] = pr.price if pin["min_price"] is None else min(pin["min_price"], pr.price)
    return {"total": len(rows), "groups": rows, "shops": sorted(pins.values(), key=lambda s: s["distance_km"]),
            "category_counts": dict(category_counts), "shops_in_radius": len(shops)}


CASES = [
    ("charger", {}), ("usb cable", {}), ("tape", {}), ("pipe", {}), ("shoes", {}), ("ಚಾರ್ಜರ್", {}),
    ("charger", {"sort": "distance"}), ("charger", {"sort": "price_asc"}), ("pipe", {"sort": "price_desc"}),
    ("charger", {"in_stock_only": True}), ("cable", {"fulfillment": "delivery"}), ("cable", {"fulfillment": "pickup"}),
    ("charger", {"category": "electronics"}), ("pipe", {"min_price": 50, "max_price": 300}),
    ("charger", {"radius_km": 2.0}), ("charger", {"radius_km": 25.0}), ("", {}), ("", {"sort": "price_asc"}),
    ("", {"in_stock_only": True, "radius_km": 4.0}), ("wire", {"page": 2}),
]


@pytest.mark.parametrize(("q", "extra"), CASES, ids=[f"{q or 'browse'}-{sorted(e.items())}" for q, e in CASES])
def test_the_top_20_equal_the_uncapped_version(city, q, extra):
    p = SearchParams(q=q, lat=CENTER[0], lng=CENTER[1], **extra)
    got = run_search(city, p, log=False, now=NOW)
    want = uncapped(city, p)
    start = (p.page - 1) * p.page_size
    assert got["total"] == want["total"]
    assert got["groups"] == want["groups"][start:start + 20]  # every field of every group, offers included
    assert got["shops"] == want["shops"]
    assert got["category_counts"] == want["category_counts"]
    assert got["shops_in_radius"] == want["shops_in_radius"]


def test_the_test_database_holds_more_listings_in_radius_than_the_cap(city):
    shops = nearby_shops(city, CENTER[0], CENTER[1], MAX_RADIUS_KM)
    in_radius = city.products.count({"is_active": True, "shop_id": {"$in": list(shops)}})
    assert in_radius > SPEC_CANDIDATES
    browse = run_search(city, SearchParams(lat=CENTER[0], lng=CENTER[1], radius_km=MAX_RADIUS_KM), log=False, now=NOW)
    assert sum(pin["match_count"] for pin in browse["shops"]) == in_radius  # counted, though not read in full


# ---------------------------------------------------------------- 1. the radius cap


def test_the_radius_is_capped_at_25_km(city, client):
    far = {s.id for s in city.shops.find({"slug": {"$in": ["scale-shop-110", "scale-shop-111", "scale-shop-112"]}})}
    wide = SearchParams(q="charger", lat=CENTER[0], lng=CENTER[1], radius_km=50)
    capped = run_search(city, wide, log=False, now=NOW)
    assert capped == run_search(city, replace(wide, radius_km=25), log=False, now=NOW)
    assert capped["shops_in_radius"] == NEAR_SHOPS
    assert not far & {pin["id"] for pin in capped["shops"]}
    # Without the cap, 50 km would reach the shop 27 km away (and 30 and 34): the cap is what leaves them out.
    assert len(nearby_shops(city, CENTER[0], CENTER[1], 50)) == NEAR_SHOPS + len(FAR_KM)
    # The route still accepts up to 50 km (links from before P8.2), searches 25 and tells the client.
    assert capped["radius_km"] == MAX_RADIUS_KM
    res = client.get("/api/search", params={"q": "charger", "lat": CENTER[0], "lng": CENTER[1], "radius_km": 50})
    assert res.status_code == 200 and res.json()["radius_km"] == MAX_RADIUS_KM
    near = client.get("/api/search", params={"q": "charger", "lat": CENTER[0], "lng": CENTER[1], "radius_km": 3})
    assert near.json()["radius_km"] == 3
    assert client.get("/api/search", params={"q": "x", "page_size": 51}).status_code == 422
    assert client.get("/api/search", params={"q": "x", "radius_km": 51}).status_code == 422


# ---------------------------------------------------------------- 2 and 3. stages limited to the shops in radius


def test_every_stage_scores_only_the_listings_in_radius(city):
    p = SearchParams(q="charger", lat=CENTER[0], lng=CENTER[1], radius_km=3.0)
    norm = normalize_query(p.q)
    fetched = search_module._candidates(city, p, norm, expand_synonyms(norm), NOW)
    in_radius = {r["_id"] for r in city.products.find_raw({"shop_id": {"$in": list(fetched.shops)}}, {"_id": 1})}
    assert 0 < len(fetched.shops) < NEAR_SHOPS
    assert set(fetched.kw) and set(fetched.kw) <= in_radius
    assert set(keyword_scores(city, ["charger"])) - in_radius  # unscoped, the text index scores the whole city
    local = search_module.local_term_scores(city, "ಚಾರ್ಜರ್", 1.0, list(fetched.shops))
    assert set(local) and set(local) <= in_radius
    assert set(search_module.local_term_scores(city, "ಚಾರ್ಜರ್", 1.0)) - in_radius
    assert set(fetched.sem) == {r["_id"] for r in fetched.rows}  # the semantic stage: the fetched listings only
    every = index.query(city, norm)
    assert len(every) > len(fetched.sem)
    assert all(abs(fetched.sem[pid] - every[pid]) < 1e-5 for pid in fetched.sem)  # the same similarities
    assert search_module._candidates(city, replace(p, lat=0.0, lng=0.0), norm, [], NOW).kw == {}  # no shop, no score


# ---------------------------------------------------------------- 4 and 5. narrow reads, at most MAX_FULL_READS in full


@pytest.fixture
def product_reads(monkeypatch):
    """Every products query the Collection helpers send: (filter, projection)."""
    reads: list[tuple[dict, dict | None]] = []
    find, find_raw = Collection.find, Collection.find_raw

    def spy_find(self, filter=None, **kw):
        if self.name == "products":
            reads.append((filter or {}, kw.get("projection")))
        return find(self, filter, **kw)

    def spy_find_raw(self, filter=None, projection=None, **kw):
        if self.name == "products":
            reads.append((filter or {}, projection))
        return find_raw(self, filter, projection, **kw)

    monkeypatch.setattr(Collection, "find", spy_find)
    monkeypatch.setattr(Collection, "find_raw", spy_find_raw)
    return reads


def test_listings_are_read_with_the_fields_ranking_and_display_need(city, product_reads):
    res = run_search(city, SearchParams(q="charger", lat=CENTER[0], lng=CENTER[1], radius_km=25), log=False, now=NOW)
    assert res["total"] > 0
    # The listing reads (the semantic index's own build and the spelling vocabulary read the city by other filters).
    listing_reads = [(f, proj) for f, proj in product_reads
                     if "$text" not in f and "$or" not in f and ("shop_id" in f or "_id" in f)]
    assert [proj for _, proj in listing_reads] == [search_module.RANK_FIELDS, search_module.DISPLAY_FIELDS]
    for _, proj in listing_reads:
        assert not {"description", "specs", "keywords", "local_terms"} & set(proj)
    # The keyword and local-term stages: only for the shops in radius.
    stages = [f for f, _ in product_reads if "$text" in f or "$or" in f]
    assert len(stages) == 2 and all("shop_id" in f for f in stages)
    shown = listing_reads[1][0]["_id"]["$in"]
    offers = {o["id"] for g in res["groups"] for o in [g["best_offer"], *g["offers"]]}
    assert set(shown) == offers and len(shown) <= 20 * 7  # only what the page shows


def test_at_most_max_full_reads_listings_are_read_in_full_and_paging_loses_nothing(city, product_reads):
    assert search_module.MAX_FULL_READS == search_module.MAX_PAGE_SIZE * 7 == 350
    p = SearchParams(lat=CENTER[0], lng=CENTER[1], radius_km=25, page_size=200)
    want = uncapped(city, replace(p, page_size=10_000))["groups"]
    assert len(want) > 2 * search_module.MAX_PAGE_SIZE
    seen: list[dict] = []
    page = 1
    while True:
        product_reads.clear()
        res = run_search(city, replace(p, page=page), log=False, now=NOW)
        [shown] = [f["_id"]["$in"] for f, proj in product_reads if proj == search_module.DISPLAY_FIELDS]
        assert len(shown) <= search_module.MAX_FULL_READS
        assert res["page_size"] == search_module.MAX_PAGE_SIZE  # a larger page is served at the cap, and says so
        assert len(res["groups"]) == min(search_module.MAX_PAGE_SIZE, len(want) - len(seen))
        seen += res["groups"]
        if not res["has_more"]:
            break
        page += 1
    assert seen == want  # every group on exactly one page, in rank order


def test_a_cached_re_ranking_reads_only_the_listings_it_has_not_shown(city, product_reads):
    cache: dict = {}
    p = SearchParams(q="charger", lat=CENTER[0], lng=CENTER[1], radius_km=10)
    first = run_search(city, p, log=False, now=NOW, cache=cache)
    product_reads.clear()
    again = run_search(city, p, log=False, now=NOW, cache=cache)
    assert again == first and product_reads == []
    page2 = run_search(city, replace(p, page=2, page_size=5), log=False, now=NOW, cache=cache)
    assert page2["groups"] == uncapped(city, p)["groups"][5:10]
    assert search_module.fetch_ms(cache) > 0


# ---------------------------------------------------------------- 6. the city-scale load-test data generator


def _generate(db_name: str, *args: str, environment: str = "test") -> subprocess.CompletedProcess:
    env = {**os.environ, "NEARSHOP_ENVIRONMENT": environment, "NEARSHOP_MONGODB_DB": db_name}
    return subprocess.run([sys.executable, "loadtest/locustfile.py", "generate-city", *args], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=180)


def test_the_generator_builds_the_city_shape_only_where_a_seed_may_run(city):
    name = f"{settings.mongodb_db}_city"
    db = Database(name)
    schema.reset(db.mongo, confirm=name)
    try:
        # A small seeded database: 4 of the scale shops and their listings.
        db.categories.raw.insert_many(city.categories.find_raw({}))
        db.catalog_items.raw.insert_many(city.catalog_items.find_raw({}))
        seed_shops = city.shops.find_raw({"slug": {"$in": [f"scale-shop-{n}" for n in range(4)]}})
        db.shops.raw.insert_many(seed_shops)
        db.products.raw.insert_many(city.products.find_raw({"shop_id": {"$in": [s["_id"] for s in seed_shops]}}))
        db.mongo["counters"].insert_many(city.mongo["counters"].find({"_id": {"$in": ["shops", "products"]}}))
        before = (db.shops.count(), db.products.count())
        have = db.products.count({"is_active": True})

        for environment in ("staging", "production"):
            refused = _generate(name, "--confirm-db", name, "--shops", "10", "--listings", "300",
                                environment=environment)
            assert refused.returncode == 2 and "nothing was written" in refused.stderr
        wrong = _generate(name, "--confirm-db", "another", "--shops", "10", "--listings", "300")
        assert wrong.returncode == 2 and "does not match" in wrong.stderr
        assert (db.shops.count(), db.products.count()) == before

        done = _generate(name, "--confirm-db", name, "--shops", "10", "--listings", "300")
        assert done.returncode == 0, done.stderr
        assert db.shops.count({"is_active": True}) == 10
        assert db.products.count({"is_active": True}) == 300
        synthetic = db.shops.find_raw({"slug": {"$regex": "-city-"}})
        assert len(synthetic) == 6
        templates = {s["_id"]: s for s in seed_shops}
        for shop in synthetic:
            assert "synthetic" in shop["name"]
            rows = db.products.find_raw({"shop_id": shop["_id"]})
            assert len(rows) in ((300 - have) // 6, (300 - have) // 6 + 1)  # split evenly
            items = [r["catalog_item_id"] for r in rows if r.get("catalog_item_id") is not None]
            assert len(items) == len(set(items))  # one listing per catalogue item and shop
            lng, lat = shop["location"]["coordinates"]
            assert min(abs(lat - t["location"]["coordinates"][1]) + abs(lng - t["location"]["coordinates"][0])
                       for t in templates.values()) <= 0.04  # within about 2 km of a seeded shop
        again = _generate(name, "--confirm-db", name, "--shops", "10", "--listings", "300")
        assert again.returncode == 0 and "0 shops and 0 listings added" in again.stdout
        # Search on the generated data ranks as the uncapped version does.
        p = SearchParams(q="charger", lat=CENTER[0], lng=CENTER[1], radius_km=25)
        assert run_search(db, p, log=False, now=NOW)["groups"] == uncapped(db, p)["groups"][:20]
    finally:
        db.mongo.client.drop_database(name)
        index.mark_dirty()
