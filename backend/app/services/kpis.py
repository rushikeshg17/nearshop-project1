"""Gate KPIs: the figures the business model's gates use, computed one way every month (P9.12; plan section 8.23;
business/BUSINESS_MODEL.md section 11.2; scorecard C6, checks 10.3 and 10.5).

The team reads every gate figure from one admin page (GET /api/admin/kpis, /admin/kpis), live and as monthly
snapshots, each beside the threshold of its next gate and pass or fail. The thresholds are business-model assumptions
and live in settings (NEARSHOP_KPI_*). The city-economics test stays in the business model's own model: nothing here
projects or models, it only counts.

Scope
-----
Every shop-based figure counts the gate city (settings.seed_city's name, matched on Shop.city ignoring case and
spaces, as P7.4's freshness report does) and never a shop the seed made (an owner on the demo email domain,
statements.demo_shop_ids). The zero-result rate counts every search (search_history has no city). Months are IST
calendar months; "the month" is the last complete one before the time the live figures are computed for, and the
snapshotted month itself for a snapshot (see Snapshots for what a snapshot measures when).

The figures (compute)
---------------------
  * active_shops: verified active shops, statements.active_shops (verified and live, with an active listing whose
    details or stock changed in the last 14 days), in the city.
  * median_stock_age_hours: P7.4's city-wide median: the hours since stock_updated_at over the active listings of
    the city's verified active (is_active) shops, as app/services/analytics.py freshness_report computes it.
  * fresh_weeks: complete ISO weeks (Monday to Sunday, IST), counted back from the last complete one, in which the
    daily reading of the median stock age (kpi_days, recorded by the kpis job) was at most
    NEARSHOP_KPI_MAX_MEDIAN_STOCK_AGE_HOURS every time, with at least NEARSHOP_KPI_WEEK_MIN_READINGS readings. A week
    that fails or has too few readings ends the run.
  * arrival_failure_pct: plan section 8.17: arrival failures ÷ (completed pickups and deliveries + arrival failures)
    over the last 90 days, shown with both counts (check 10.5). An arrival failure is a reservation or order closed
    in the window with a reason in ARRIVAL_FAILURE_REASONS and arrival_reported_at set (the customer's report, P9.1;
    P9.8 adds the owner's "out of stock after confirming" reason there and must set arrival_reported_at with it).
    Only the report sets arrival_reported_at, so a cancellation reason typed to match one (an order's reason is free
    text) is never counted. completions_90d is the completions in the same window.
  * completions_month: completed pickups (completed_at) plus delivered orders (delivered_at) in the month, with the
    current month so far beside it.
  * month2_return_pct: of the customers whose first reservation or order (at a city shop) was in the month before
    the month, the share who placed another in the month.
  * zero_result_pct: searches with 0 results ÷ all searches over the last 30 days (search_history, the P5.6 view's
    zero_result_rate_all).
  * shop_paid_pct: active shops on an active plan they pay for (source shop_paid or advance, covering the time, an
    Autopay hold included) ÷ active shops. sponsored_seats (a distributor's seats) are shown separately and never
    counted. Plan section 8.23 measures it at month end: a snapshot takes it, and the active shops it divides by, from
    the month-end reading (below); the live value is measured now, and each says when in detail.measured_at.
  * founding_renewal_pct: of the founding subscriptions (founding_slot set) whose first year ended in the last 12
    months and at least 30 days ago, the share renewed within 30 days of that end: a renewal of the subscription
    (plans.renew always charges the list price in force) or a new paid subscription for the shop. The first year ends
    at the end date before the first renewal (credits and extensions included), or at the current end date of one
    never renewed. Those still inside their 30 days are shown as pending and not counted.
  * annual_inactivity_pct: of the active shops recorded in the snapshot of the month 12 months before the month,
    the share not active now. None until that snapshot exists.
  * partner_plans: paying Partner plans; 0 until Partner plans exist (P10.3).
  * accurate_pct and advances_paid, the readout figures (B25): of the city's statements for the latest month stated,
    the share whose shop's latest Accurate or Not accurate answer was Accurate (as statements.admin_summary counts
    it); and the city's advance subscriptions that were paid and not cancelled or refunded.
  * activation_cost_rupees and support_minutes cannot be computed: admins enter them each month (record_input), from
    R6's cost log and the B12 ticket log, each with a source note. The latest entry is shown with when it was entered.

Thresholds (next_threshold)
---------------------------
GATES lists the gates with their dates, THRESHOLDS each figure's value at each gate that has one. A figure is judged
against its next gate: the first one on or after today (IST) that has a threshold for it, or its last one once all
have passed. pass is None when the figure has no value yet.

Snapshots
---------
The kpis job (app/jobs.py), every tick under its lease, records a daily reading once per IST day (record_day: the median
stock age, the active shop ids and the shop-paid and sponsored counts) and, on the 2nd of each month from SNAPSHOT_AT
IST or on the first tick after it, stores the previous month's snapshot (take_snapshot). A snapshot holds its own
month's figures whenever it is taken (compute with for_month), each with its threshold and pass, the latest entered inputs
and the active shop ids (for annual inactivity a year later):
  * the active shops, the shop-paid share, the sponsored seats and the median stock age come from the month-end
    reading, the reading of the 1st of the next month taken on the job's first tick after midnight IST. Without one
    they are measured when the snapshot is taken if that is still in the next month (detail.measured_at says when),
    and are left empty (None) once it is later, rather than stored with a later month's values;
  * completions in the month, month-2 return and annual inactivity count the snapshotted month;
  * the 90-day, 30-day, fresh-weeks and founding-renewal windows end at the month's end;
  * the readout figures and the entered inputs are as they stand when the snapshot is taken. A snapshot is only ever inserted, under the
month as its _id: a second run for the month leaves the first unchanged, and nothing in the app updates or deletes
one, so later code changes cannot rewrite history. Nothing is recorded from demo data (statements.demo_data).
"""
from __future__ import annotations

