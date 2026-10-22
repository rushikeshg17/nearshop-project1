"""Lead events and monthly lead counts per shop (P9.2; plan sections 8.3, 8.4 and 8.18).

NearShop tells each shop, from counts alone, how many customers it sent them each month. It never states a rupee
value of a shop's own sales: it cannot measure one.

Lead events
-----------
POST /api/leads (app/api/routes/catalog.py) records a tap on a shop's Call or Directions link, on the shop page or a
product page (the listing then too). The pages send it with fetch keepalive just before following the link
(frontend/src/lib/api.ts recordLead), so a failed or slow call never blocks the dialler or the map.

  * lead_events rows hold only the shop, the listing (or none), the kind (call_tap or directions_tap) and the IST day
    (day: the start of that IST day as a UTC time), under an integer _id from counters. Never a user id, phone number,
    IP address, user agent or the time of the tap; the collection's validator refuses any other field. The TTL index
    on day removes a row 90 days after its day.
  * Each visitor counts once per shop, kind and IST day. The visitor key is a hash of the client IP (an IPv6 address
    by its /64, as the rate limits count it), the user agent and the day's salt, with the shop, kind and day; it is
    kept only as the _id of a lead_dedupe row, which the TTL index removes 48 hours later, never on the event. The
    dedupe row and the event are written in one transaction, so a tap counts once or not at all.
  * The salt is random and replaced every IST day: one lead_salts row per day, removed by its TTL index an hour after
    that day ends. Once it is gone a dedupe hash can no longer be matched to any visitor.
  * The endpoint is limited to 30 per IP per minute (app/core/ratelimit.py LEAD_EVENT).

Monthly counts
--------------
The lead_counts job (app/jobs.py), on the 1st at 02:00 IST, writes one lead_counts row per shop for the previous
month (rollup_month): every shop live at the time, and every other shop with any activity in that month. A re-run
replaces that month's rows. The fields are COUNT_FIELDS:

  * call_taps and directions_taps: lead events in the month;
  * reservation_requests (reservations created), reservations_confirmed (confirmed_at in the month) and
    pickups_completed (completed reservations, completed_at in the month);
  * delivery_orders (orders created) and deliveries_completed (delivered orders, delivered_at in the month);
  * customer_requests: reservation requests plus delivery orders from customers whose phone is not the shop's, the
    owner's or that of anyone in the shop's staff list (shop_staff, which P9.13 adds; empty until then). A delivery
    order whose contact phone is one of those is left out too. Phones compare by their last ten digits.

Months are IST calendar months. lead_counts rows are business records kept 8 years (plan section 8.4; confirm with
the CA): keep_until is 8 years after the month ends, and the TTL index removes a row then. To keep rows longer, move
keep_until; the index need not change.

month_counts is the one function later prompts use (statements P9.9, the outcome guarantee P9.4, the KPI report
P9.12): a shop's counts for a month, the current month so far included.

Facilitated sales
-----------------
facilitated_sales gives, for an Indian financial year (1 April to 31 March, IST), each shop's completed pickups and
deliveries and their total value, for the CA's review of TDS on e-commerce sales (business model risk R22): a pickup
is worth its quantity times its unit price, a delivery its order total (the goods and the shop's delivery fee, what
the customer paid the shop). database/admin_tasks.py facilitated-sales writes it as a CSV.
"""
from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, utcnow
from app.core.ratelimit import ip_key
from app.models.core import live_shop_filter
from app.models.enums import OrderStatus, ReservationStatus
from app.services.reviews import phone_key  # the last ten digits, as P3.5 compares phones

IST = ZoneInfo("Asia/Kolkata")

CALL_TAP, DIRECTIONS_TAP = "call_tap", "directions_tap"
LEAD_KINDS = (CALL_TAP, DIRECTIONS_TAP)

EVENTS = "lead_events"
DEDUPE = "lead_dedupe"
SALTS = "lead_salts"
COUNTS = "lead_counts"
STAFF = "shop_staff"  # P9.13: rows with shop_id and the staff member's phone, or user_id

EVENTS_KEEP = timedelta(days=90)
DEDUPE_KEEP = timedelta(hours=48)
SALT_GRACE = timedelta(hours=1)  # a salt outlives its IST day by this much, for taps sent just before midnight
COUNTS_KEEP_YEARS = 8

