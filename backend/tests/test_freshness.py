"""Stock freshness rules (P7.4): bands, labels and ranking multipliers, the in-stock-only filter, and the admin
freshness report's medians."""
import os
from datetime import datetime, timedelta

import pytest

from app.core.database import Database
from app.core.database import client as mongo_client
from app.models import Product, Shop
from app.schemas.serializers import FRESHNESS_MULTIPLIERS, STALE_LABEL, freshness
from app.services import analytics, search
from app.services.search import SearchParams, run_search
from database import schema
from tests.conftest import CENTER, HEADERS

T0 = datetime(2027, 3, 1, 12, 0, 0)


# ---------------------------------------------------------------- bands, labels and multipliers

@pytest.mark.parametrize(("age", "band", "label", "multiplier"), [
    (timedelta(0), "fresh", None, 1.0),
    (timedelta(hours=47, minutes=59), "fresh", None, 1.0),
    (timedelta(hours=48), "fresh", None, 1.0),
    (timedelta(hours=48, seconds=1), "aging", "updated 2 days ago", 0.85),
    (timedelta(days=3, hours=5), "aging", "updated 3 days ago", 0.85),
    (timedelta(days=7), "aging", "updated 7 days ago", 0.85),
    (timedelta(days=7, seconds=1), "stale", STALE_LABEL, 0.6),
    (timedelta(days=40), "stale", "stock not confirmed recently", 0.6),
])
def test_each_band_has_its_label_and_multiplier(age, band, label, multiplier):
    assert freshness(T0 - age, T0) == {"band": band, "label": label, "multiplier": multiplier}


def test_a_listing_never_confirmed_counts_as_stale():
    assert freshness(None, T0)["band"] == "stale"
    assert FRESHNESS_MULTIPLIERS == {"fresh": 1.0, "aging": 0.85, "stale": 0.6}


@pytest.fixture
def clock():
    """Fix search's freshness clock for one test; the current time again afterwards."""
    yield search.set_clock
    search.set_clock(None)


def _search(db, **kw):
    return run_search(db, SearchParams(lat=CENTER[0], lng=CENTER[1], radius_km=30, **kw), log=False)


def _group(res, product_id):
    return next(g for g in res["groups"] if any(o["id"] == product_id for o in g["offers"]))


def test_group_score_takes_the_band_multiplier_and_the_card_gets_the_label(db, ids, clock):
    confirmed = db.products.get(ids["charger"]).stock_updated_at
    scores = {}
    for band, age in [("fresh", timedelta(hours=1)), ("aging", timedelta(days=3)), ("stale", timedelta(days=8))]:
        clock(confirmed + age)
        group = _group(_search(db), ids["charger"])
        scores[band] = group["score"]
        assert group["best_offer"]["freshness"]["band"] == band
    assert group["best_offer"]["freshness"]["label"] == STALE_LABEL
    assert scores["aging"] == pytest.approx(scores["fresh"] * 0.85, abs=1e-3)
    assert scores["stale"] == pytest.approx(scores["fresh"] * 0.6, abs=1e-3)


def test_a_group_ranks_by_its_freshest_offer(db, ids, clock):
    tape, far = db.products.get(ids["tape"]), db.products.get(ids["tape_far"])
    try:
        db.products.update_one({"_id": far.id}, {"$set": {"stock_updated_at": tape.stock_updated_at - timedelta(days=9)}})
        clock(tape.stock_updated_at + timedelta(hours=2))
        both = _group(_search(db), ids["tape"])["score"]
        db.products.update_one({"_id": tape.id}, {"$set": {"stock_updated_at": tape.stock_updated_at - timedelta(days=9)}})
        stale = _group(_search(db), ids["tape"])["score"]
    finally:
        for p in (tape, far):
            db.products.update_one({"_id": p.id}, {"$set": {"stock_updated_at": p.stock_updated_at}})
    assert stale == pytest.approx(both * 0.6, abs=1e-3)


# ---------------------------------------------------------------- the in-stock-only filter

def test_in_stock_only_leaves_out_listings_beyond_seven_days(db, ids, clock):
    newest = max(db.products.get(i).stock_updated_at for i in (ids["charger"], ids["tape"], ids["tape_far"]))
    clock(newest + timedelta(days=3))
    aging = {o["id"] for g in _search(db, in_stock_only=True)["groups"] for o in g["offers"]}
    assert {ids["charger"], ids["tape"], ids["tape_far"]} <= aging
    clock(newest + timedelta(days=7))  # exactly 7 days: still in
    assert _search(db, in_stock_only=True)["total"] == len(_search(db, in_stock_only=False)["groups"])
    clock(newest + timedelta(days=7, minutes=1))
    assert _search(db, in_stock_only=True)["total"] == 0
    # Without the filter they are still found, labelled and ranked lower.
    res = _search(db)
    assert res["total"] > 0
    assert all(g["best_offer"]["freshness"]["band"] == "stale" for g in res["groups"])


