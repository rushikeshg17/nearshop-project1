"""Boost packs: one labelled sponsored slot, city eligibility and the make-good (P10.12; plan section 8.22).

A shop pays for one clearly labelled "Sponsored" card on results pages. It never changes the organic results, their
order or ratings: run_search (app/services/search.py) ranks and pages exactly as without Boost, and only then asks
choose() for a card, which the response carries apart from the organic groups (sponsored, with its position). With no
pack serving, the response is exactly what it was before Boost.

Packs (collection boost_packs)
------------------------------
The shop, its city (the shop's city, folded: city_key), up to boost_pack_groups item groups (categories or catalogue
items), the kind (standard: NEARSHOP_BOOST_PACK_*; festive: NEARSHOP_BOOST_FESTIVE_*), the list price, the plan
discount (the boost_discount_pct entitlement of the shop's plan, P9.4: 25% on Growth and above) and the amount, the
period, the payment request and payment (P9.5, purpose boost) and the status:

  * pending: bought, waiting for its payment. Matching the payment (billing._settle) activates it; a newer purchase
    by the same shop cancels its request and ends it.
  * active: serving, from period_start to ends_at (period_start + days).
  * paused: its city fell below a threshold (or it was paid while the city was below one). Not serving; when the city
    is eligible again it resumes in its earlier status and ends_at moves later by the paused time ("the days added
    back"; paused_seconds keeps the total).
  * make_good: the free next period. A period that ends with 0 attributed requests earns, automatically and once
    per purchase (make_good_used), one more period of the same length from the old end.
  * ended: its last period is over, it was replaced while pending, or its payment was refunded (end_reason).

The slot (choose)
-----------------
On the first page of a search with a query or a category, after ranking: a pack is eligible when it is active or in
its make-good period (period_start <= now < ends_at), and its shop (verified, live and inside the radius: the shops
run_search searched) has a listing among the search's candidates (so it matches the query and passes every filter,
the in-stock filter included) in one of the pack's item groups, in stock, and with its stock confirmed within
boost_fresh_hours (48). One eligible pack is shown, in equal rotation (round robin per set of eligible packs, in each
process), at position boost_slot_position (3) of the first page, or after the last organic group of a shorter page.
At most one card per page, so never the same shop twice in the sponsored slot; the organic list may also hold the
same listing. Shop pages (shop_id) and later pages get none.

Attribution, without personal data
----------------------------------
The card carries a token: the pack, the listing and the issue time, signed with an HMAC of the secret key. Its link
(P10.13) posts the token to POST /api/search/sponsored-click within boost_token_minutes; that counts a click and adds
the pack and the click time, signed, to the cookie nearshop_boost (HttpOnly, SameSite Lax, path /api, no lifetime: it
ends with the browser session). A reservation or order created in the same browser session on the same IST day, at the
pack's shop, on a listing in the pack's item groups, counts as one request for the pack while it is serving
(attribute, from app/api/routes/customer.py; off the request path). Impressions (searches run with log on that showed
the card), clicks and requests are counted per pack per IST day in boost_stats; the pack also keeps its current
period's requests (period_requests) for the make-good. Nothing names a customer: no user id, session id, IP address,
user agent or reservation or order id is stored.

City eligibility (the boost job, app/jobs.py)
---------------------------------------------
Once per IST day the job records, per city with a live shop (boost_city_days, one row per city and day): the verified
live shops, the monthly active customers and the median stock age. A city is eligible when its latest row (today's or
yesterday's) has boost_min_verified_shops (100) or more verified live shops and a median stock age of
boost_max_median_stock_age_hours (48) or less, and every row of each of the last boost_customer_weeks (4) weeks
(each week needs at least one) counted boost_min_monthly_customers (3,000) or more. Below any threshold no pack is
sold and the city's serving packs pause; they resume, with the days added back, once it is eligible again.

  * Verified live shops: Shop.is_live (P3.3), in the city.
  * Median stock age: the median hours since stock_updated_at over the active listings of those shops (P7.4's
    city-wide median).
  * Monthly active customers: active customer accounts seen in the 30 days to the check (a sign-in, a session in use,
    or a reservation or order created), each in the city of the shop of their latest reservation or order, or the
    deployment's city (settings.seed_city) when they have none. Only the count is stored.

Every tick, the job also closes the periods that have ended (end_periods): the make-good or the end.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import logging
import statistics
import threading
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from fastapi import Request, Response

from app.core.config import settings
from app.core.database import Collection, Database, utcnow
from app.core.errors import AppError, NotFound
from app.models import Shop
from app.models.base import Document, Embedded, Timestamps
from app.models.billing import Actor
from app.models.core import live_shop_filter
from app.services import billing, leads, plans
from app.services.notifications import notify

log = logging.getLogger(__name__)

PACKS = "boost_packs"
STATS = "boost_stats"
CITY_DAYS = "boost_city_days"
COOKIE = "nearshop_boost"
COOKIE_ENTRIES = 5  # the latest clicked packs a session keeps
SPONSORED_LABEL = "Sponsored"
BOOST_LINK = "/owner/boost"
KIND_BOOST = "boost_pack"  # the owner's notifications
ACTIVE_CUSTOMER_DAYS = 30
STAT_FIELDS = ("impressions", "clicks", "requests")
TOKEN_VERSION = "b1"


class BoostRefused(AppError):
    status_code = 409
    code = "boost_refused"


class PackKind(StrEnum):
    STANDARD = "standard"
    FESTIVE = "festive"


class PackStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"
    MAKE_GOOD = "make_good"


SERVING = (PackStatus.ACTIVE.value, PackStatus.MAKE_GOOD.value)


class GroupType(StrEnum):
    CATEGORY = "category"
    ITEM = "item"  # a catalogue item


class ItemGroup(Embedded):
    type: GroupType
    id: int
    name: str


class BoostPack(Document, Timestamps):
    shop_id: int
    owner_id: int
    city: str  # city_key of the shop's city at purchase
    kind: PackKind
    groups: list[ItemGroup]
    days: int
    list_price_paise: int
    discount_pct: int = 0
    amount_paise: int  # before GST: what the payment request asks for, GST added by billing
    plan_code: str  # the shop's plan whose discount applied
    payment_request_id: int | None = None
    payment_request_number: str | None = None
    payment_id: int | None = None
    status: PackStatus = PackStatus.PENDING
    paid_at: datetime | None = None
    period_start: datetime | None = None
    ends_at: datetime | None = None
    period_requests: int = 0
    requests_total: int = 0
    make_good_used: bool = False
    paused_at: datetime | None = None
    resume_status: PackStatus | None = None
    pause_reason: str | None = None
    paused_seconds: int = 0
    ended_at: datetime | None = None
    end_reason: str | None = None


def packs(db: Database) -> Collection[BoostPack]:
    return Collection(db, PACKS, BoostPack)


# ---------------------------------------------------------------- prices


def city_key(name: str | None) -> str:
    """A city's name folded for comparison: case and spacing ignored."""
    return " ".join(str(name or "").split()).casefold()