COUNT_FIELDS = ("call_taps", "directions_taps", "reservation_requests", "reservations_confirmed", "pickups_completed",
                "delivery_orders", "deliveries_completed", "customer_requests")
TAP_FIELDS = {CALL_TAP: "call_taps", DIRECTIONS_TAP: "directions_taps"}


# ---------------------------------------------------------------- IST days and months


def ist_day(at: datetime) -> date:
    """The IST calendar day of a naive UTC time."""
    return at.replace(tzinfo=UTC).astimezone(IST).date()


def ist_start(day: date) -> datetime:
    """The start of an IST day, as a naive UTC time."""
    return datetime.combine(day, time(0), tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """The IST calendar month, as naive UTC times [start, end)."""
    return ist_start(date(year, month, 1)), ist_start(date(*next_month(year, month), 1))


def financial_year_bounds(start_year: int) -> tuple[datetime, datetime]:
    """The Indian financial year start_year-(start_year + 1): 1 April to 31 March, IST, as naive UTC times [start, end)."""
    return ist_start(date(start_year, 4, 1)), ist_start(date(start_year + 1, 4, 1))


# ---------------------------------------------------------------- lead events

_salts: dict[str, str] = {}  # this process's copy of the current day's salt


def day_salt(db: Database, day: date) -> str:
    """The random salt of an IST day: created by the first tap of the day, removed an hour after the day ends."""
    key = day.isoformat()
    if (salt := _salts.get(key)) is not None:
        return salt
    coll = db.mongo[SALTS]
    doc = coll.find_one({"_id": key})
    if doc is None:
        try:
            coll.insert_one({"_id": key, "salt": secrets.token_hex(32),
                             "expires_at": ist_start(day + timedelta(days=1)) + SALT_GRACE})
        except DuplicateKeyError:  # another process created it first
            pass
        doc = coll.find_one({"_id": key})
    _salts.clear()  # only one day is ever kept
    _salts[key] = doc["salt"]
    return doc["salt"]


def visitor_hash(salt: str, ip: str, user_agent: str, shop_id: int, kind: str, day: date) -> str:
    """The dedupe key: one visitor (IP and user agent, salted for the day) for one shop, kind and day."""
    parts = (salt, ip_key(ip), user_agent, str(shop_id), kind, day.isoformat())
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def record_tap(db: Database, shop_id: int, kind: str, product_id: int | None, ip: str, user_agent: str,
               now: datetime | None = None) -> bool:
    """Record one Call or Directions tap. True when it counted, False when this visitor already counted for this
    shop and kind today. The caller has checked the shop and listing."""
    if kind not in LEAD_KINDS:
        raise ValueError(f"unknown lead kind {kind!r}")
    now = now or utcnow()
    day = ist_day(now)
    digest = visitor_hash(day_salt(db, day), ip, user_agent or "", shop_id, kind, day)

    def write() -> None:
        db.mongo[DEDUPE].insert_one({"_id": digest, "expires_at": now + DEDUPE_KEEP}, session=db.session)
        db.mongo[EVENTS].insert_one({"_id": db.next_id(EVENTS), "shop_id": shop_id, "product_id": product_id,
                                     "kind": kind, "day": ist_start(day)}, session=db.session)

    try:
        db.transaction(write)
    except DuplicateKeyError:
        return False
    return True


# ---------------------------------------------------------------- monthly counts


def _excluded_phones(db: Database, shop_ids: Iterable[int]) -> dict[int, set[str]]:
    """Per shop: the phones whose requests are not customer requests (the shop's, the owner's, its staff's)."""
    shop_ids = list(shop_ids)
    shops = db.shops.find_raw({"_id": {"$in": shop_ids}}, {"_id": 1, "phone": 1, "owner_id": 1})
    staff = list(db.mongo[STAFF].find({"shop_id": {"$in": shop_ids}}, {"shop_id": 1, "phone": 1, "user_id": 1}))
    user_ids = {s.get("owner_id") for s in shops} | {r.get("user_id") for r in staff}
    phones = {u["_id"]: u.get("phone") for u in db.users.find_raw(
        {"_id": {"$in": [u for u in user_ids if u is not None]}}, {"_id": 1, "phone": 1})}
    out: dict[int, set[str]] = defaultdict(set)
    for s in shops:
        out[s["_id"]].update({phone_key(s.get("phone")), phone_key(phones.get(s.get("owner_id")))})
    for r in staff:
        out[r["shop_id"]].update({phone_key(r.get("phone")), phone_key(phones.get(r.get("user_id")))})
    return {sid: {p for p in keys if p} for sid, keys in out.items()}


def compute_counts(db: Database, start: datetime, end: datetime,
                   shop_ids: Iterable[int] | None = None) -> dict[int, dict[str, int]]:
    """Every COUNT_FIELDS count per shop for [start, end) (naive UTC), for the given shops or every shop with any
    activity. A shop with no activity is missing from the result."""
    in_range = {"$gte": start, "$lt": end}
    only = {"shop_id": {"$in": list(shop_ids)}} if shop_ids is not None else {}
    counts: dict[int, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COUNT_FIELDS, 0))

    def grouped(collection: str, match: dict[str, Any]) -> list[dict]:
        return list(db.mongo[collection].aggregate([{"$match": {**match, **only}},
                                                     {"$group": {"_id": "$shop_id", "n": {"$sum": 1}}}]))

    taps, confirmed, completed, delivered, requests, orders = db.gather(
        lambda: list(db.mongo[EVENTS].aggregate([
            {"$match": {"day": in_range, **only}},
            {"$group": {"_id": {"shop_id": "$shop_id", "kind": "$kind"}, "n": {"$sum": 1}}}])),
        lambda: grouped("reservations", {"confirmed_at": in_range}),
        lambda: grouped("reservations", {"status": ReservationStatus.COMPLETED.value, "completed_at": in_range}),
        lambda: grouped("orders", {"status": OrderStatus.DELIVERED.value, "delivered_at": in_range}),
        lambda: db.reservations.find_raw({"created_at": in_range, **only}, {"shop_id": 1, "customer_id": 1}),
        lambda: db.orders.find_raw({"created_at": in_range, **only}, {"shop_id": 1, "customer_id": 1,
                                                                       "contact_phone": 1}),
    )
    for row in taps:
        field = TAP_FIELDS.get(row["_id"]["kind"])
        if field:
            counts[row["_id"]["shop_id"]][field] += row["n"]
    for rows, field in ((confirmed, "reservations_confirmed"), (completed, "pickups_completed"),
                        (delivered, "deliveries_completed")):
        for row in rows:
            counts[row["_id"]][field] += row["n"]

    customers = {r["customer_id"] for r in (*requests, *orders)}
    customer_phones = {u["_id"]: phone_key(u.get("phone")) for u in db.users.find_raw(
        {"_id": {"$in": list(customers)}}, {"_id": 1, "phone": 1})} if customers else {}
    excluded = _excluded_phones(db, {r["shop_id"] for r in (*requests, *orders)})
    for r in requests:
        counts[r["shop_id"]]["reservation_requests"] += 1
        if customer_phones.get(r["customer_id"]) not in excluded.get(r["shop_id"], set()):
            counts[r["shop_id"]]["customer_requests"] += 1
    for r in orders:
        counts[r["shop_id"]]["delivery_orders"] += 1
        shop_phones = excluded.get(r["shop_id"], set())
        phones = {customer_phones.get(r["customer_id"]), phone_key(r.get("contact_phone"))} - {None}
        if not phones & shop_phones:
            counts[r["shop_id"]]["customer_requests"] += 1
    return dict(counts)


