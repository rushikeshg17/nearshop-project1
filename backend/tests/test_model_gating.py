"""Model gating by real-data thresholds (P7.3, plan section 8.9): each threshold just below and at its boundary,
is_demo sales left out in production mode, the reason and progress in the owner insights payload when a model is
gated, and the 5-user rule for nearby search insights."""
import uuid
from datetime import timedelta

import numpy as np
import pytest

from app.ai import anomaly, demand, recommend
from app.core.config import Settings, settings
from app.core.database import utcnow
from app.models import (
    AnomalyStatus,
    CatalogItem,
    DemandForecast,
    ModelRun,
    PriceAnomaly,
    Product,
    SaleSource,
    SalesRecord,
    SearchEvent,
    Shop,
)
from app.services import analytics
from tests.conftest import CENTER, HEADERS

GATED = ("demand", "recommendations", "anomalies")


@pytest.fixture
def clean(db):
    """Removes what a test adds: its sales, searches, shops, listings, catalog items, flags and model runs."""
    made: dict[str, list] = {"shops": [], "products": [], "catalog_items": []}
    tag = f"gate-{uuid.uuid4().hex[:8]}"
    yield made, tag
    db.sales_history.delete_many({"basket_id": {"$regex": f"^{tag}"}})
    db.search_history.delete_many({"query": {"$regex": f"^{tag}"}})
    db.price_anomalies.delete_many({"review_note": tag})
    db.price_anomalies.delete_many({"product_id": {"$in": made["products"]}})
    db.demand_forecasts.delete_many({})
    db.recommendations.delete_many({})
    db.model_runs.delete_many({"name": {"$in": list(GATED)}})
    for name, ids_ in made.items():
        getattr(db, name).delete_many({"_id": {"$in": ids_}})


def own_listing(db, made: dict, tag: str, shop_id: int | None = None) -> tuple[int, int]:
    """A listing of the test's own (in a new shop unless shop_id is given), so sales other tests record on the
    shared listings never reach its figures: (shop id, product id)."""
    if shop_id is None:
        shop = db.shops.insert(Shop(owner_id=1, name=tag, slug=tag, address_line="x", city="Mysuru", lat=CENTER[0],
                                    lng=CENTER[1]))
        made["shops"].append(shop.id)
        shop_id = shop.id
    category = db.categories.find_one().id
    product = db.products.insert(Product(shop_id=shop_id, category_id=category, name=f"{tag} item", price=10,
                                         quantity=5))
    made["products"].append(product.id)
    return shop_id, product.id


def sale(tag: str, shop_id: int, product_id: int, days_ago: int, qty: int = 1, *, demo: bool = False,
         basket: str | None = None, catalog_item_id: int | None = None) -> SalesRecord:
    return SalesRecord(shop_id=shop_id, product_id=product_id, catalog_item_id=catalog_item_id,
                       basket_id=f"{tag}-{basket or uuid.uuid4().hex[:10]}", quantity=qty, unit_price=10,
                       source=SaleSource.DEMO if demo else SaleSource.ORDER, is_demo=demo,
                       sold_at=utcnow() - timedelta(days=days_ago, hours=1))


# ------------------------------------------------------------------ thresholds, at and just below the boundary

@pytest.mark.parametrize(("history", "sale_days", "units", "eligible"), [
    (56, 20, 30, True),
    (55, 20, 30, False),
    (56, 19, 30, False),
    (56, 20, 29, False),
])
def test_demand_listing_eligibility_boundaries(history, sale_days, units, eligible):
    assert analytics.demand_listing_eligible(history, sale_days, units) is eligible


def test_demand_listing_eligibility_from_a_daily_series():
    series = np.zeros(150)
    series[-56] = 11  # first sale exactly 56 days ago, counting today
    series[-19:] = 1  # 19 more days with a sale: 20 in all, 30 units
    assert demand.listing_eligible(series)
    shorter = series.copy()
    shorter[-56], shorter[-55] = 0, 11  # first sale 55 days ago
    assert not demand.listing_eligible(shorter)
    fewer_days = series.copy()
    fewer_days[-1], fewer_days[-2] = 0, 2  # 19 days with a sale, still 30 units
    assert not demand.listing_eligible(fewer_days)
    fewer_units = series.copy()
    fewer_units[-56] = 10  # 29 units
    assert not demand.listing_eligible(fewer_units)
    assert not demand.listing_eligible(np.zeros(150))