def pack_terms(kind: PackKind | str) -> dict[str, int]:
    """The list price (paise, before GST), days and item groups of a pack kind, from settings."""
    if PackKind(kind) == PackKind.FESTIVE:
        return {"list_price_paise": settings.boost_festive_price_paise, "days": settings.boost_festive_days,
                "max_groups": settings.boost_festive_groups}
    return {"list_price_paise": settings.boost_pack_price_paise, "days": settings.boost_pack_days,
            "max_groups": settings.boost_pack_groups}


def discounted(list_price_paise: int, pct: int) -> int:
    """The price after a percentage discount, to the nearest paisa (half up)."""
    value = Decimal(list_price_paise) * (100 - max(0, min(pct, 100))) / 100
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def quote(db: Database, shop: Shop, kind: PackKind | str, now: datetime | None = None) -> dict[str, Any]:
    """What the shop pays for a pack of this kind now: the list price less its plan's Boost discount (P9.4)."""
    now = now or utcnow()
    plan = plans.effective_plan(db, shop.id, now)
    pct = int(plan["entitlements"].get("boost_discount_pct") or 0)
    terms = pack_terms(kind)
    return {"kind": PackKind(kind).value, **terms, "discount_pct": pct,
            "amount_paise": discounted(terms["list_price_paise"], pct), "plan_code": plan["code"],
            "plan_version": plan["version"]}


# ---------------------------------------------------------------- city eligibility


def _ist_day(at: datetime) -> date:
    return leads.ist_day(at)