import csv
import io
import re
import statistics
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import AppError
from app.models.billing import PAID_SOURCES, Period, SubscriptionSource, SubscriptionStatus
from app.models.enums import OrderStatus, ReservationStatus
from app.services import leads, plans, statements
from app.services.meta import city_config
from app.services.reservations import NOT_AVAILABLE_ON_ARRIVAL

SNAPSHOTS = "kpi_snapshots"
DAYS = "kpi_days"
INPUTS = "kpi_inputs"

SNAPSHOT_DAY = 2  # of each month
SNAPSHOT_AT = time(6, 0)  # IST
SNAPSHOTS_SHOWN = 12
ARRIVAL_WINDOW_DAYS = 90
ZERO_RESULT_WINDOW_DAYS = 30
RENEWAL_GRACE_DAYS = 30
RENEWAL_WINDOW_DAYS = 365
INACTIVITY_MONTHS = 12
WEEKS_LOOKED_BACK = 104
NOTE_MIN, NOTE_MAX = 10, 500
INPUT_MAX = 1_000_000
# A reservation or order closed with one of these reasons is an arrival failure (plan section 8.17).
ARRIVAL_FAILURE_REASONS: tuple[str, ...] = (NOT_AVAILABLE_ON_ARRIVAL,)

ENTERED = ("activation_cost_rupees", "support_minutes")
ENTERED_SOURCES = {"activation_cost_rupees": "R6's activation cost log",
                   "support_minutes": "the B12 support ticket log"}

# (key, name, settings attribute holding its date)
GATES: tuple[tuple[str, str, str], ...] = (
    ("g4", "G4 (checks 10.3 and 10.5)", "kpi_g4_date"),
    ("readout", "Willingness-to-pay readout (B25)", "kpi_readout_date"),
    ("ballari", "Ballari gate", "kpi_ballari_gate_date"),
)

# key -> (label, unit, group)
FIGURES: dict[str, tuple[str, str, str]] = {
    "active_shops": ("Verified active shops", "count", "Supply"),
    "median_stock_age_hours": ("Median stock age", "hours", "Supply"),
    "fresh_weeks": ("Consecutive weeks at 48 hours or less", "weeks", "Supply"),
    "arrival_failure_pct": ("Arrival-failure rate, last 90 days", "pct", "Reliability (check 10.5)"),
    "completions_90d": ("Completions, last 90 days", "count", "Reliability (check 10.5)"),
    "completions_month": ("Completions in the month", "count", "Demand"),
    "month2_return_pct": ("Customers returning in month 2", "pct", "Demand"),
    "zero_result_pct": ("Live zero-result rate, last 30 days", "pct", "Demand"),
    "shop_paid_pct": ("Shop-paid share of active shops", "pct", "Revenue"),
    "sponsored_seats": ("Sponsored seats (shown, not counted)", "count", "Revenue"),
    "founding_renewal_pct": ("Founding cohort list-price renewal", "pct", "Revenue"),
    "annual_inactivity_pct": ("Annual inactivity", "pct", "Revenue"),
    "partner_plans": ("Paying Partner plans", "count", "Revenue"),
    "accurate_pct": ("Active shops answering Accurate to the latest statement", "pct", "Readout (B25)"),
    "advances_paid": ("Advance subscriptions paid", "count", "Readout (B25)"),
    "activation_cost_rupees": ("Activation cost per activated shop", "rupees", "Entered by hand"),
    "support_minutes": ("Support minutes per active shop per month", "minutes", "Entered by hand"),
}