def test_demand_model_gate_needs_ten_percent_better_than_last_week():
    at = analytics.demand_model_gate(0.9, 1.0, eligible_listings=3)
    assert at["shown"] and at["reason"] is None
    below = analytics.demand_model_gate(0.9001, 1.0, eligible_listings=3)
    assert not below["shown"] and below["code"] == "backtest"
    assert below["progress"] == [{"label": "% better than same as last week", "have": 9.9, "need": 10.0}]
    assert not analytics.demand_model_gate(0.0, 0.0, eligible_listings=3)["shown"]  # no baseline error to beat
    none = analytics.demand_model_gate(None, None, eligible_listings=0)
    assert not none["shown"] and none["code"] == "listing_history"


def test_demand_history_gate_when_the_history_is_already_enough():
    """A shop whose best listing meets every threshold but has no forecast is not told it lacks data."""
    gate = analytics.demand_history_gate(analytics.demand_listing_progress(56, 56, 56))
    assert not gate["shown"] and " of " not in gate["reason"]
    assert gate["reason"].startswith("No forecasts for your listings yet")


def test_bundles_gate_boundary():
    assert analytics.bundles_gate(1000)["shown"]
    below = analytics.bundles_gate(999)
    assert not below["shown"] and below["reason"] == ("Not enough shared baskets yet: 999 of 1,000 multi-item "
                                                      "purchases in the last 90 days")


def test_price_flag_gates_boundaries():
    assert analytics.price_flags_shops_gate(25)["shown"]
    assert not analytics.price_flags_shops_gate(24)["shown"]
    assert analytics.price_flags_precision_gate(35, 15)["shown"]  # 50 reviewed, 70% upheld
    fewer = analytics.price_flags_precision_gate(35, 14)  # 49 reviewed
    assert not fewer["shown"] and fewer["reason"].endswith("49 of 50 flags reviewed")
    imprecise = analytics.price_flags_precision_gate(34, 16)  # 50 reviewed, 68% upheld
    assert not imprecise["shown"] and "68% of reviewed flags were right" in imprecise["reason"]
    assert analytics.price_flags_precision_gate(70, 30)["shown"]
    assert not analytics.price_flags_precision_gate(69, 31)["shown"]  # 69%


def test_search_rule_boundary():
    assert analytics.search_query_shown(5)
    assert not analytics.search_query_shown(4)
    with pytest.raises(ValueError, match="NEARSHOP_SEARCH_INSIGHTS_MIN_USERS"):
        Settings(search_insights_min_users=4)


# ------------------------------------------------------------------ demo rows in production mode

def test_production_mode_leaves_out_demo_rows(monkeypatch):
    assert Settings(environment="production").models_real_data_only is True
    assert Settings(environment="demo").models_real_data_only is False
    assert Settings(environment="production", models_real_data_only=False).models_real_data_only is False
    monkeypatch.setattr(settings, "models_real_data_only", True)
    assert analytics.real_sales_filter() == {"is_demo": False}
    monkeypatch.setattr(settings, "models_real_data_only", False)
    assert analytics.real_sales_filter() == {}


def test_demand_training_excludes_demo_sales_in_production_mode(db, ids, clean, monkeypatch):
    made, tag = clean
    shop_id, product_id = own_listing(db, made, tag)
    # 60 days of demo sales: eligible when demo rows count, nothing at all when they do not.
    db.sales_history.insert_many([sale(tag, shop_id, product_id, d, 2, demo=True) for d in range(60)])
    monkeypatch.setattr(settings, "models_real_data_only", False)
    run = demand.train_and_forecast(db)
    assert run.uses_demo_data and run.metrics["eligible_listings"] >= 1

    monkeypatch.setattr(settings, "models_real_data_only", True)
    run = demand.train_and_forecast(db)
    assert not run.uses_demo_data
    assert product_id not in {f.product_id for f in db.demand_forecasts.find()}
    shop = db.shops.get(shop_id)
    gate = analytics.owner_insights(db, shop)["gates"]["demand"]
    assert not gate["shown"] and gate["reason"] == "Not enough sales data yet: 0 of 56 days"