def _monthly_customers(db: Database, now: datetime, shop_city: dict[int, str]) -> dict[str, int]:
    """Monthly active customers per city key (module docstring): active customer accounts seen in the 30 days to now,
    in the city of the shop of their latest reservation or order, else the deployment's city."""
    since = now - timedelta(days=ACTIVE_CUSTOMER_DAYS)
    seen = {u["_id"] for u in db.users.find_raw(
        {"role": "customer", "is_active": True,
         "$or": [{"last_login_at": {"$gte": since}}, {"sessions.last_seen_at": {"$gte": since}}]}, {"_id": 1})}
    for name in ("reservations", "orders"):
        seen.update(r["_id"] for r in db.mongo[name].aggregate([
            {"$match": {"created_at": {"$gte": since, "$lte": now}}}, {"$group": {"_id": "$customer_id"}}]))
    if seen:  # accounts deleted or switched off since their last reservation are not customers any more
        seen = {u["_id"] for u in db.users.find_raw({"_id": {"$in": list(seen)}, "role": "customer",
                                                      "is_active": True}, {"_id": 1})}
    latest: dict[int, tuple[datetime, int]] = {}
    for name in ("reservations", "orders"):
        for r in db.mongo[name].aggregate([
                {"$match": {"customer_id": {"$in": list(seen)}, "created_at": {"$lte": now}}},
                {"$sort": {"created_at": -1}},
                {"$group": {"_id": "$customer_id", "shop_id": {"$first": "$shop_id"}, "at": {"$first": "$created_at"}}}]):
            if r["_id"] not in latest or r["at"] > latest[r["_id"]][0]:
                latest[r["_id"]] = (r["at"], r["shop_id"])
    home = city_key(_deployment_city())
    out: dict[str, int] = defaultdict(int)
    for customer in seen:
        city = home
        if customer in latest:
            shop_id = latest[customer][1]
            city = shop_city.get(shop_id) or _shop_city(db, shop_id, shop_city) or home
        out[city] += 1
    return dict(out)


def _deployment_city() -> str:
    from app.services.meta import city_config

    return str(city_config()["name"])


def _shop_city(db: Database, shop_id: int, cache: dict[int, str]) -> str | None:
    """The city key of a shop that is not live now (customers keep the city of their latest shop)."""
    if shop_id not in cache:
        doc = db.shops.find_raw({"_id": shop_id}, {"city": 1}, limit=1)
        cache[shop_id] = city_key(doc[0].get("city")) if doc else ""
    return cache[shop_id] or None


def measure_cities(db: Database, now: datetime) -> dict[str, dict[str, Any]]:
    """Today's figures per city with a live shop: verified live shops, monthly active customers and the median stock
    age in hours (None without a confirmed listing)."""
    live = db.shops.find_raw(live_shop_filter(now), {"city": 1})
    shop_city = {s["_id"]: city_key(s.get("city")) for s in live}
    names: dict[str, str] = {}
    for s in live:
        names.setdefault(city_key(s.get("city")), " ".join(str(s.get("city") or "").split()))
    ages: dict[str, list[float]] = defaultdict(list)
    for r in db.products.find_raw({"is_active": True, "shop_id": {"$in": list(shop_city)}},
                                  {"shop_id": 1, "stock_updated_at": 1}):
        if r.get("stock_updated_at") is not None:
            ages[shop_city[r["shop_id"]]].append(max((now - r["stock_updated_at"]).total_seconds() / 3600, 0.0))
    customers = _monthly_customers(db, now, dict(shop_city))
    shops_per_city: dict[str, int] = defaultdict(int)
    for key in shop_city.values():
        shops_per_city[key] += 1
    out = {}
    for key, n in shops_per_city.items():
        med = statistics.median(ages[key]) if ages.get(key) else None
        out[key] = {"city": key, "name": names[key], "verified_shops": n, "monthly_customers": customers.get(key, 0),
                    "median_stock_age_hours": round(med, 2) if med is not None else None}
    return out


def record_city_days(db: Database, now: datetime) -> list[dict[str, Any]]:
    """The daily eligibility check's figures, one boost_city_days row per city and IST day (a re-run replaces it)."""
    day = _ist_day(now).isoformat()
    rows = []
    for key, figures in measure_cities(db, now).items():
        row = {**figures, "day": day, "checked_at": now}
        db.mongo[CITY_DAYS].replace_one({"_id": f"{key}:{day}"}, {"_id": f"{key}:{day}", **row}, upsert=True)
        rows.append(row)
    return rows