# key -> [(gate, op, settings attribute)]
THRESHOLDS: dict[str, list[tuple[str, str, str]]] = {
    "active_shops": [("g4", ">=", "kpi_g4_min_active_shops"), ("ballari", ">=", "kpi_ballari_min_active_shops")],
    "median_stock_age_hours": [("g4", "<=", "kpi_max_median_stock_age_hours"),
                               ("ballari", "<=", "kpi_max_median_stock_age_hours")],
    "fresh_weeks": [("g4", ">=", "kpi_g4_min_fresh_weeks"), ("ballari", ">=", "kpi_ballari_min_fresh_weeks")],
    "arrival_failure_pct": [("g4", "<=", "kpi_max_arrival_failure_pct"),
                            ("ballari", "<=", "kpi_max_arrival_failure_pct")],
    "completions_90d": [("g4", ">=", "kpi_g4_min_completions_90d")],
    "completions_month": [("ballari", ">=", "kpi_ballari_min_completions_month")],
    "month2_return_pct": [("ballari", ">=", "kpi_ballari_min_month2_return_pct")],
    "zero_result_pct": [("ballari", "<=", "kpi_max_zero_result_pct")],
    "shop_paid_pct": [("ballari", ">=", "kpi_ballari_min_shop_paid_pct")],
    "founding_renewal_pct": [("ballari", ">=", "kpi_ballari_min_renewal_pct")],
    "annual_inactivity_pct": [("ballari", "<=", "kpi_ballari_max_annual_inactivity_pct")],
    "partner_plans": [("ballari", ">=", "kpi_ballari_min_partner_plans")],
    "accurate_pct": [("readout", ">=", "kpi_readout_min_accurate_pct")],
    "advances_paid": [("readout", ">=", "kpi_readout_min_advances")],
    "activation_cost_rupees": [("ballari", "<=", "kpi_ballari_max_activation_cost_rupees")],
    "support_minutes": [("ballari", "<=", "kpi_max_support_minutes")],
}

ASSUMPTIONS_NOTE = ("The thresholds are the business model's gate values (business/BUSINESS_MODEL.md section 11.2, "
                    "and checks 10.3 and 10.5). They are assumptions under test, held in settings, not facts.")


class KpiInputRefused(AppError):
    status_code = 422
    code = "kpi_input_refused"


# ---------------------------------------------------------------- gates and thresholds


def gates() -> list[dict[str, Any]]:
    return [{"key": key, "name": name, "date": getattr(settings, attr).isoformat()} for key, name, attr in GATES]


def next_threshold(key: str, today: date) -> dict[str, Any] | None:
    """The threshold of the figure's next gate: the first gate on or after today with one, else its last gate."""
    dated = {g: (name, getattr(settings, attr)) for g, name, attr in GATES}
    rows = sorted(THRESHOLDS.get(key, []), key=lambda t: dated[t[0]][1])
    if not rows:
        return None
    gate, op, attr = next((r for r in rows if dated[r[0]][1] >= today), rows[-1])
    name, when = dated[gate]
    return {"gate": gate, "gate_name": name, "gate_date": when.isoformat(), "op": op,
            "value": getattr(settings, attr)}


def judge(value: float | None, threshold: dict[str, Any] | None) -> bool | None:
    if value is None or threshold is None:
        return None
    return value >= threshold["value"] if threshold["op"] == ">=" else value <= threshold["value"]


def _pct(part: int, whole: int) -> float | None:
    return 100 * part / whole if whole else None


def _round(value: float | None) -> float | None:
    return round(value, 2) if isinstance(value, float) else value


# ---------------------------------------------------------------- scope


def default_city() -> str:
    return city_config()["name"]


def city_key(city: str) -> str:
    return " ".join(city.split()).casefold()


def _city_filter(city: str) -> dict[str, Any]:
    return {"city": {"$regex": rf"^\s*{re.escape(city.strip())}\s*$", "$options": "i"}}


def city_shop_ids(db: Database, city: str) -> list[int]:
    """Every shop in the city, whatever its status, leaving out the seed's shops."""
    demo = statements.demo_shop_ids(db)
    return [i for i in db.shops.distinct("_id", _city_filter(city)) if i not in demo]


def active_shop_ids(db: Database, at: datetime, city: str) -> list[int]:
    """Verified active shops in the city (statements.active_shops: verified, live, an active listing changed in the
    last 14 days; the seed's shops left out)."""
    key = city_key(city)
    return sorted(s.id for s in statements.active_shops(db, at) if city_key(s.city or "") == key)


def _month(at: datetime) -> tuple[int, int]:
    """The last complete IST month before at."""
    day = leads.ist_day(at)
    return leads.previous_month(day.year, day.month)


# ---------------------------------------------------------------- supply