def test_bundle_training_excludes_demo_baskets_and_counts_the_window(db, ids, clean, monkeypatch):
    _, tag = clean
    tape_item = db.products.get(ids["tape"]).catalog_item_id
    charger_item = db.products.get(ids["charger"]).catalog_item_id
    monkeypatch.setattr(settings, "bundles_min_baskets", 25)
    rows = []
    for n in range(25):  # 25 real two-item baskets, one of them older than the 90-day window
        days = 120 if n == 0 else n
        rows += [sale(tag, ids["shop_a"], ids["tape"], days, basket=f"r{n}", catalog_item_id=tape_item),
                 sale(tag, ids["shop_a"], ids["charger"], days, basket=f"r{n}", catalog_item_id=charger_item)]
    for n in range(40):  # single-item baskets of another item: lift for the pair above 1.5
        rows.append(sale(tag, ids["shop_b"], ids["tape_far"], n % 20, basket=f"s{n}", catalog_item_id=987_654))
    for n in range(30):  # demo baskets
        rows += [sale(tag, ids["shop_a"], ids["tape"], 3, demo=True, basket=f"d{n}", catalog_item_id=tape_item),
                 sale(tag, ids["shop_a"], ids["charger"], 3, demo=True, basket=f"d{n}", catalog_item_id=charger_item)]
    db.sales_history.insert_many(rows)

    monkeypatch.setattr(settings, "models_real_data_only", True)
    run = recommend.train(db)
    assert not run.uses_demo_data and run.metrics["recent_multi_item_baskets"] == 24
    assert not run.metrics["gate"]["shown"] and run.metrics["gate"]["progress"][0] == {
        "label": "multi-item purchases", "have": 24, "need": 25}
    assert db.recommendations.count() == 0

    monkeypatch.setattr(settings, "models_real_data_only", False)
    run = recommend.train(db)
    assert run.uses_demo_data and run.metrics["recent_multi_item_baskets"] >= 25
    assert run.metrics["gate"]["shown"] and db.recommendations.count() > 0


# ------------------------------------------------------------------ demand publishes only eligible listings

def test_demand_publishes_only_eligible_listings_and_only_when_the_gate_passes(db, ids, clean, monkeypatch):
    _, tag = clean
    monkeypatch.setattr(settings, "models_real_data_only", True)
    # The tape: 60 days with 2 units each (eligible). The charger: 20 days of sales (ineligible: 20 of 56 days).
    db.sales_history.insert_many([sale(tag, ids["shop_a"], ids["tape"], d, 2) for d in range(60)]
                                 + [sale(tag, ids["shop_a"], ids["charger"], d, 2) for d in range(20)])
    monkeypatch.setattr(demand, "demand_model_gate", lambda *a, **k: analytics.gate(True))
    run = demand.train_and_forecast(db)
    assert run.metrics["gate"]["shown"]
    published = {f.product_id for f in db.demand_forecasts.find()}
    assert ids["tape"] in published and ids["charger"] not in published

    closed = analytics.gate(False, "backtest", "Forecasts are paused")
    monkeypatch.setattr(demand, "demand_model_gate", lambda *a, **k: closed)
    run = demand.train_and_forecast(db)
    assert db.demand_forecasts.count() == 0 and run.metrics["gate"] == closed


# ------------------------------------------------------------------ price flags