def city_eligibility(db: Database, city: str, now: datetime | None = None) -> dict[str, Any]:
    """Whether Boost may be sold and serve in the city (a city_key) now, from the daily checks, with the figures and
    the reasons it is not."""
    now = now or utcnow()
    today = _ist_day(now)
    weeks = settings.boost_customer_weeks
    first = today - timedelta(days=7 * weeks - 1)
    rows = sorted(db.mongo[CITY_DAYS].find({"city": city, "day": {"$gte": first.isoformat(),
                                                                   "$lte": today.isoformat()}}),
                  key=lambda r: r["day"])
    reasons: list[str] = []
    latest = rows[-1] if rows else None
    if latest is None or latest["day"] < (today - timedelta(days=1)).isoformat():
        reasons.append("no_recent_check")
    else:
        if latest["verified_shops"] < settings.boost_min_verified_shops:
            reasons.append("verified_shops")
        age = latest.get("median_stock_age_hours")
        if age is None or age > settings.boost_max_median_stock_age_hours:
            reasons.append("median_stock_age")
    lowest = None
    for w in range(weeks):
        end = today - timedelta(days=7 * w)
        start = end - timedelta(days=6)
        week = [r for r in rows if start.isoformat() <= r["day"] <= end.isoformat()]
        if not week:
            reasons.append("customer_weeks")
            break
        low = min(r["monthly_customers"] for r in week)
        lowest = low if lowest is None else min(lowest, low)
    if lowest is not None and lowest < settings.boost_min_monthly_customers and "customer_weeks" not in reasons:
        reasons.append("monthly_customers")
    return {
        "city": city, "eligible": not reasons, "reasons": reasons, "as_of": latest["day"] if latest else None,
        "verified_shops": latest["verified_shops"] if latest else None,
        "median_stock_age_hours": latest.get("median_stock_age_hours") if latest else None,
        "monthly_customers": latest["monthly_customers"] if latest else None,
        "lowest_monthly_customers": lowest, "days_checked": len(rows),
        "thresholds": {"verified_shops": settings.boost_min_verified_shops,
                       "monthly_customers": settings.boost_min_monthly_customers,
                       "customer_weeks": weeks,
                       "median_stock_age_hours": settings.boost_max_median_stock_age_hours},
    }


# ---------------------------------------------------------------- pausing and periods


def _pause(db: Database, pack: BoostPack, now: datetime, reason: str) -> bool:
    return bool(packs(db).update_one(
        {"_id": pack.id, "status": {"$in": list(SERVING)}},
        {"$set": {"status": PackStatus.PAUSED.value, "paused_at": now, "resume_status": pack.status.value,
                  "pause_reason": reason, "updated_at": now}}))


def _resume(db: Database, pack: BoostPack, now: datetime) -> bool:
    """Back to its earlier status, with ends_at moved later by the paused time (the days added back)."""
    paused = max(now - (pack.paused_at or now), timedelta(0))
    ends_at = (pack.ends_at or now) + paused
    return bool(packs(db).update_one(
        {"_id": pack.id, "status": PackStatus.PAUSED.value, "paused_at": pack.paused_at},
        {"$set": {"status": (pack.resume_status or PackStatus.ACTIVE).value, "ends_at": ends_at, "paused_at": None,
                  "resume_status": None, "pause_reason": None, "updated_at": now},
         "$inc": {"paused_seconds": int(paused.total_seconds())}}))


def apply_eligibility(db: Database, now: datetime) -> dict[str, int]:
    """Pause the serving packs of every city that is not eligible, and resume the paused packs of every city that is."""
    paused = resumed = 0
    verdicts: dict[str, bool] = {}
    for pack in packs(db).find({"status": {"$in": [*SERVING, PackStatus.PAUSED.value]}}):
        if pack.city not in verdicts:
            verdicts[pack.city] = city_eligibility(db, pack.city, now)["eligible"]
        if not verdicts[pack.city] and pack.status != PackStatus.PAUSED:
            if _pause(db, pack, now, "city_not_eligible"):
                paused += 1
                _notice(db, pack, "Boost paused", "Your Boost pack is paused because your city is below the Boost "
                        "thresholds for now. The days it is paused are added back when it restarts.")
        elif verdicts[pack.city] and pack.status == PackStatus.PAUSED:
            if _resume(db, pack, now):
                resumed += 1
                _notice(db, pack, "Boost restarted", "Your Boost pack is showing again, with the paused days added "
                        "to its end date.")
    return {"paused": paused, "resumed": resumed}