def median_stock_age(db: Database, at: datetime, city: str) -> tuple[float | None, int]:
    """P7.4's city-wide median (analytics.freshness_report's city_median_age_hours): hours since stock_updated_at
    over the active listings of the city's verified, active shops. Returns (median, listings)."""
    shop_ids = db.shops.distinct("_id", {"is_active": True, "is_verified": True, **_city_filter(city)})
    demo = statements.demo_shop_ids(db)
    shop_ids = [i for i in shop_ids if i not in demo]
    ages = [max((at - r["stock_updated_at"]).total_seconds() / 3600, 0.0)
            for r in db.products.find_raw({"is_active": True, "shop_id": {"$in": shop_ids}},
                                          {"stock_updated_at": 1})
            if r.get("stock_updated_at") is not None] if shop_ids else []
    return (statistics.median(ages) if ages else None), len(ages)


def record_day(db: Database, now: datetime, city: str | None = None) -> dict[str, Any]:
    """The day's reading of the median stock age, the active shops and the shop-paid count, once per IST day and city:
    the first reading of a day stands. The reading of the 1st, taken on the first tick after midnight IST, is the
    previous month's month-end reading (month_end_reading)."""
    city = city or default_city()
    day = leads.ist_day(now)
    med, listings = median_stock_age(db, now, city)
    active = active_shop_ids(db, now, city)
    paid = shop_paid(db, active, now)
    doc = {"_id": f"{city_key(city)}:{day.isoformat()}", "city": city_key(city), "day": day.isoformat(),
           "median_stock_age_hours": round(med, 2) if med is not None else None, "listings": listings,
           "active_shops": len(active), "active_shop_ids": active, "paid": paid["paid"],
           "sponsored": paid["sponsored"], "recorded_at": now}
    try:
        db.mongo[DAYS].insert_one(doc)
    except DuplicateKeyError:
        return {"day": day.isoformat(), "recorded": False}
    return {"day": day.isoformat(), "recorded": True, "median_stock_age_hours": doc["median_stock_age_hours"]}


def fresh_weeks(db: Database, at: datetime, city: str) -> tuple[int, str | None]:
    """Consecutive complete ISO weeks (IST), back from the last complete one, whose daily readings were all at most the
    threshold, each with at least kpi_week_min_readings readings. Returns (weeks, the last complete week's Monday)."""
    today = leads.ist_day(at)
    week_start = today - timedelta(days=today.weekday() + 7)  # Monday of the last complete week
    earliest = week_start - timedelta(weeks=WEEKS_LOOKED_BACK)
    readings: dict[date, list[float | None]] = {}
    for r in db.mongo[DAYS].find({"city": city_key(city), "day": {"$gte": earliest.isoformat(),
                                                                   "$lt": (week_start + timedelta(days=7)).isoformat()}},
                                 {"day": 1, "median_stock_age_hours": 1}):
        d = date.fromisoformat(r["day"])
        readings.setdefault(d - timedelta(days=d.weekday()), []).append(r.get("median_stock_age_hours"))
    weeks, monday = 0, week_start
    limit = settings.kpi_max_median_stock_age_hours
    while monday >= earliest:
        values = readings.get(monday, [])
        if len(values) < settings.kpi_week_min_readings or any(v is None or v > limit for v in values):
            break
        weeks += 1
        monday -= timedelta(weeks=1)
    return weeks, week_start.isoformat()


# ---------------------------------------------------------------- reliability and demand


def completions(db: Database, shop_ids: list[int], start: datetime, end: datetime) -> dict[str, int]:
    pickups = db.reservations.count({"shop_id": {"$in": shop_ids}, "status": ReservationStatus.COMPLETED.value,
                                     "completed_at": {"$gte": start, "$lt": end}})
    deliveries = db.orders.count({"shop_id": {"$in": shop_ids}, "status": OrderStatus.DELIVERED.value,
                                  "delivered_at": {"$gte": start, "$lt": end}})
    return {"pickups": pickups, "deliveries": deliveries, "total": pickups + deliveries}


def arrival_failures(db: Database, shop_ids: list[int], start: datetime, end: datetime) -> int:
    """Reservations and orders closed in [start, end) as an arrival failure: the reason is one of
    ARRIVAL_FAILURE_REASONS and arrival_reported_at is set. Only the arrival report itself sets arrival_reported_at,
    so a cancellation reason typed to look like one is never counted."""
    flt = {"close_reason": {"$in": list(ARRIVAL_FAILURE_REASONS)}, "arrival_reported_at": {"$ne": None},
           "closed_at": {"$gte": start, "$lt": end}, "shop_id": {"$in": shop_ids}}
    return db.reservations.count(flt) + db.orders.count(flt)


def arrival_failure_rate(db: Database, shop_ids: list[int], at: datetime) -> dict[str, Any]:
    """Plan section 8.17 over the last ARRIVAL_WINDOW_DAYS: failures ÷ (completions + failures)."""
    start = at - timedelta(days=ARRIVAL_WINDOW_DAYS)
    done = completions(db, shop_ids, start, at)
    failed = arrival_failures(db, shop_ids, start, at)
    return {"failures": failed, "completions": done["total"], "pickups": done["pickups"],
            "deliveries": done["deliveries"], "denominator": done["total"] + failed,
            "rate": _pct(failed, done["total"] + failed), "since": start.isoformat(), "days": ARRIVAL_WINDOW_DAYS}