def test_price_flags_need_verified_shops_and_distinct_peer_shops(db, ids, clean, monkeypatch):
    made, tag = clean
    run = anomaly.detect(db)  # the test city has no verified shops
    assert run.metrics["gate"]["code"] == "verified_shops" and run.metrics["flagged"] == 0

    monkeypatch.setattr(settings, "price_flags_min_verified_shops", 1)
    a, b = ids["shop_a"], ids["shop_b"]
    cat = db.products.get(ids["tape"]).category_id
    item = db.catalog_items.insert(CatalogItem(slug=f"{tag}-item", name="Gate test item", category_id=cat,
                                               typical_price=100, icon="Droplets"))
    made["catalog_items"].append(item.id)
    extra = db.shops.insert_many([
        Shop(owner_id=1, name=f"{tag} {n}", slug=f"{tag}-{n}", address_line="x", city="Mysuru", lat=CENTER[0],
             lng=CENTER[1], is_verified=True) for n in range(2)])
    made["shops"] += [s.id for s in extra]

    def listings(shop_ids):
        ps = db.products.insert_many([Product(shop_id=s, catalog_item_id=item.id, category_id=cat, name="Gate test item",
                                              price=100 + n, quantity=5) for n, s in enumerate(shop_ids)])
        made["products"].extend(p.id for p in ps)
        return ps

    listings([a, b, extra[0].id])  # 3 peer shops: below 4
    stale = db.price_anomalies.insert(PriceAnomaly(product_id=ids["charger"], shop_id=a, catalog_item_id=1, price=1,
                                                   reference_price=1, deviation_pct=50, score=0.7, direction="high",
                                                   peer_count=4, model_run_id=1, review_note=tag))
    run = anomaly.detect(db)
    assert run.metrics["gate"]["shown"] and run.metrics["listings_checked"] == 0
    assert db.price_anomalies.get(stale.id) is None  # too few listings to compare: older open flags are withdrawn
    listings([extra[1].id])  # a fourth shop
    run = anomaly.detect(db)
    assert run.metrics["listings_checked"] == 4


def test_owner_sees_price_flags_only_after_precise_admin_review(db, ids, clean, owner_a):
    made, tag = clean
    db.model_runs.insert(ModelRun(name="anomalies", algorithm="IsolationForest",
                                  metrics={"gate": analytics.gate(True)}))
    flag = db.price_anomalies.insert(PriceAnomaly(product_id=ids["charger"], shop_id=ids["shop_a"], catalog_item_id=1,
                                                  price=1600, reference_price=1250, deviation_pct=28, score=0.7,
                                                  direction="high", peer_count=4, model_run_id=1, review_note=tag))
    body = owner_a.get("/api/owner/insights", headers=HEADERS).json()
    assert body["price_flags"] == [] and body["gates"]["price_flags"]["code"] == "precision"
    assert body["gates"]["price_flags"]["reason"].endswith("0 of 50 flags reviewed")

    def reviewed(n: int, status: AnomalyStatus) -> list[PriceAnomaly]:
        return [PriceAnomaly(product_id=10_000 + i, shop_id=999, catalog_item_id=1, price=1, reference_price=1,
                             deviation_pct=0, score=0, direction="high", peer_count=4, model_run_id=1, status=status,
                             review_note=tag) for i in range(n)]

    db.price_anomalies.insert_many(reviewed(35, AnomalyStatus.REVIEWED) + reviewed(15, AnomalyStatus.DISMISSED))
    body = owner_a.get("/api/owner/insights", headers=HEADERS).json()
    assert body["gates"]["price_flags"]["shown"] and [f["id"] for f in body["price_flags"]] == [flag.id]


# ------------------------------------------------------------------ the insights payload

