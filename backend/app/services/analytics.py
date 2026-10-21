"""Numbers behind the shop-owner and admin dashboards.

Each figure is one aggregation pipeline that runs inside MongoDB; the independent ones for a
page are sent together (db.gather), so a dashboard costs about one network round trip.
"""
import logging
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pymongo.database import Database as MongoDatabase
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.ai.semantic_index import index
from app.core.config import settings
from app.core.database import Database, utcnow
from app.models import AnomalyStatus, InventoryReason, OrderStatus, ReservationStatus, Role, Shop
from app.schemas.serializers import iso, listing
from app.services.loaders import categories, with_category
from app.services.search import keyword_scores
from app.utils.geo import within_km
from app.utils.text import normalize_query

IST = ZoneInfo("Asia/Kolkata")
REVENUE = {"$sum": {"$multiply": ["$quantity", "$unit_price"]}}


def ist_day(field: str) -> dict:
    """Aggregation expression: the local (IST) calendar day of a UTC timestamp, as YYYY-MM-DD."""
    return {"$dateToString": {"format": "%Y-%m-%d", "date": field, "timezone": "Asia/Kolkata"}}


def ist_day_start_utc(days_ago: int = 0) -> datetime:
    """UTC timestamp of local (IST) midnight `days_ago` days back."""
    now_ist = datetime.now(IST)
    midnight = (now_ist - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _sales_by_ist_day(db: Database, match: dict | None = None, days: int = 30) -> list[dict]:
    rows = db.sales_history.aggregate([
        {"$match": {"sold_at": {"$gte": ist_day_start_utc(days - 1)}, **(match or {})}},
        {"$group": {"_id": ist_day("$sold_at"), "revenue": REVENUE, "units": {"$sum": "$quantity"}}},
    ])
    by_day = {r["_id"]: r for r in rows}
    out = []
    for i in range(days - 1, -1, -1):
        day = (datetime.now(IST) - timedelta(days=i)).date().isoformat()
        r = by_day.get(day, {})
        out.append({"date": day, "revenue": round(float(r.get("revenue") or 0), 2), "units": int(r.get("units") or 0)})
    return out


def _count_by(db_collection, field: str, match: dict | None = None) -> dict:
    return {r["_id"]: r["n"] for r in db_collection.aggregate([
        {"$match": match or {}}, {"$group": {"_id": f"${field}", "n": {"$sum": 1}}}])}


# ------------------------------------------------------------------ model gates (P7.3)
#
# Plan section 8.9: owners see a model's output only when the data meets its threshold (settings in
# app/core/config.py), and otherwise a reason with the progress towards it. Every gate is a dict:
#   shown     whether the output is shown
#   code      why not (None when shown): not_trained, listing_history, backtest, baskets, verified_shops,
#             precision or few_users
#   reason    the sentence the owner sees, for example "Not enough sales data yet: 23 of 56 days"
#   progress  [{label, have, need}], what the threshold counts so far
# The training jobs (app/ai/demand.py, recommend.py, anomaly.py) store the model-wide gate in their run's
# metrics["gate"]; owner_insights adds the per-shop and live parts. Nothing here imports a training library.

DEMAND_HISTORY_DAYS = 150  # the days of sales the demand model trains on (app/ai/demand.py)


def real_sales_filter() -> dict:
    """The sales_history filter the models train and count on: is_demo rows are left out when
    NEARSHOP_MODELS_REAL_DATA_ONLY is on (the default in production)."""
    return {"is_demo": False} if settings.models_real_data_only else {}


def gate(shown: bool, code: str | None = None, reason: str | None = None,
         progress: list[dict] | None = None) -> dict:
    return {"shown": shown, "code": None if shown else code, "reason": None if shown else reason,
            "progress": progress or []}


def progress_item(label: str, have: float, need: float) -> dict:
    return {"label": label, "have": have, "need": need}


def _fmt(n: float) -> str:
    return f"{n:g}" if isinstance(n, float) else f"{n:,}"


def demand_listing_progress(history_days: int, sale_days: int, units: int) -> list[dict]:
    """One listing's progress towards demand-forecast eligibility."""
    return [progress_item("days", history_days, settings.demand_min_history_days),
            progress_item("days with a sale", sale_days, settings.demand_min_sale_days),
            progress_item("units sold", units, settings.demand_min_units)]


def demand_listing_eligible(history_days: int, sale_days: int, units: int) -> bool:
    return all(p["have"] >= p["need"] for p in demand_listing_progress(history_days, sale_days, units))


def demand_history_gate(progress: list[dict] | None) -> dict:
    """Not shown for want of listing history: progress is the closest listing's, or None for no sales at all."""
    progress = progress or demand_listing_progress(0, 0, 0)
    short = next((p for p in progress if p["have"] < p["need"]), None)
    if short is None:  # the history is there, but no forecast was made for an active listing (yet)
        return gate(False, "listing_history",
                    "No forecasts for your listings yet: they appear after the next model run, for active "
                    "listings with enough sales", progress)
    return gate(False, "listing_history",
                f"Not enough sales data yet: {_fmt(short['have'])} of {_fmt(short['need'])} {short['label']}",
                progress)


def demand_model_gate(mae: float | None, naive_mae: float | None, eligible_listings: int) -> dict:
    """Model-wide: shown only when, on the backtest of the eligible listings, the forecast's MAE is at least
    demand_min_improvement_pct percent below the naive same-as-last-week forecast's."""
    need = settings.demand_min_improvement_pct
    if eligible_listings == 0 or mae is None or naive_mae is None:
        return demand_history_gate(None)
    improvement = (naive_mae - mae) / naive_mae * 100 if naive_mae else 0.0
    shown = naive_mae > 0 and mae <= naive_mae * (1 - need / 100)
    return gate(shown, "backtest",
                f"Forecasts are paused: over the last {settings.demand_backtest_days} days they were "
                f"{math.floor(improvement * 10) / 10:g}% better than \"same as last week\", and need to be {need:g}% better",
                [progress_item("% better than same as last week", math.floor(improvement * 10) / 10, need)])


def bundles_gate(multi_item_baskets: int) -> dict:
    need = settings.bundles_min_baskets
    return gate(multi_item_baskets >= need, "baskets",
                f"Not enough shared baskets yet: {multi_item_baskets:,} of {need:,} multi-item purchases in the "
                f"last {settings.bundles_window_days} days",
                [progress_item("multi-item purchases", multi_item_baskets, need)])


def price_flags_shops_gate(verified_live_shops: int) -> dict:
    need = settings.price_flags_min_verified_shops
    return gate(verified_live_shops >= need, "verified_shops",
                f"Not enough shops to compare prices yet: {verified_live_shops} of {need} verified shops",
                [progress_item("verified shops", verified_live_shops, need)])


def price_flags_precision_gate(upheld: int, dismissed: int) -> dict:
    """Owners see flags only once admins have reviewed enough of them and upheld enough of those."""
    reviewed = upheld + dismissed
    precision = round(100 * upheld / reviewed, 1) if reviewed else 0.0
    need_n, need_pct = settings.price_flags_min_reviewed, settings.price_flags_min_precision_pct
    shown = reviewed >= need_n and upheld * 100 >= need_pct * reviewed
    reason = (f"Price checks are still being reviewed by NearShop: {reviewed} of {need_n} flags reviewed"
              if reviewed < need_n else
              f"Price checks are still being tuned: {precision:g}% of reviewed flags were right, and {need_pct:g}% "
              f"is needed")
    return gate(shown, "precision", reason, [progress_item("flags reviewed", reviewed, need_n),
                                              progress_item("% upheld", precision, need_pct)])


def search_query_shown(distinct_users: int) -> bool:
    return distinct_users >= settings.search_insights_min_users


def search_gate(best_users: int, any_shown: bool) -> dict:
    need = settings.search_insights_min_users
    return gate(any_shown, "few_users",
                f"Not enough searches near you yet: {best_users} of {need} people searched for the same thing in "
                f"the last {settings.search_insights_window_days} days",
                [progress_item("people", best_users, need)])


def _run_gate(run, not_trained: str) -> dict:
    if run is None:
        return gate(False, "not_trained", not_trained)
    stored = (run.metrics or {}).get("gate")
    return stored if stored else gate(False, "not_trained", not_trained)


def price_flag_review_counts(db: Database) -> tuple[int, int]:
    """(upheld, dismissed) admin decisions: an admin marking a flag "reviewed" counts as upholding it."""
    counts = _count_by(db.price_anomalies, "status",
                       {"status": {"$in": [AnomalyStatus.REVIEWED.value, AnomalyStatus.DISMISSED.value]}})
    return counts.get(AnomalyStatus.REVIEWED.value, 0), counts.get(AnomalyStatus.DISMISSED.value, 0)


def _shop_demand_progress(db: Database, shop_id: int) -> list[dict] | None:
    """The progress of this shop's listing closest to demand-forecast eligibility, or None without sales."""
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.sales_history.aggregate([
        {"$match": {"shop_id": shop_id, "sold_at": {"$gte": today - timedelta(days=DEMAND_HISTORY_DAYS - 1)},
                    **real_sales_filter()}},
        {"$group": {"_id": "$product_id", "first": {"$min": "$sold_at"}, "units": {"$sum": "$quantity"},
                    "days": {"$addToSet": {"$dateTrunc": {"date": "$sold_at", "unit": "day"}}}}},
        {"$project": {"first": 1, "units": 1, "sale_days": {"$size": "$days"}}},
    ])
    best, best_score = None, -1.0
    for r in rows:
        first = r["first"].replace(hour=0, minute=0, second=0, microsecond=0)
        progress = demand_listing_progress((today - first).days + 1, int(r["sale_days"]), int(r["units"]))
        score = min(min(p["have"] / p["need"], 1.0) for p in progress)
        if score > best_score:
            best, best_score = progress, score
    return best


# ------------------------------------------------------------------ shop owner

def owner_overview(db: Database, shop: Shop) -> dict:
    today = ist_day_start_utc(0)
    mine = {"shop_id": shop.id}
    low_stock_expr = {"$expr": {"$lte": ["$quantity", "$low_stock_threshold"]}}

    def top_products() -> list[dict]:
        return db.sales_history.aggregate([
            {"$match": {**mine, "sold_at": {"$gte": ist_day_start_utc(29)}}},
            {"$group": {"_id": "$product_id", "units": {"$sum": "$quantity"}, "revenue": REVENUE}},
            {"$sort": {"units": -1, "_id": 1}},
            {"$limit": 6},
        ])

    def inventory() -> dict:
        rows = db.products.aggregate([
            {"$match": {**mine, "is_active": True}},
            {"$group": {
                "_id": None,
                "listings": {"$sum": 1},
                "in_stock": {"$sum": {"$cond": [{"$gt": ["$quantity", "$low_stock_threshold"]}, 1, 0]}},
                "low_stock": {"$sum": {"$cond": [{"$and": [{"$gt": ["$quantity", 0]},
                                                           {"$lte": ["$quantity", "$low_stock_threshold"]}]}, 1, 0]}},
                "out_of_stock": {"$sum": {"$cond": [{"$eq": ["$quantity", 0]}, 1, 0]}},
            }},
        ])
        return rows[0] if rows else {}

    series, top, inv, low, res_status, ord_status, res_today, orders_today, uses_demo = db.gather(
        lambda: _sales_by_ist_day(db, mine, days=30),
        top_products,
        inventory,
        lambda: with_category(db, db.products.find({**mine, "is_active": True, **low_stock_expr},
                                                   sort=[("quantity", 1)], limit=8)),
        lambda: _count_by(db.reservations, "status", mine),
        lambda: _count_by(db.orders, "status", mine),
        lambda: db.reservations.count({**mine, "created_at": {"$gte": today}}),
        lambda: db.orders.count({**mine, "created_at": {"$gte": today}}),
        lambda: db.sales_history.exists({**mine, "is_demo": True}),
    )
    top_by_id, forecasts = db.gather(
        lambda: {p.id: p for p in with_category(db, db.products.by_ids(t["_id"] for t in top).values())},
        lambda: {f.product_id: f for f in db.demand_forecasts.find({"product_id": {"$in": [p.id for p in low]}})},
    )

    def revenue(days_from: int, days_to: int = 0) -> float:
        """Revenue for the IST days [days_from .. days_to] ago, read off the 30-day series."""
        return round(sum(d["revenue"] for d in series[len(series) - 1 - days_from: len(series) - days_to]), 2)

    def res(*statuses) -> int:
        return sum(res_status.get(s.value, 0) for s in statuses)

    def orders(*statuses) -> int:
        return sum(ord_status.get(s.value, 0) for s in statuses)

    return {
        "today": {"revenue": series[-1]["revenue"], "units": series[-1]["units"], "reservations": res_today,
                  "orders": orders_today},
        "revenue_7d": revenue(6),
        "revenue_prev_7d": revenue(13, 7),
        "revenue_30d": revenue(29),
        "queue": {
            "reservation_requests": res(ReservationStatus.REQUESTED),
            "pickups_waiting": res(ReservationStatus.CONFIRMED, ReservationStatus.READY_FOR_PICKUP),
            "orders_pending": orders(OrderStatus.PENDING),
            "deliveries_in_progress": orders(OrderStatus.SHOP_CONFIRMED, OrderStatus.PREPARING,
                                             OrderStatus.OUT_FOR_DELIVERY),
        },
        "inventory": {
            "listings": inv.get("listings", 0),
            "in_stock": inv.get("in_stock", 0),
            "low_stock": inv.get("low_stock", 0),
            "out_of_stock": inv.get("out_of_stock", 0),
            "updated_at": iso(shop.inventory_updated_at),
        },
        "sales_series": series,
        "top_products": [
            {**listing(top_by_id[t["_id"]]), "units": int(t["units"]), "revenue": round(float(t["revenue"]), 2)}
            for t in top if t["_id"] in top_by_id
        ],
        "low_stock": [
            {**listing(p), "days_to_stockout": forecasts[p.id].days_to_stockout if p.id in forecasts else None,
             "recommended_restock": forecasts[p.id].recommended_restock if p.id in forecasts else None}
            for p in low
        ],
        "rating": {"avg": shop.rating_avg, "count": shop.rating_count},
        "includes_demo_data": uses_demo,
    }


def _nearby_searches(db: Database, shop: Shop) -> list[dict]:
    """Queries searched within the shop's radius in the search-insights window: searches, distinct signed-in
    users, and the searches and distinct users that found nothing (2dsphere index)."""
    radius = shop.delivery_radius_km if shop.offers_delivery and shop.delivery_radius_km > 0 \
        else settings.search_insights_radius_km
    since = utcnow() - timedelta(days=settings.search_insights_window_days)
    zero = {"$eq": ["$results_count", 0]}

    def distinct(field: str) -> dict:
        return {"$size": {"$filter": {"input": field, "cond": {"$ne": ["$$this", None]}}}}

    return db.search_history.aggregate([
        {"$match": {"created_at": {"$gte": since}, "location": within_km(shop.lat, shop.lng, radius)}},
        {"$group": {"_id": "$normalized_query", "searches": {"$sum": 1},
                    "users": {"$addToSet": "$user_id"},
                    "zero": {"$sum": {"$cond": [zero, 1, 0]}},
                    "zero_users": {"$addToSet": {"$cond": [zero, "$user_id", None]}}}},
        {"$project": {"searches": 1, "zero": 1, "users": distinct("$users"), "zero_users": distinct("$zero_users")}},
    ])


def owner_insights(db: Database, shop: Shop) -> dict:
    """The owner's insights: each module's output when its model gate is open, and otherwise the gate's
    reason and progress (gates[module]); a gated module's output is always empty."""
    forecasts, flags, searches, my_products, rules, runs, review_counts = db.gather(
        lambda: db.demand_forecasts.find({"shop_id": shop.id}),
        lambda: db.price_anomalies.find({"shop_id": shop.id, "status": AnomalyStatus.OPEN}, sort=[("score", -1)]),
        lambda: _nearby_searches(db, shop),
        lambda: db.products.find_raw({"shop_id": shop.id, "is_active": True}, {"catalog_item_id": 1, "quantity": 1}),
        lambda: db.recommendations.find(sort=[("lift", -1)], limit=3000),
        lambda: db.model_runs.find({"name": {"$in": ["demand", "recommendations", "anomalies"]}},
                                   sort=[("created_at", 1)]),
        lambda: price_flag_review_counts(db),
    )
    latest = {r.name: r for r in runs}  # sorted oldest first, so the newest run of each model wins

    # The gates: the model-wide part stored by the last training run, then this shop's own part.
    demand_gate = _run_gate(latest.get("demand"), "Forecasts have not been trained yet")
    if (demand_gate["shown"] and not forecasts) or demand_gate["code"] == "listing_history":
        demand_gate = demand_history_gate(_shop_demand_progress(db, shop.id))
    bundle_gate = _run_gate(latest.get("recommendations"), "Bundles have not been learned yet")
    flag_gate = _run_gate(latest.get("anomalies"), "Prices have not been compared yet")
    if flag_gate["shown"]:
        flag_gate = price_flags_precision_gate(*review_counts)
    if not demand_gate["shown"]:
        forecasts = []
    if not bundle_gate["shown"]:
        rules = []
    if not flag_gate["shown"]:
        flags = []

    products = {p.id: p for p in with_category(
        db, db.products.by_ids([f.product_id for f in forecasts] + [a.product_id for a in flags]).values())}

    # 1. Demand forecasts (Random Forest)
    restock, rising = [], []
    for fc in forecasts:
        p = products.get(fc.product_id)
        if p is None or not p.is_active:
            continue
        item = {
            **listing(p),
            "predicted_7d": fc.predicted_7d,
            "daily_rate": fc.daily_rate,
            "days_to_stockout": fc.days_to_stockout,
            "recommended_restock": fc.recommended_restock,
            "trend": fc.trend,
            "confidence": fc.confidence,
            "is_demo": fc.is_demo,
        }
        if fc.recommended_restock > 0 or (fc.days_to_stockout is not None and fc.days_to_stockout < 10):
            restock.append(item)
        if fc.trend == "rising" and fc.predicted_7d >= 3:
            rising.append(item)
    restock.sort(key=lambda x: (x["days_to_stockout"] if x["days_to_stockout"] is not None else 999))
    rising.sort(key=lambda x: -x["predicted_7d"])

    # 2. Price review flags (Isolation Forest) for this shop's own listings
    price_flags = [
        {"id": a.id, "product": listing(products[a.product_id]), "price": a.price,
         "reference_price": a.reference_price, "deviation_pct": a.deviation_pct, "direction": a.direction,
         "peer_count": a.peer_count}
        for a in flags if a.product_id in products
    ]

    # 3. What people nearby are searching for: only queries enough distinct people searched
    shown = [s for s in searches if search_query_shown(s["users"])]
    top_queries = sorted(shown, key=lambda s: (-s["searches"], s["_id"]))[:10]
    missed = sorted((s for s in searches if s["zero"] and search_query_shown(s["zero_users"])),
                    key=lambda s: (-s["zero"], s["_id"]))[:6]
    search_insights_gate = search_gate(max((s["users"] for s in searches), default=0), bool(shown))
    in_stock_ids = {p["_id"] for p in my_products if p["quantity"] > 0}

    def stocked(q: str) -> bool:
        kw = keyword_scores(db, normalize_query(q).split())
        sem = index.query(db, q)
        return any(pid in kw or sem.get(pid, 0) > 0.5 for pid in in_stock_ids)

    stocked_flags = db.gather(*[lambda q=s["_id"]: stocked(q) for s in top_queries]) if top_queries else []
    area_demand = [{"query": s["_id"], "searches": s["searches"], "you_stock_it": st}
                   for s, st in zip(top_queries, stocked_flags)]

    # 4. Bundle gaps (Apriori): things often bought with what you sell, that you do not stock
    my_items = {p["catalog_item_id"] for p in my_products if p.get("catalog_item_id")}
    gaps: dict[int, dict] = {}
    if my_items:
        for rule in rules:
            if len(rule.antecedents) != 1 or rule.antecedents[0] not in my_items:
                continue
            for c in rule.consequents:
                if c not in my_items and (c not in gaps or gaps[c]["confidence"] < rule.confidence):
                    gaps[c] = {"catalog_item_id": c, "because_of": rule.antecedents[0], "confidence": rule.confidence,
                               "lift": rule.lift}
    top_gaps = sorted(gaps.values(), key=lambda g: -g["confidence"] * g["lift"])[:6]
    names = db.catalog_items.by_ids([g["catalog_item_id"] for g in top_gaps] + [g["because_of"] for g in top_gaps])
    bundle_gaps = [
        {"item": {"id": g["catalog_item_id"], "name": names[g["catalog_item_id"]].name,
                  "icon": names[g["catalog_item_id"]].icon,
                  "typical_price": names[g["catalog_item_id"]].typical_price},
         "because_of": names[g["because_of"]].name, "confidence": g["confidence"], "lift": g["lift"]}
        for g in top_gaps if g["catalog_item_id"] in names and g["because_of"] in names
    ]

    return {
        "restock": restock[:12],
        "rising": rising[:6],
        "price_flags": price_flags,
        "area_demand": area_demand,
        "missed_demand": [{"query": s["_id"], "searches": s["zero"]} for s in missed],
        "bundle_gaps": bundle_gaps,
        "models": {
            name: {"algorithm": r.algorithm, "trained_at": iso(r.created_at), "uses_demo_data": r.uses_demo_data,
                   "metrics": r.metrics, "note": r.data_note}
            for name, r in latest.items()
        },
        "search_rule": {"window_days": settings.search_insights_window_days,
                        "min_users": settings.search_insights_min_users},
        "gates": {"demand": demand_gate, "bundles": bundle_gate, "price_flags": flag_gate,
                  "area_demand": search_insights_gate,
                  "missed_demand": search_gate(max((s["zero_users"] for s in searches), default=0), bool(missed))},
    }


# ------------------------------------------------------------------ admin

def admin_overview(db: Database) -> dict:
    since30 = ist_day_start_utc(29)
    since14 = ist_day_start_utc(13)
    owner_reasons = [InventoryReason.RESTOCK.value, InventoryReason.ADJUSTMENT.value, InventoryReason.INITIAL.value]

    def by_day(collection, match: dict) -> dict:
        return {r["_id"]: r["n"] for r in collection.aggregate([
            {"$match": match}, {"$group": {"_id": ist_day("$created_at"), "n": {"$sum": 1}}}])}

    def top_shops() -> list[dict]:
        return db.sales_history.aggregate([
            {"$match": {"sold_at": {"$gte": since30}}},
            {"$group": {"_id": "$shop_id", "rev": REVENUE, "baskets": {"$addToSet": "$basket_id"}}},
            {"$project": {"rev": 1, "baskets": {"$size": "$baskets"}}},
            {"$sort": {"rev": -1}},
            {"$limit": 8},
        ])

    def searches(match: dict, limit: int) -> list[dict]:
        return db.search_history.aggregate([
            {"$match": match},
            {"$group": {"_id": "$normalized_query", "n": {"$sum": 1}, "avg": {"$avg": "$results_count"}}},
            {"$sort": {"n": -1, "_id": 1}},
            {"$limit": limit},
        ])

    def product_totals() -> dict:
        rows = db.products.aggregate([
            {"$match": {"is_active": True}},
            {"$group": {"_id": None, "listings": {"$sum": 1},
                        "in_stock": {"$sum": {"$cond": [{"$gt": ["$quantity", 0]}, 1, 0]}}}},
        ])
        return rows[0] if rows else {}

    def shop_totals() -> dict:
        rows = db.shops.aggregate([{"$group": {
            "_id": None,
            "active": {"$sum": {"$cond": ["$is_active", 1, 0]}},
            "verified": {"$sum": {"$cond": ["$is_verified", 1, 0]}},
        }}])
        return rows[0] if rows else {}

    (res_status, ord_status, cat_sales, cat_searches, top, popular, zero, inv_rows, search_rows, roles, shops_n,
     products_n, revenue_by_demo, searches_30d, open_flags, series) = db.gather(
        lambda: _count_by(db.reservations, "status"),
        lambda: _count_by(db.orders, "status"),
        # Category revenue straight from sales: `category_id` is denormalised onto each sale.
        lambda: db.sales_history.aggregate([
            {"$match": {"sold_at": {"$gte": since30}, "category_id": {"$ne": None}}},
            {"$group": {"_id": "$category_id", "rev": REVENUE}},
            {"$sort": {"rev": -1}},
        ]),
        lambda: _count_by(db.search_history, "category_slug", {"category_slug": {"$ne": None}}),
        top_shops,
        lambda: searches({"created_at": {"$gte": utcnow() - timedelta(days=14)}}, 10),
        lambda: searches({"results_count": 0, "created_at": {"$gte": utcnow() - timedelta(days=30)}}, 8),
        lambda: by_day(db.inventory_events, {"created_at": {"$gte": since14}, "reason": {"$in": owner_reasons}}),
        lambda: by_day(db.search_history, {"created_at": {"$gte": since14}}),
        lambda: _count_by(db.users, "role"),
        shop_totals,
        product_totals,
        lambda: {r["_id"]: r["rev"] for r in db.sales_history.aggregate([
            {"$match": {"sold_at": {"$gte": since30}}}, {"$group": {"_id": "$is_demo", "rev": REVENUE}}])},
        lambda: db.search_history.count({"created_at": {"$gte": since30}}),
        lambda: db.price_anomalies.count({"status": AnomalyStatus.OPEN}),
        lambda: _sales_by_ist_day(db, days=30),
    )
    cats = categories(db)
    top_shop_docs = db.shops.by_ids(s["_id"] for s in top)

    activity = []
    for i in range(13, -1, -1):
        d = (datetime.now(IST) - timedelta(days=i)).date().isoformat()
        activity.append({"date": d, "inventory_updates": inv_rows.get(d, 0), "searches": search_rows.get(d, 0)})

    return {
        "totals": {
            "customers": roles.get(Role.CUSTOMER.value, 0),
            "owners": roles.get(Role.OWNER.value, 0),
            "shops": shops_n.get("active", 0),
            "shops_verified": shops_n.get("verified", 0),
            "listings": products_n.get("listings", 0),
            "in_stock": products_n.get("in_stock", 0),
            "reservations": sum(res_status.values()),
            "orders": sum(ord_status.values()),
            "revenue_30d": round(float(sum(revenue_by_demo.values())), 2),
            "revenue_30d_real": round(float(revenue_by_demo.get(False, 0)), 2),
            "searches_30d": searches_30d,
            "open_price_flags": open_flags,
        },
        "reservations_by_status": res_status,
        "orders_by_status": ord_status,
        "sales_series": series,
        "categories": [{"name": cats[r["_id"]].name, "slug": cats[r["_id"]].slug, "color_hue": cats[r["_id"]].color_hue,
                        "revenue_30d": round(float(r["rev"] or 0), 2),
                        "searches": cat_searches.get(cats[r["_id"]].slug, 0)}
                       for r in cat_sales if r["_id"] in cats],
        "top_shops": [{"id": s["_id"], "name": top_shop_docs[s["_id"]].name, "slug": top_shop_docs[s["_id"]].slug,
                       "locality": top_shop_docs[s["_id"]].locality, "revenue_30d": round(float(s["rev"]), 2),
                       "baskets": s["baskets"], "rating_avg": top_shop_docs[s["_id"]].rating_avg}
                      for s in top if s["_id"] in top_shop_docs],
        "popular_searches": [{"query": r["_id"], "count": r["n"], "avg_results": round(float(r["avg"] or 0), 1)}
                             for r in popular],
        "zero_result_searches": [{"query": r["_id"], "count": r["n"]} for r in zero],
        "activity": activity,
    }


# ------------------------------------------------------------------ storage budget (P2.2)
#
# The Atlas tier's storage limit (M0 512 MB, Flex 5 GB) counts the uncompressed BSON of every document
# plus every index. Once a day (the snapshot_storage job in app/jobs.py, under a lease) each collection's
# data size and index size are recorded in storage_snapshots, one document per IST day: _id (the day,
# YYYY-MM-DD), taken_at, limit_mb, data_bytes, index_bytes, total_bytes and collections (name, documents,
# data_bytes, index_bytes for each). storage_report projects from them when 85% of
# NEARSHOP_STORAGE_LIMIT_MB is reached. Alerts: docs/runbooks/monitoring.md.

STORAGE_SNAPSHOTS = "storage_snapshots"
MB = 1024 * 1024
STORAGE_WARN_PCT = 70  # plan section 8.10: alert at 70% and 85% of the tier limit
STORAGE_ALERT_PCT = 85
GROWTH_WINDOW_DAYS = 30
PROJECTION_HORIZON_DAYS = 3650  # growth so slow that 85% is more than 10 years away has no date
NAMESPACE_NOT_FOUND = 26  # MongoDB's error code for a collection that does not exist

storage_log = logging.getLogger("nearshop.storage")


def ist_date(moment: datetime) -> date:
    """The IST calendar day of a naive UTC timestamp."""
    return moment.replace(tzinfo=UTC).astimezone(IST).date()


def collection_sizes(mongo: MongoDatabase) -> list[dict]:
    """Each collection's document count, data size (uncompressed BSON) and index size, largest first.
    Views and system collections are left out; a collection dropped while it is being read is skipped."""
    names = sorted(c["name"] for c in mongo.list_collections(filter={"type": "collection"})
                   if not c["name"].startswith("system."))
    out = []
    for name in names:
        try:
            rows = list(mongo[name].aggregate([{"$collStats": {"storageStats": {}}}]))
            stats = rows[0]["storageStats"] if rows else None
        except OperationFailure:  # a server without $collStats storageStats: the older command
            try:
                stats = mongo.command("collStats", name)
            except OperationFailure as exc:
                if exc.code != NAMESPACE_NOT_FOUND:
                    raise
                stats = None
        if not stats:
            continue
        out.append({"name": name, "documents": int(stats.get("count") or 0),
                    "data_bytes": int(stats.get("size") or 0), "index_bytes": int(stats.get("totalIndexSize") or 0)})
    out.sort(key=lambda c: (-(c["data_bytes"] + c["index_bytes"]), c["name"]))
    return out


def record_storage_snapshot(mongo: MongoDatabase, now: datetime, limit_mb: int | None = None) -> dict[str, Any]:
    """Record today's (IST) snapshot unless it exists. Cheap when it does: one lookup by _id, so the job
    can be called on every scheduler tick. A failed measurement raises; the scheduler then waits an hour
    before the next attempt (RETRY_AFTER_FAILURE in app/jobs.py). Logs a warning at 70% of the limit and
    an error (an error tracker issue) at 85%. Returns what the lease records as the job's last result."""
    day = ist_date(now).isoformat()
    coll = mongo[STORAGE_SNAPSHOTS]
    if coll.find_one({"_id": day}, {"_id": 1}) is not None:
        return {"snapshot": day, "recorded": False}
    limit_mb = limit_mb or settings.storage_limit_mb
    sizes = collection_sizes(mongo)
    data = sum(c["data_bytes"] for c in sizes)
    index_bytes = sum(c["index_bytes"] for c in sizes)
    doc = {"_id": day, "taken_at": now, "limit_mb": limit_mb, "data_bytes": data, "index_bytes": index_bytes,
           "total_bytes": data + index_bytes, "collections": sizes}
    try:
        coll.insert_one(doc)
    except DuplicateKeyError:  # another process recorded today's at the same moment
        return {"snapshot": day, "recorded": False}
    pct = 100 * doc["total_bytes"] / (limit_mb * MB)
    if pct >= STORAGE_ALERT_PCT:
        storage_log.error("database storage is above 85%% of the %d MB limit: %.1f%% used", limit_mb, pct)
    elif pct >= STORAGE_WARN_PCT:
        storage_log.warning("database storage is above 70%% of the %d MB limit: %.1f%% used", limit_mb, pct)
    return {"snapshot": day, "recorded": True, "total_mb": round(doc["total_bytes"] / MB, 1), "used_pct": round(pct, 1)}


def project_storage(points: list[tuple[date, int]], limit_bytes: int, pct: float = STORAGE_ALERT_PCT) -> dict[str, Any]:
    """Growth per day over the last GROWTH_WINDOW_DAYS days, and the day `pct`% of limit_bytes is reached.

    points are (day, total bytes), in any order. Growth is the change from the oldest point in the
    window to the latest, divided by the days between them. projection is one of:
      reached          the latest total is already at or above the threshold; the date is the latest day
      projected        the date the threshold is reached at the current growth
      not_growing      growth is zero or negative: no date
      beyond_horizon   the threshold is more than PROJECTION_HORIZON_DAYS away: no date
      insufficient     fewer than two days of snapshots in the window: no growth and no date
      no_data          no snapshot at all"""
    if not points:
        return {"growth_bytes_per_day": None, "growth_days": 0, "projection": "no_data", "projected_date": None}
    points = sorted(points)
    latest_day, latest = points[-1]
    window = [p for p in points if (latest_day - p[0]).days <= GROWTH_WINDOW_DAYS]
    base_day, base = window[0]
    days = (latest_day - base_day).days
    growth = (latest - base) / days if days > 0 else None
    target = limit_bytes * pct / 100
    if latest >= target:
        status, when = "reached", latest_day
    elif growth is None:
        status, when = "insufficient", None
    elif growth <= 0:
        status, when = "not_growing", None
    else:
        days_left = math.ceil((target - latest) / growth)
        status, when = ("projected", latest_day + timedelta(days=days_left)) if days_left <= PROJECTION_HORIZON_DAYS \
            else ("beyond_horizon", None)
    return {"growth_bytes_per_day": growth, "growth_days": days, "projection": status,
            "projected_date": when.isoformat() if when else None}


def storage_report(mongo: MongoDatabase, limit_mb: int | None = None) -> dict[str, Any]:
    """The admin storage card: the latest total against the limit, the 30-day growth per day, the
    projected date of reaching 85%, and the five largest collections."""
    limit_mb = limit_mb or settings.storage_limit_mb
    limit_bytes = limit_mb * MB
    coll = mongo[STORAGE_SNAPSHOTS]
    latest = coll.find_one(sort=[("_id", -1)])
    points: list[tuple[date, int]] = []
    if latest is not None:
        since = date.fromisoformat(latest["_id"]) - timedelta(days=GROWTH_WINDOW_DAYS)
        points = [(date.fromisoformat(s["_id"]), int(s["total_bytes"]))
                  for s in coll.find({"_id": {"$gte": since.isoformat()}}, {"total_bytes": 1})]
    projection = project_storage(points, limit_bytes)
    growth = projection["growth_bytes_per_day"]
    total = int(latest["total_bytes"]) if latest else None
    return {
        "limit_mb": limit_mb,
        "snapshot_day": latest["_id"] if latest else None,
        "taken_at": iso(latest["taken_at"]) if latest else None,
        "total_bytes": total,
        "total_mb": round(total / MB, 1) if total is not None else None,
        "data_mb": round(latest["data_bytes"] / MB, 1) if latest else None,
        "index_mb": round(latest["index_bytes"] / MB, 1) if latest else None,
        "used_pct": round(100 * total / limit_bytes, 1) if total is not None else None,
        "thresholds": [{"pct": p, "mb": round(limit_mb * p / 100, 1)} for p in (STORAGE_WARN_PCT, STORAGE_ALERT_PCT)],
        "growth_mb_per_day": round(growth / MB, 3) if growth is not None else None,
        "growth_days": projection["growth_days"],
        "projection": projection["projection"],
        "projected_85_date": projection["projected_date"],
        "largest_collections": [
            {"name": c["name"], "documents": c["documents"], "data_mb": round(c["data_bytes"] / MB, 2),
             "index_mb": round(c["index_bytes"] / MB, 2), "total_mb": round((c["data_bytes"] + c["index_bytes"]) / MB, 2)}
            for c in (latest["collections"] if latest else [])[:5]
        ],
    }