def _customers(db: Database, shop_ids: list[int], start: datetime, end: datetime) -> set[int]:
    flt = {"shop_id": {"$in": shop_ids}, "created_at": {"$gte": start, "$lt": end}}
    return set(db.reservations.distinct("customer_id", flt)) | set(db.orders.distinct("customer_id", flt))


def month2_return(db: Database, shop_ids: list[int], year: int, month: int) -> dict[str, Any]:
    """Of the customers whose first reservation or order was in the month before (year, month), the share who placed
    another in (year, month)."""
    cy, cm = leads.previous_month(year, month)
    c_start, c_end = leads.month_bounds(cy, cm)
    start, end = leads.month_bounds(year, month)
    seen = _customers(db, shop_ids, c_start, c_end)
    if seen:
        earlier = {"shop_id": {"$in": shop_ids}, "customer_id": {"$in": list(seen)}, "created_at": {"$lt": c_start}}
        seen -= set(db.reservations.distinct("customer_id", earlier)) | set(db.orders.distinct("customer_id", earlier))
    returned = seen & _customers(db, shop_ids, start, end) if seen else set()
    return {"cohort_month": leads.month_key(cy, cm), "return_month": leads.month_key(year, month),
            "cohort": len(seen), "returned": len(returned), "rate": _pct(len(returned), len(seen))}


def zero_result_rate(db: Database, at: datetime) -> dict[str, Any]:
    start = at - timedelta(days=ZERO_RESULT_WINDOW_DAYS)
    window = {"created_at": {"$gte": start, "$lt": at}}
    total = db.search_history.count(window)
    zero = db.search_history.count({**window, "results_count": 0})
    return {"searches": total, "zero": zero, "rate": _pct(zero, total), "days": ZERO_RESULT_WINDOW_DAYS}


# ---------------------------------------------------------------- revenue


def _covering(db: Database, shop_ids: list[int], at: datetime) -> dict[int, Any]:
    """Each shop's active subscription covering at (plans.active_subscription, in two queries)."""
    subs = plans.subscriptions(db)
    out: dict[int, Any] = {}
    for sub in subs.find({"shop_ids": {"$in": shop_ids}, "status": SubscriptionStatus.ACTIVE.value,
                          "starts_at": {"$lte": at}, "ends_at": {"$gt": at}}, sort=[("ends_at", 1)]):
        for sid in sub.shop_ids:
            out[sid] = sub
    held = subs.find({"shop_ids": {"$in": shop_ids}, "status": SubscriptionStatus.ACTIVE.value,
                      "period": Period.MONTH.value,
                      "ends_at": {"$lte": at, "$gt": at - timedelta(days=settings.autopay_grace_days)}})
    for sub in held:
        if plans.autopay_hold(db, sub, at):
            for sid in sub.shop_ids:
                out.setdefault(sid, sub)
    return out


def shop_paid(db: Database, active: list[int], at: datetime) -> dict[str, Any]:
    covering = _covering(db, active, at) if active else {}
    paid = sum(1 for sid in active if sid in covering and covering[sid].source in PAID_SOURCES)
    sponsored = sum(1 for sid in active if sid in covering and covering[sid].source == SubscriptionSource.SPONSORED)
    return {"paid": paid, "sponsored": sponsored, "active": len(active), "rate": _pct(paid, len(active))}


def founding_renewal(db: Database, shop_ids: list[int], at: datetime) -> dict[str, Any]:
    """Of the founding subscriptions whose first year ended in the last RENEWAL_WINDOW_DAYS and at least
    RENEWAL_GRACE_DAYS ago, the share renewed (at list price) within RENEWAL_GRACE_DAYS of that end."""
    grace = timedelta(days=RENEWAL_GRACE_DAYS)
    start = at - timedelta(days=RENEWAL_WINDOW_DAYS)
    cohort = renewed = pending = 0
    subs = plans.subscriptions(db).find({"founding_slot": {"$ne": None}, "shop_ids": {"$in": shop_ids}})
    for sub in subs:
        first_renewal = plans.events(db).find_one({"subscription_id": sub.id, "kind": "renewed"},
                                                  sort=[("at", 1), ("_id", 1)])
        if first_renewal is not None:
            first_end = first_renewal.ends_at_before or sub.ends_at
        elif sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.LAPSED):
            first_end = sub.ends_at
        else:
            continue  # not started (an advance), or cancelled or refunded before its first year ended
        if not start <= first_end <= at:
            continue
        if first_end + grace > at:
            pending += 1  # still inside its 30 days: counted once they are over
            continue
        renewed_in_time = first_renewal is not None and first_renewal.at <= first_end + grace
        if not renewed_in_time:
            renewed_in_time = plans.subscriptions(db).exists({
                "_id": {"$ne": sub.id}, "shop_ids": sub.founding_shop_id or sub.shop_ids[0],
                "source": {"$in": [s.value for s in PAID_SOURCES]},
                "status": {"$nin": [SubscriptionStatus.CANCELLED.value, SubscriptionStatus.REFUNDED.value]},
                "paid_at": {"$gt": sub.created_at, "$lte": first_end + grace}})
        cohort += 1
        renewed += renewed_in_time
    return {"cohort": cohort, "renewed": renewed, "pending": pending, "rate": _pct(renewed, cohort)}