def test_insights_payload_gives_a_reason_and_progress_for_each_gated_module(db, ids, clean, owner_a, monkeypatch):
    made, tag = clean
    # The progress counts UTC days (analytics._shop_demand_progress). Pin its clock, and put each sale on its own UTC
    # day at or before that time, so a run just after UTC midnight (early morning IST) counts the same 23 days.
    now = utcnow()
    monkeypatch.setattr(analytics, "utcnow", lambda: now)
    day0 = max(now - timedelta(hours=1), now.replace(hour=0, minute=0, second=0, microsecond=0))
    body = owner_a.get("/api/owner/insights", headers=HEADERS).json()
    gates = body["gates"]
    assert set(gates) == {"demand", "bundles", "price_flags", "area_demand", "missed_demand"}
    for name, g in gates.items():
        assert not g["shown"] and g["reason"], name
    assert gates["demand"]["code"] == "not_trained"
    assert body["restock"] == body["rising"] == body["bundle_gaps"] == body["price_flags"] == []

    # The model passes model-wide, but Shop A's best listing has 23 of 56 days: the reason says so.
    db.model_runs.insert(ModelRun(name="demand", algorithm="RandomForestRegressor",
                                  metrics={"gate": analytics.gate(True)}))
    _, product_id = own_listing(db, made, tag, ids["shop_a"])
    sales = [sale(tag, ids["shop_a"], product_id, d, 3) for d in range(23)]
    for d, record in enumerate(sales):
        record.sold_at = day0 - timedelta(days=d)
    db.sales_history.insert_many(sales)
    gate = owner_a.get("/api/owner/insights", headers=HEADERS).json()["gates"]["demand"]
    assert not gate["shown"] and gate["reason"] == "Not enough sales data yet: 23 of 56 days"
    assert gate["progress"] == [{"label": "days", "have": 23, "need": 56},
                                {"label": "days with a sale", "have": 23, "need": 20},
                                {"label": "units sold", "have": 69, "need": 30}]

    # Shown once the shop has a forecast; a gated bundle model keeps its reason.
    db.demand_forecasts.insert(DemandForecast(product_id=ids["tape"], shop_id=ids["shop_a"], predicted_7d=20,
                                              daily_rate=3, days_to_stockout=2, recommended_restock=10, model_run_id=1))
    db.model_runs.insert(ModelRun(name="recommendations", algorithm="Apriori",
                                  metrics={"gate": analytics.bundles_gate(23)}))
    body = owner_a.get("/api/owner/insights", headers=HEADERS).json()
    assert body["gates"]["demand"]["shown"] and [r["id"] for r in body["restock"]] == [ids["tape"]]
    assert body["gates"]["bundles"]["reason"].startswith("Not enough shared baskets yet: 23 of 1,000")


def test_model_cards_say_whether_each_model_is_gated(client, clean, db):
    db.model_runs.insert(ModelRun(name="recommendations", algorithm="Apriori",
                                  metrics={"gate": analytics.bundles_gate(5)}))
    cards = {m["name"]: m for m in client.get("/api/ai/models").json()["models"]}
    assert cards["embeddings"]["gate"] is None and cards["word2vec"]["gate"] is None
    assert cards["recommendations"]["gate"]["code"] == "baskets"
    assert cards["demand"]["gate"]["code"] == "not_trained"


# ------------------------------------------------------------------ the 5-user rule for search insights

def test_search_insights_need_five_distinct_users_nearby_in_30_days(db, ids, clean):
    _, tag = clean
    shop = db.shops.get(ids["shop_a"])
    q = f"{tag} usb fan"
    near = {"lat": shop.lat, "lng": shop.lng}

    def search(user_id, days_ago=1, results=0, **where):
        return SearchEvent(user_id=user_id, query=q, normalized_query=q, results_count=results,
                           created_at=utcnow() - timedelta(days=days_ago), **(where or near))

    db.search_history.insert_many(
        [search(u) for u in (101, 102, 103, 104)] + [search(101), search(None), search(None)]  # 4 users, 7 searches
        + [search(105, days_ago=31)]  # outside the 30 days
        + [search(106, lat=shop.lat + 0.2, lng=shop.lng)])  # 22 km away, outside the shop's 3 km
    out = analytics.owner_insights(db, shop)
    assert q not in [a["query"] for a in out["area_demand"]]
    gate = out["gates"]["area_demand"]
    assert not gate["shown"] and gate["progress"] == [{"label": "people", "have": 4, "need": 5}]
    assert gate["reason"] == ("Not enough searches near you yet: 4 of 5 people searched for the same thing in the "
                              "last 30 days")

    assert out["search_rule"] == {"window_days": 30, "min_users": 5}  # the owner page's subtitle uses these

    db.search_history.insert(search(107))  # a fifth person
    out = analytics.owner_insights(db, shop)
    assert [a["searches"] for a in out["area_demand"] if a["query"] == q] == [8]
    assert out["gates"]["area_demand"]["shown"]
    # Missed demand comes from the weekly demand report (P9.7, tests/test_demand_report.py), never from raw
    # zero-result searches: switched off by default, it shows nothing and says why.
    assert out["missed_demand"] == [] and out["gates"]["missed_demand"]["code"] == "disabled"