def test_in_stock_only_keeps_a_stale_groups_fresh_offer(db, ids, clock):
    tape, far = db.products.get(ids["tape"]), db.products.get(ids["tape_far"])
    try:
        db.products.update_one({"_id": tape.id}, {"$set": {"stock_updated_at": far.stock_updated_at - timedelta(days=10)}})
        clock(far.stock_updated_at + timedelta(hours=1))
        offers = {o["id"] for g in _search(db, in_stock_only=True)["groups"] for o in g["offers"]}
    finally:
        db.products.update_one({"_id": tape.id}, {"$set": {"stock_updated_at": tape.stock_updated_at}})
    assert ids["tape_far"] in offers and ids["tape"] not in offers


# ---------------------------------------------------------------- medians and the admin report

@pytest.mark.parametrize(("values", "expected"), [
    ([], None), ([5.0], 5.0), ([3.0, 1.0, 2.0], 2.0), ([4.0, 1.0, 3.0, 2.0], 2.5), ([10.0, 10.0], 10.0),
])
def test_median(values, expected):
    assert analytics.median(values) == expected


@pytest.fixture
def fresh_db(ids):
    name = os.environ["NEARSHOP_MONGODB_DB"] + "_fresh"
    db = Database(name)
    schema.reset(db.mongo, confirm=name)
    yield db
    mongo_client.drop_database(name)


def _shop(db, n, active=True, verified=True):
    return db.shops.insert(Shop(owner_id=100 + n, name=f"Shop {n}", slug=f"shop-{n}", address_line="a", locality="l",
                                city="Ballari", lat=15.14, lng=76.92, is_active=active, is_verified=verified))


def _listings(db, shop, ages_hours, active=True):
    db.products.insert_many([
        Product(shop_id=shop.id, category_id=1, name=f"Item {shop.id}-{i}", price=10, quantity=1, is_active=active,
                stock_updated_at=T0 - timedelta(hours=h)) for i, h in enumerate(ages_hours)])


def test_freshness_report_medians(fresh_db):
    a, b, c = _shop(fresh_db, 1), _shop(fresh_db, 2, verified=False), _shop(fresh_db, 3)
    closed = _shop(fresh_db, 4, active=False)
    empty = _shop(fresh_db, 5)
    _listings(fresh_db, a, [1, 10, 30, 100])          # median 20 h, 3 of 4 within 48 h
    _listings(fresh_db, a, [5000], active=False)      # inactive listings do not count
    _listings(fresh_db, b, [200, 300, 400])           # median 300 h: beyond 7 days (168 h)
    _listings(fresh_db, c, [48, 49])                  # median 48.5 h, 1 of 2 within 48 h
    _listings(fresh_db, closed, [9000])               # a closed shop does not count

    r = analytics.freshness_report(fresh_db, now=T0)
    by_id = {s["id"]: s for s in r["shops"]}
    assert set(by_id) == {a.id, b.id, c.id, empty.id}
    assert (by_id[a.id]["median_age_hours"], by_id[a.id]["fresh_share"], by_id[a.id]["listings"]) == (20.0, 0.75, 4)
    assert (by_id[b.id]["median_age_hours"], by_id[b.id]["fresh_share"]) == (300.0, 0.0)
    assert (by_id[c.id]["median_age_hours"], by_id[c.id]["fresh_share"]) == (48.5, 0.5)
    assert (by_id[empty.id]["median_age_hours"], by_id[empty.id]["fresh_share"]) == (None, None)
    assert [s["id"] for s in r["stale_shops"]] == [b.id]
    assert [s["id"] for s in r["shops"]] == [b.id, c.id, a.id, empty.id]  # stalest first, no listings last
    # City median over every active listing in a live shop: 1, 10, 30, 48, 49, 100, 200, 300, 400 -> 49.
    assert r["city_median_age_hours"] == 49.0
    assert r["city_fresh_share"] == round(4 / 9, 3)
    assert (r["live_shops"], r["verified_live_shops"], r["listings"]) == (4, 3, 9)


def test_freshness_report_with_no_listings(fresh_db):
    _shop(fresh_db, 1)
    r = analytics.freshness_report(fresh_db, now=T0)
    assert r["city_median_age_hours"] is None and r["city_fresh_share"] is None and r["stale_shops"] == []


def test_admin_freshness_endpoint_shows_the_city_median(admin, customer):
    res = admin.get("/api/admin/freshness", headers=HEADERS)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["city_median_age_hours"] is not None and body["live_shops"] >= 2
    assert {"median_age_hours", "fresh_share"} <= set(body["shops"][0])
    assert customer.get("/api/admin/freshness", headers=HEADERS).status_code == 403