def _keep_until(year: int, month: int) -> datetime:
    _, end = month_bounds(year, month)
    return end.replace(year=end.year + COUNTS_KEEP_YEARS)


def rollup_month(db: Database, year: int, month: int, now: datetime | None = None) -> dict[str, Any]:
    """Write the month's lead_counts rows (module docstring), replacing any earlier ones. Returns the month, the
    number of shops written and the totals of every count."""
    now = now or utcnow()
    key = month_key(year, month)
    start, end = month_bounds(year, month)
    counts = compute_counts(db, start, end)
    live = [d["_id"] for d in db.shops.find_raw(live_shop_filter(), {"_id": 1})]
    shop_ids = sorted(set(live) | set(counts))
    existing = {d["shop_id"] for d in db.mongo[COUNTS].find({"month": key}, {"shop_id": 1})}
    new_ids = iter(db.next_ids(COUNTS, len([s for s in shop_ids if s not in existing])))
    keep_until = _keep_until(year, month)
    ops = []
    for sid in shop_ids:
        row = counts.get(sid) or dict.fromkeys(COUNT_FIELDS, 0)
        insert = {} if sid in existing else {"$setOnInsert": {"_id": next(new_ids)}}
        ops.append(UpdateOne({"shop_id": sid, "month": key},
                             {"$set": {**row, "computed_at": now, "keep_until": keep_until}, **insert}, upsert=True))

    def write() -> None:
        if ops:
            db.mongo[COUNTS].bulk_write(ops, ordered=False, session=db.session)
        db.mongo[COUNTS].delete_many({"month": key, "shop_id": {"$nin": shop_ids}}, session=db.session)

    db.transaction(write)
    totals = {f: sum(c[f] for c in counts.values()) for f in COUNT_FIELDS}
    return {"month": key, "shops": len(shop_ids), **totals}