def annual_inactivity(db: Database, active: Iterable[int], year: int, month: int) -> dict[str, Any]:
    """Of the active shops in the snapshot of the month INACTIVITY_MONTHS before (year, month), the share not active
    now."""
    y, m = year - INACTIVITY_MONTHS // 12, month
    earlier = leads.month_key(y, m)
    snap = db.mongo[SNAPSHOTS].find_one({"_id": earlier}, {"active_shop_ids": 1})
    if snap is None:
        return {"from_month": earlier, "then_active": None, "inactive": None, "rate": None}
    before = set(snap.get("active_shop_ids") or [])
    gone = before - set(active)
    return {"from_month": earlier, "then_active": len(before), "inactive": len(gone), "rate": _pct(len(gone), len(before))}


# ---------------------------------------------------------------- the readout (B25)


def accurate_share(db: Database, shop_ids: list[int]) -> dict[str, Any]:
    """The latest month stated: of the city's statements, the share whose latest Accurate or Not accurate answer was
    Accurate (as statements.admin_summary counts it)."""
    latest = db.mongo[statements.STATEMENTS].find_one({"shop_id": {"$in": shop_ids}}, {"month": 1},
                                                      sort=[("month", -1)])
    if latest is None:
        return {"month": None, "statements": 0, "accurate": 0, "rate": None}
    month = latest["month"]
    ids = [r["_id"] for r in db.mongo[statements.STATEMENTS].find({"month": month, "shop_id": {"$in": shop_ids}},
                                                                   {"_id": 1})]
    answers: dict[int, tuple[datetime, int, str]] = {}
    for r in db.mongo[statements.REPLIES].find(
            {"statement_id": {"$in": ids}, "kind": {"$in": [statements.ACCURATE, statements.NOT_ACCURATE]}},
            {"statement_id": 1, "kind": 1, "at": 1}):
        key = (r["at"], r["_id"], r["kind"])
        if r["statement_id"] not in answers or key > answers[r["statement_id"]]:
            answers[r["statement_id"]] = key
    accurate = sum(1 for _, _, kind in answers.values() if kind == statements.ACCURATE)
    return {"month": month, "statements": len(ids), "accurate": accurate, "rate": _pct(accurate, len(ids))}


def advances_paid(db: Database, shop_ids: list[int]) -> int:
    return plans.subscriptions(db).count({
        "source": SubscriptionSource.ADVANCE.value, "shop_ids": {"$in": shop_ids}, "paid_at": {"$ne": None},
        "status": {"$nin": [SubscriptionStatus.CANCELLED.value, SubscriptionStatus.REFUNDED.value]}})


# ---------------------------------------------------------------- entered by hand