def end_periods(db: Database, now: datetime) -> dict[str, int]:
    """Close every serving period that has ended: a paid period with 0 requests becomes a free make-good period of the
    same length, once per purchase; anything else ends. Each change is conditional on what it read."""
    made_good = ended = 0
    for pack in packs(db).find({"status": {"$in": list(SERVING)}, "ends_at": {"$lte": now}}):
        if (pack.status == PackStatus.ACTIVE and not pack.make_good_used and pack.period_requests == 0
                and pack.ends_at is not None):
            if packs(db).update_one(
                    {"_id": pack.id, "status": PackStatus.ACTIVE.value, "ends_at": pack.ends_at, "period_requests": 0},
                    {"$set": {"status": PackStatus.MAKE_GOOD.value, "period_start": pack.ends_at,
                              "ends_at": pack.ends_at + timedelta(days=pack.days), "make_good_used": True,
                              "period_requests": 0, "updated_at": now}}):
                made_good += 1
                _notice(db, pack, "Boost: next period free", f"Your Boost pack brought no customer requests in its "
                        f"{pack.days} days, so it runs again for {pack.days} days at no charge.")
                continue
            pack = packs(db).get(pack.id) or pack  # a request arrived meanwhile: it ends instead
            if pack.status not in (PackStatus.ACTIVE, PackStatus.MAKE_GOOD) or pack.ends_at is None or pack.ends_at > now:
                continue
        if packs(db).update_one({"_id": pack.id, "status": pack.status.value, "ends_at": pack.ends_at},
                                {"$set": {"status": PackStatus.ENDED.value, "ended_at": pack.ends_at,
                                          "end_reason": "period_over", "updated_at": now}}):
            ended += 1
            _notice(db, pack, "Boost ended", f"Your Boost pack has ended after bringing {pack.requests_total} customer "
                    f"request{'' if pack.requests_total == 1 else 's'}.")
    return {"made_good": made_good, "ended": ended}


def run_daily(db: Database, now: datetime) -> dict[str, Any]:
    """The daily eligibility check: today's city figures, then pausing and resuming packs."""
    rows = record_city_days(db, now)
    return {"cities": len(rows), **apply_eligibility(db, now)}


def _notice(db: Database, pack: BoostPack, title: str, body: str) -> None:
    try:
        notify(db, pack.owner_id, KIND_BOOST, title, body, BOOST_LINK)
    except Exception as exc:  # noqa: BLE001 - a notice is never worth failing the job
        log.warning("boost notice not sent: %s", type(exc).__name__)


# ---------------------------------------------------------------- buying and payment


def _groups(db: Database, wanted: Iterable[tuple[str, int]], max_groups: int) -> list[ItemGroup]:
    pairs = list(dict.fromkeys((GroupType(t), int(i)) for t, i in wanted))
    if not 1 <= len(pairs) <= max_groups:
        raise BoostRefused(f"Choose 1 to {max_groups} item groups", code="boost_groups")
    cats = db.categories.by_ids(i for t, i in pairs if t == GroupType.CATEGORY)
    items = db.catalog_items.by_ids(i for t, i in pairs if t == GroupType.ITEM)
    out = []
    for t, i in pairs:
        found = (cats if t == GroupType.CATEGORY else items).get(i)
        if found is None:
            raise NotFound(f"There is no {'category' if t == GroupType.CATEGORY else 'catalogue item'} {i}")
        out.append(ItemGroup(type=t, id=i, name=found.name))
    return out


def buy(db: Database, shop: Shop, *, kind: PackKind | str, groups: Iterable[tuple[str, int]], actor: Actor,
        now: datetime | None = None) -> tuple[BoostPack, billing.PaymentRequest]:
    """A pending pack and its payment request (purpose boost, P9.5). Refused while sales are off
    (boost_sales_enabled, B31), for a shop customers cannot see, and in a city that is not eligible. The shop's
    earlier pending pack, if any, ends and its request is cancelled."""
    now = now or utcnow()
    if not settings.boost_sales_enabled:
        raise BoostRefused("Boost packs are not on sale yet", code="boost_sales_off")
    if not shop.is_live:
        raise BoostRefused("Boost is for verified shops that customers can see", code="boost_shop_not_live")
    city = city_key(shop.city)
    if not city_eligibility(db, city, now)["eligible"]:
        raise BoostRefused("Boost is not sold in your city yet", code="boost_city_not_eligible")
    q = quote(db, shop, kind, now)
    chosen = _groups(db, groups, q["max_groups"])
    if not billing.configured():
        raise billing.BillingNotConfigured("Payments to NearShop are not set up yet")

    def write() -> tuple[BoostPack, billing.PaymentRequest]:
        packs(db).update_many({"shop_id": shop.id, "status": PackStatus.PENDING.value},
                              {"$set": {"status": PackStatus.ENDED.value, "ended_at": now, "end_reason": "replaced",
                                        "updated_at": now}})
        pack = BoostPack(id=db.next_id(PACKS), shop_id=shop.id, owner_id=shop.owner_id, city=city, kind=q["kind"],
                         groups=chosen, days=q["days"], list_price_paise=q["list_price_paise"],
                         discount_pct=q["discount_pct"], amount_paise=q["amount_paise"], plan_code=q["plan_code"],
                         created_at=now, updated_at=now)
        req = billing.create_boost_request(db, shop, amount_paise=pack.amount_paise, plan_code=q["plan_code"],
                                           plan_version=q["plan_version"], boost_pack_id=pack.id, actor=actor, now=now)
        pack.payment_request_id, pack.payment_request_number = req.id, req.number
        return packs(db).insert(pack), req

    return db.transaction(write)