def month_counts(db: Database, shop_id: int, year: int, month: int, now: datetime | None = None) -> dict[str, Any]:
    """A shop's counts for an IST month: the stored lead_counts row of a finished month, else computed now (the
    current month so far, or a finished month the job has not rolled up yet). complete is false for the current
    month. A month that has not begun raises ValueError."""
    now = now or utcnow()
    start, end = month_bounds(year, month)
    if start > now:
        raise ValueError(f"{month_key(year, month)} has not begun")
    complete = end <= now
    if complete:
        stored = db.mongo[COUNTS].find_one({"shop_id": shop_id, "month": month_key(year, month)})
        if stored is not None:
            return {"shop_id": shop_id, "month": month_key(year, month), "complete": True,
                    **{f: int(stored.get(f, 0)) for f in COUNT_FIELDS}}
    row = compute_counts(db, start, min(end, now), [shop_id]).get(shop_id) or dict.fromkeys(COUNT_FIELDS, 0)
    return {"shop_id": shop_id, "month": month_key(year, month), "complete": complete, **row}


# ---------------------------------------------------------------- facilitated sales (TDS review)

FACILITATED_COLUMNS = ("shop_id", "shop_name", "completed_pickups", "completed_deliveries", "total_value")


def facilitated_sales(db: Database, start_year: int) -> list[dict[str, Any]]:
    """Per shop with any completed pickup or delivery in the financial year start_year-(start_year + 1): the counts and
    their total value in rupees (module docstring), ordered by shop id."""
    start, end = financial_year_bounds(start_year)
    in_range = {"$gte": start, "$lt": end}
    pickups, deliveries = db.gather(
        lambda: list(db.mongo["reservations"].aggregate([
            {"$match": {"status": ReservationStatus.COMPLETED.value, "completed_at": in_range}},
            {"$group": {"_id": "$shop_id", "n": {"$sum": 1},
                        "value": {"$sum": {"$multiply": ["$unit_price", "$quantity"]}}}}])),
        lambda: list(db.mongo["orders"].aggregate([
            {"$match": {"status": OrderStatus.DELIVERED.value, "delivered_at": in_range}},
            {"$group": {"_id": "$shop_id", "n": {"$sum": 1}, "value": {"$sum": "$total"}}}])),
    )
    rows: dict[int, dict[str, Any]] = {}
    for docs, field in ((pickups, "completed_pickups"), (deliveries, "completed_deliveries")):
        for d in docs:
            row = rows.setdefault(d["_id"], {"shop_id": d["_id"], "shop_name": "", "completed_pickups": 0,
                                             "completed_deliveries": 0, "total_value": 0.0})
            row[field] += d["n"]
            row["total_value"] += float(d["value"] or 0)
    names = {s["_id"]: s.get("name", "") for s in db.shops.find_raw({"_id": {"$in": list(rows)}}, {"name": 1})}
    for sid, row in rows.items():
        row["shop_name"] = names.get(sid, "")
        row["total_value"] = round(row["total_value"], 2)
    return [rows[sid] for sid in sorted(rows)]