def _month_key(value: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise KpiInputRefused("The month must be YYYY-MM")
    return value


def record_input(db: Database, *, key: str, month: str, value: float, source_note: str, admin_id: int,
                 now: datetime | None = None) -> dict[str, Any]:
    """An admin's monthly entry of a figure that cannot be computed. A source note (NOTE_MIN to NOTE_MAX characters)
    is required. Entries are only added: a correction is a new entry, and the latest one is shown."""
    if key not in ENTERED:
        raise KpiInputRefused(f"{key} is not entered by hand")
    note = " ".join((source_note or "").split())
    if len(note) < NOTE_MIN:
        raise KpiInputRefused(f"Say where the figure comes from ({NOTE_MIN} characters or more)")
    if len(note) > NOTE_MAX:
        raise KpiInputRefused(f"The source note is longer than {NOTE_MAX} characters")
    if not 0 <= value <= INPUT_MAX:
        raise KpiInputRefused(f"The value must be between 0 and {INPUT_MAX}")
    now = now or utcnow()
    doc = {"_id": db.next_id(INPUTS), "key": key, "month": _month_key(month), "value": float(value),
           "source_note": note, "entered_by": admin_id, "entered_at": now}
    db.mongo[INPUTS].insert_one(doc)
    return input_out(doc)


def input_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {"id": doc["_id"], "key": doc["key"], "month": doc["month"], "value": doc["value"],
            "source_note": doc["source_note"], "entered_by": doc.get("entered_by"),
            "entered_at": doc["entered_at"].isoformat()}


def latest_inputs(db: Database) -> dict[str, dict[str, Any] | None]:
    out: dict[str, dict[str, Any] | None] = {}
    for key in ENTERED:
        doc = db.mongo[INPUTS].find_one({"key": key}, sort=[("entered_at", -1), ("_id", -1)])
        out[key] = input_out(doc) if doc else None
    return out


# ---------------------------------------------------------------- all figures


def month_end_reading(db: Database, year: int, month: int, city: str) -> dict[str, Any] | None:
    """The month's month-end reading: record_day's reading of the 1st of the next month (taken on the job's first tick
    after midnight IST), or None if there is none (or it predates the active shops being recorded)."""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    doc = db.mongo[DAYS].find_one({"_id": f"{city_key(city)}:{date(ny, nm, 1).isoformat()}"})
    return doc if doc is not None and "active_shop_ids" in doc else None


def _figure(key: str, value: float | None, today: date, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    label, unit, group = FIGURES[key]
    threshold = next_threshold(key, today)
    return {"key": key, "label": label, "unit": unit, "group": group, "value": _round(value),
            "threshold": threshold, "pass": judge(value, threshold), "detail": detail or {}}


def compute(db: Database, at: datetime | None = None, city: str | None = None,
            for_month: tuple[int, int] | None = None) -> dict[str, Any]:
    """Every figure for the city (default the gate city), each with its next gate's threshold and pass or fail.

    Live (for_month None): every figure at `at` (default now), for the last complete month before it.
    For a month's snapshot (for_month given): the figures of that month whatever `at` is. The monthly figures count that
    month; the trailing windows (90 and 30 days, fresh weeks, founding renewal) end at its end; the active shops, the
    shop-paid share, the sponsored seats and the median stock age come from its month-end reading. Without that
    reading they are measured at `at` while `at` is still in the next month (measured_at says when), and are None
    once it is later. The readout figures and the entered inputs are as they stand at `at`."""
    at = at or utcnow()
    city = city or default_city()
    today = leads.ist_day(at)
    snapshot = for_month is not None
    year, month = for_month or _month(at)
    m_start, m_end = leads.month_bounds(year, month)
    ref = m_end if snapshot else at
    shops = city_shop_ids(db, city)
    reading = month_end_reading(db, year, month, city) if snapshot else None
    if reading is not None:
        active = sorted(reading["active_shop_ids"])
        measured_at: datetime | None = reading["recorded_at"]
        med, listings = reading.get("median_stock_age_hours"), reading.get("listings", 0)
        paid = {"paid": reading["paid"], "sponsored": reading["sponsored"], "active": len(active),
                "rate": _pct(reading["paid"], len(active))}
    elif not snapshot or _month(at) == (year, month):
        active = active_shop_ids(db, at, city)
        measured_at = at
        med, listings = median_stock_age(db, at, city)
        paid = shop_paid(db, active, at)
    else:  # too late to measure the month's end: unknown, not a later month's figures
        active, measured_at, med, listings = [], None, None, 0
        paid = {"paid": None, "sponsored": None, "active": None, "rate": None}
    when = {"measured_at": measured_at.isoformat() if measured_at else None,
            "month_end_reading": reading is not None} if snapshot else {"measured_at": at.isoformat()}
    paid = {**paid, **when}
    weeks, last_week = fresh_weeks(db, ref, city)
    arrival = arrival_failure_rate(db, shops, ref)
    month_done = completions(db, shops, m_start, m_end)
    so_far = None if snapshot else completions(db, shops, m_end, at)["total"]
    ret = month2_return(db, shops, year, month)
    zero = zero_result_rate(db, ref)
    renewal = founding_renewal(db, shops, ref)
    inactive = annual_inactivity(db, active, year, month) if measured_at else \
        {"from_month": None, "then_active": None, "inactive": None, "rate": None}
    readout = accurate_share(db, shops)
    advances = advances_paid(db, shops)
    entered = latest_inputs(db)
    figures = [
        _figure("active_shops", len(active) if measured_at else None, today, when),
        _figure("median_stock_age_hours", med, today, {"listings": listings, **when}),
        _figure("fresh_weeks", weeks, today, {"last_complete_week": last_week,
                                              "min_readings": settings.kpi_week_min_readings}),
        _figure("arrival_failure_pct", arrival["rate"], today, arrival),
        _figure("completions_90d", arrival["completions"], today, {"since": arrival["since"]}),
        _figure("completions_month", month_done["total"], today,
                {**month_done, "month": leads.month_key(year, month), "this_month_so_far": so_far}),
        _figure("month2_return_pct", ret["rate"], today, ret),
        _figure("zero_result_pct", zero["rate"], today, zero),
        _figure("shop_paid_pct", paid["rate"], today, paid),
        _figure("sponsored_seats", paid["sponsored"], today, when),
        _figure("founding_renewal_pct", renewal["rate"], today, renewal),
        _figure("annual_inactivity_pct", inactive["rate"], today, inactive),
        _figure("partner_plans", 0, today, {"note": "Partner plans arrive with P10.3."}),
        _figure("accurate_pct", readout["rate"], today, readout),
        _figure("advances_paid", advances, today),
        *(_figure(key, entered[key]["value"] if entered[key] else None, today, entered[key] or {})
          for key in ENTERED),
    ]
    return {"as_of": at.isoformat(), "city": city, "month": leads.month_key(year, month), "figures": figures,
            "active_shop_ids": active}


# ---------------------------------------------------------------- snapshots


def snapshot_month(now: datetime) -> tuple[int, int]:
    """The month the kpis job snapshots at now: the one before the most recent slot (the 2nd at SNAPSHOT_AT IST)."""
    local = leads.ist_day(now)
    year, month = local.year, local.month
    slot = leads.ist_start(date(year, month, SNAPSHOT_DAY)) + timedelta(hours=SNAPSHOT_AT.hour,
                                                                        minutes=SNAPSHOT_AT.minute)
    if now < slot:
        year, month = leads.previous_month(year, month)
    return leads.previous_month(year, month)


def take_snapshot(db: Database, year: int, month: int, now: datetime | None = None,
                  city: str | None = None) -> dict[str, Any]:
    """Store the month's snapshot once, holding that month's figures (compute with for_month) whenever it is taken. A
    snapshot already stored for the month is left exactly as it is."""
    now = now or utcnow()
    key = leads.month_key(year, month)
    if db.mongo[SNAPSHOTS].find_one({"_id": key}, {"_id": 1}) is not None:
        return {"month": key, "taken": False}
    live = compute(db, now, city, for_month=(year, month))
    doc = {"_id": key, "month": key, "taken_at": now, "city": live["city"], "figures": live["figures"],
           "active_shop_ids": live["active_shop_ids"]}
    try:
        db.mongo[SNAPSHOTS].insert_one(doc)
    except DuplicateKeyError:  # another process took it a moment ago: that one stands
        return {"month": key, "taken": False}
    return {"month": key, "taken": True}


def snapshot_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {"month": doc["month"], "taken_at": doc["taken_at"].isoformat(), "city": doc.get("city"),
            "figures": doc["figures"]}


def snapshots(db: Database, limit: int = SNAPSHOTS_SHOWN) -> list[dict[str, Any]]:
    """The latest snapshots, newest first."""
    return [snapshot_out(d) for d in db.mongo[SNAPSHOTS].find({}, {"active_shop_ids": 0},
                                                                sort=[("_id", -1)], limit=limit)]


def report(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """The admin page: the live figures, the last SNAPSHOTS_SHOWN snapshots, the entered inputs and the gates."""
    live = compute(db, now)
    live.pop("active_shop_ids")
    return {"live": live, "snapshots": snapshots(db), "inputs": latest_inputs(db),
            "entered": [{"key": k, "label": FIGURES[k][0], "unit": FIGURES[k][1], "source": ENTERED_SOURCES[k]}
                        for k in ENTERED],
            "gates": gates(), "note": ASSUMPTIONS_NOTE}


CSV_COLUMNS = ("month", "taken_at", "city", "figure", "label", "value", "unit", "gate", "gate_date", "op",
               "threshold", "pass")


def snapshots_csv(db: Database) -> str:
    """Every snapshot, oldest first, one row per figure."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for doc in db.mongo[SNAPSHOTS].find({}, {"active_shop_ids": 0}, sort=[("_id", 1)]):
        for f in doc["figures"]:
            t = f.get("threshold") or {}
            ok = f.get("pass")
            writer.writerow([doc["month"], doc["taken_at"].isoformat(), doc.get("city") or "", f["key"], f["label"],
                             "" if f.get("value") is None else f["value"], f["unit"], t.get("gate_name", ""),
                             t.get("gate_date", ""), t.get("op", ""), t.get("value", ""),
                             "" if ok is None else ("pass" if ok else "fail")])
    return buf.getvalue()


# ---------------------------------------------------------------- the job


def run_job(db: Database, now: datetime, last: dict[str, Any]) -> dict[str, Any]:
    """The kpis job's work for one tick (app/jobs.py): the day's reading once per IST day (reading_for) and the
    previous month's snapshot from the 2nd at SNAPSHOT_AT IST (snapshot_for). Nothing from demo data."""
    if statements.demo_data():
        return {"skipped": "demo data"}
    today = leads.ist_day(now).isoformat()
    year, month = snapshot_month(now)
    key = leads.month_key(year, month)
    result: dict[str, Any] = {"reading_for": last.get("reading_for"), "snapshot_for": last.get("snapshot_for")}
    if last.get("reading_for") != today:
        record_day(db, now)
        result["reading_for"] = today
    if last.get("snapshot_for") != key:
        result["snapshot"] = take_snapshot(db, year, month, now)
        result["snapshot_for"] = key
    return result