def activate_paid(db: Database, req: billing.PaymentRequest, *, now: datetime) -> BoostPack:
    """In billing's matching transaction: the pack its request pays for starts now, paused (with the days added back
    later) when its city is not eligible at this moment."""
    pack = packs(db).get(req.boost_pack_id)
    if pack is None or pack.payment_request_id != req.id:
        raise billing.BillingRefused(f"Request {req.number} names no Boost pack")
    if pack.status != PackStatus.PENDING:
        raise billing.BillingRefused(f"The Boost pack of request {req.number} is {pack.status.value}")
    fields: dict[str, Any] = {"status": PackStatus.ACTIVE, "paid_at": now, "period_start": now,
                              "ends_at": now + timedelta(days=pack.days)}
    if not city_eligibility(db, pack.city, now)["eligible"]:
        fields.update(status=PackStatus.PAUSED, paused_at=now, resume_status=PackStatus.ACTIVE,
                      pause_reason="city_not_eligible")
    return packs(db).set(pack, **fields)


def record_payment(db: Database, pack_id: int, payment_id: int) -> None:
    """In billing's matching transaction: the payment that paid for the pack."""
    packs(db).update_one({"_id": pack_id}, {"$set": {"payment_id": payment_id}})


def end_refunded(db: Database, pack_id: int, now: datetime) -> None:
    """In billing's refund transaction: the pack ends at once."""
    packs(db).update_one({"_id": pack_id, "status": {"$ne": PackStatus.ENDED.value}},
                         {"$set": {"status": PackStatus.ENDED.value, "ended_at": now, "end_reason": "refunded",
                                   "updated_at": now}})


def description(pack: BoostPack) -> str:
    """The receipt or invoice line for a pack."""
    off = f", {pack.discount_pct}% plan discount" if pack.discount_pct else ""
    return f"NearShop Boost {pack.kind.value} pack, {pack.days} days ({len(pack.groups)} item groups{off})"


# ---------------------------------------------------------------- the slot


_turns: dict[tuple[int, ...], itertools.count] = {}
_turns_lock = threading.Lock()


def _next_turn(key: tuple[int, ...]) -> int:
    with _turns_lock:
        if key not in _turns:
            if len(_turns) > 10_000:
                _turns.clear()
            _turns[key] = itertools.count()
        return next(_turns[key])


def reset_rotation() -> None:
    """Forget the rotation (tests)."""
    with _turns_lock:
        _turns.clear()


def choose(db: Database, rows: list[dict], relevance: dict[int, float], now: datetime) -> tuple[dict, dict] | None:
    """The pack (raw document) and listing row for the sponsored slot among a search's candidate rows (RANK_FIELDS,
    already limited to live shops in radius, the query and the filters), or None. Equal rotation among the eligible
    packs."""
    shop_ids = list({r["shop_id"] for r in rows})
    if not shop_ids:
        return None
    serving = list(db.mongo[PACKS].find({"status": {"$in": list(SERVING)}, "shop_id": {"$in": shop_ids},
                                         "period_start": {"$lte": now}, "ends_at": {"$gt": now}},
                                        {"shop_id": 1, "groups": 1}, sort=[("_id", 1)]))
    if not serving:
        return None
    fresh_since = now - timedelta(hours=settings.boost_fresh_hours)
    by_shop: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r["quantity"] > 0 and r.get("stock_updated_at") is not None and r["stock_updated_at"] >= fresh_since:
            by_shop[r["shop_id"]].append(r)
    eligible: list[tuple[dict, dict]] = []
    for pack in serving:
        cats = {g["id"] for g in pack["groups"] if g["type"] == GroupType.CATEGORY.value}
        items = {g["id"] for g in pack["groups"] if g["type"] == GroupType.ITEM.value}
        matching = [r for r in by_shop.get(pack["shop_id"], [])
                    if r.get("category_id") in cats or (r.get("catalog_item_id") is not None
                                                        and r["catalog_item_id"] in items)]
        if matching:
            best = min(matching, key=lambda r: (-relevance.get(r["_id"], 0.0), float(r["price"]), r["_id"]))
            eligible.append((pack, best))
    if not eligible:
        return None
    return eligible[_next_turn(tuple(p["_id"] for p, _ in eligible)) % len(eligible)]


# ---------------------------------------------------------------- tokens and counting


def _key() -> bytes:
    return hmac.new(settings.secret_key.encode(), b"nearshop-boost", hashlib.sha256).digest()


def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(_key(), payload.encode(), hashlib.sha256).digest()[:18]).decode()


def _epoch(at: datetime) -> int:
    return int(at.replace(tzinfo=UTC).timestamp())


def _from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)


def token(pack_id: int, listing_id: int, at: datetime | None = None) -> str:
    """The sponsored card's link token: the pack, the listing and the issue time, signed."""
    payload = f"{TOKEN_VERSION}.{pack_id}.{listing_id}.{_epoch(at or utcnow())}"
    return f"{payload}.{_sign(payload)}"


def read_token(value: str, now: datetime | None = None) -> tuple[int, int] | None:
    """(pack id, listing id) of a token that this server signed within boost_token_minutes, else None."""
    parts = (value or "").split(".")
    if len(parts) != 5 or parts[0] != TOKEN_VERSION:
        return None
    payload = ".".join(parts[:4])
    if not hmac.compare_digest(_sign(payload), parts[4]):
        return None
    try:
        pack_id, listing_id, issued = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    age = _epoch(now or utcnow()) - issued
    if not 0 <= age <= settings.boost_token_minutes * 60:
        return None
    return pack_id, listing_id


def count(db: Database, pack_id: int, shop_id: int, field: str, at: datetime | None = None) -> None:
    """One impression, click or request for the pack on the IST day of at."""
    day = _ist_day(at or utcnow()).isoformat()
    db.mongo[STATS].update_one({"_id": f"{pack_id}:{day}"},
                               {"$inc": {field: 1}, "$setOnInsert": {"pack_id": pack_id, "shop_id": shop_id,
                                                                    "day": day}}, upsert=True)


def count_impression(db: Database, pack_id: int, shop_id: int) -> None:
    """Off the request path (run_search's log): an impression."""
    name = db.mongo.name
    db.background(lambda: count(Database(name), pack_id, shop_id, "impressions"))


def _cookie_entries(value: str | None) -> list[tuple[int, int]]:
    """The (pack id, click epoch) entries of a boost cookie that this server signed."""
    out = []
    for entry in (value or "").split("~")[:COOKIE_ENTRIES]:
        parts = entry.split(".")
        if len(parts) != 3:
            continue
        payload = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(_sign(f"c.{payload}"), parts[2]):
            continue
        try:
            out.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return out


def _cookie_value(entries: list[tuple[int, int]]) -> str:
    return "~".join(f"{p}.{ts}.{_sign(f'c.{p}.{ts}')}" for p, ts in entries[-COOKIE_ENTRIES:])


def record_click(db: Database, request: Request, response: Response, value: str, now: datetime | None = None) -> bool:
    """A click on a sponsored card: counted for its pack when the token is valid and the pack is serving, and the
    pack added to this browser session's boost cookie for attribution. Whether it was counted."""
    now = now or utcnow()
    read = read_token(value, now)
    if read is None:
        return False
    pack_id, _ = read
    pack = db.mongo[PACKS].find_one({"_id": pack_id, "status": {"$in": list(SERVING)}}, {"shop_id": 1})
    if pack is None:
        return False
    count(db, pack_id, pack["shop_id"], "clicks", now)
    entries = [e for e in _cookie_entries(request.cookies.get(COOKIE)) if e[0] != pack_id]
    entries.append((pack_id, _epoch(now)))
    # A session cookie (no Max-Age): attribution needs the same browser session, and the same IST day (attribute).
    response.set_cookie(COOKIE, _cookie_value(entries), httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/api")
    return True


def attribute(db: Database, request: Request, product_ids: Iterable[int], now: datetime | None = None) -> None:
    """After a reservation or order is created: one request for each pack clicked in this browser session today whose
    shop and item groups the reservation or order matches. Off the request path; never fails it."""
    try:
        entries = _cookie_entries(request.cookies.get(COOKIE))
        if not entries:
            return
        now = now or utcnow()
        today = _ist_day(now)
        pack_ids = [p for p, ts in entries if _ist_day(_from_epoch(ts)) == today and _from_epoch(ts) <= now]
        if not pack_ids:
            return
        ids, name = list(product_ids), db.mongo.name
        db.background(lambda: _attribute(Database(name), pack_ids, ids, now))
    except Exception as exc:  # noqa: BLE001 - attribution never fails a reservation or an order
        log.warning("boost attribution skipped: %s", type(exc).__name__)


def _attribute(db: Database, pack_ids: list[int], product_ids: list[int], now: datetime) -> int:
    rows = db.products.find_raw({"_id": {"$in": product_ids}}, {"shop_id": 1, "category_id": 1, "catalog_item_id": 1})
    counted = 0
    for pack in db.mongo[PACKS].find({"_id": {"$in": pack_ids}, "status": {"$in": list(SERVING)},
                                      "period_start": {"$lte": now}, "ends_at": {"$gt": now}}):
        cats = {g["id"] for g in pack["groups"] if g["type"] == GroupType.CATEGORY.value}
        items = {g["id"] for g in pack["groups"] if g["type"] == GroupType.ITEM.value}
        if not any(r["shop_id"] == pack["shop_id"] and (r.get("category_id") in cats or (
                r.get("catalog_item_id") is not None and r["catalog_item_id"] in items)) for r in rows):
            continue
        if db.mongo[PACKS].update_one({"_id": pack["_id"], "status": {"$in": list(SERVING)}, "ends_at": {"$gt": now}},
                                      {"$inc": {"period_requests": 1, "requests_total": 1}}).modified_count:
            count(db, pack["_id"], pack["shop_id"], "requests", now)
            counted += 1
    return counted


# ---------------------------------------------------------------- the owner's view


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() + "Z" if at else None


def pack_out(pack: BoostPack, stats: list[dict] | None = None) -> dict[str, Any]:
    days = sorted(stats or [], key=lambda r: r["day"])
    return {"id": pack.id, "kind": pack.kind.value, "status": pack.status.value,
            "groups": [g.model_dump(mode="json") for g in pack.groups], "days": pack.days,
            "list_price_paise": pack.list_price_paise, "discount_pct": pack.discount_pct,
            "amount_paise": pack.amount_paise, "payment_request_number": pack.payment_request_number,
            "paid_at": _iso(pack.paid_at), "period_start": _iso(pack.period_start), "ends_at": _iso(pack.ends_at),
            "make_good_used": pack.make_good_used, "paused_at": _iso(pack.paused_at),
            "pause_reason": pack.pause_reason, "paused_days_added": round(pack.paused_seconds / 86400, 2),
            "ended_at": _iso(pack.ended_at), "end_reason": pack.end_reason, "period_requests": pack.period_requests,
            "totals": {f: sum(int(r.get(f) or 0) for r in days) for f in STAT_FIELDS},
            "by_day": [{"day": r["day"], **{f: int(r.get(f) or 0) for f in STAT_FIELDS}} for r in days]}


def owner_packs(db: Database, shop: Shop) -> list[dict[str, Any]]:
    """The shop's packs, newest first, each with its impressions, clicks and requests per day and in total."""
    rows = packs(db).find({"shop_id": shop.id}, sort=[("_id", -1)], limit=50)
    stats: dict[int, list[dict]] = defaultdict(list)
    for r in db.mongo[STATS].find({"pack_id": {"$in": [p.id for p in rows]}}):
        stats[r["pack_id"]].append(r)
    return [pack_out(p, stats.get(p.id)) for p in rows]


def owner_eligibility(db: Database, shop: Shop, now: datetime | None = None) -> dict[str, Any]:
    """Whether this shop can buy a pack now, why not, its city's figures and both packs' prices for it."""
    now = now or utcnow()
    city = city_eligibility(db, city_key(shop.city), now)
    reasons = ([] if settings.boost_sales_enabled else ["sales_off"]) + \
        ([] if shop.is_live else ["shop_not_live"]) + [f"city_{r}" for r in city["reasons"]]
    return {"can_buy": not reasons, "reasons": reasons, "sales_enabled": settings.boost_sales_enabled,
            "city": {**city, "name": shop.city}, "label": SPONSORED_LABEL,
            "slot_position": settings.boost_slot_position, "fresh_hours": settings.boost_fresh_hours,
            "packs": [quote(db, shop, k, now) for k in PackKind]}
