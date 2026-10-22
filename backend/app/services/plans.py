"""Plans, entitlements and subscriptions (P9.4; plan sections 8.17 and 8.18; app/models/billing.py).

NearShop knows which plan each shop is on and what that plan allows. A plan that lapses falls back to Free at once,
without hiding the shop or changing its place in search, and no tool that keeps stock fresh is ever behind a payment.
No payment is taken here: P9.5 records payments and calls create_subscription and renew; P9.6 adds UPI Autopay.

The catalogue
-------------
plans holds a versioned catalogue (version 1 is inserted by migration 8 with business/BUSINESS_MODEL.md's values,
all assumptions under test). A catalogue version is one row per plan code with the same version and effective_from.
catalogue(db, at) is the version in force at a time: the highest version whose effective_from has passed. An admin
publishes a new version (publish_catalogue, POST /api/admin/plans) to change prices or on-sale flags, with a reason;
that is how a price cut after B25, or a plan launch (Pro after B28, Business after B32), happens without code. A
subscription keeps the plan row it was sold on (plan_id, plan_version) until it renews (renew), when it moves to the
version then in force.

Entitlements
------------
effective_plan(db, shop_id, at) is the one function that says which plan a shop is on and what it allows: the plan
row of its active subscription covering that time, else the Free row of the catalogue in force. A shop with no active
subscription (none, an advance not yet started, lapsed, cancelled or refunded) is on Free. Every paid feature asks
it, or entitled() and require_entitlement(), which call it, and nothing else. The freshness tools of plan section 8.17
(FREE_TOOLS) never ask: tests/test_plans.py checks that no route among them depends on an entitlement and calls each
one as a Free and as a lapsed shop.

Subscriptions
-------------
A subscription is held by a billing account (the owner's user id for now; P10.3 and P10.10 add distributors and
lenders), covers one shop (up to the plan's outlets on Business) and runs from starts_at to ends_at (exclusive).
Every change writes one subscription_events row with the actor and the reason, in the same transaction.

  * Sources: shop_paid, advance, sponsored (a distributor's partner seat, P10.3) and guarantee_credit.
  * Statuses: advance (paid ahead, starts on billing_starts_on), active, lapsed, cancelled and refunded (P9.5).
  * Before NEARSHOP_BILLING_STARTS_ON (1 Aug 2027) a shop can only pay a refundable advance; from that day no new
    advance is taken, and a shop-paid plan starts at once.
  * Founding offer: Growth for a year at the catalogue's founding price, for the first NEARSHOP_FOUNDING_OFFER_CAP
    shops in NEARSHOP_FOUNDING_OFFER_CITIES, counted in order of first payment, advances included. The slot is taken
    in the transaction that records the first payment, from one counter document (counters, _id founding_offer), so
    two payments at once can never take slot 100 twice; the unique index on founding_shop_id gives a shop one
    founding offer ever. A slot is not given back when a founding subscription is cancelled or refunded.

The subscriptions job
---------------------
run_jobs (the subscriptions job in app/jobs.py, every tick, under the P1.7 lease) does, in this order:

  1. activate_advances: from billing_starts_on, each advance becomes active for one year from that day, at the price
     it was paid at (the founding price when it took a founding slot).
  2. apply_guarantees: the outcome guarantee. When a paid quarter (shop_paid or advance, on a plan with
     outcome_guarantee) ends with fewer than NEARSHOP_PLAN_GUARANTEE_MIN_REQUESTS customer requests (P9.2, the
     covered shops together), ends_at moves NEARSHOP_PLAN_GUARANTEE_CREDIT_MONTHS later, with a guarantee_credit
     event and a notice to the owner. Quarters run from the paid term's start; the credited months are not paid, so
     they earn no further credit.
  3. lapse_due: at ends_at without a renewal the status becomes lapsed (effective_plan already treats the shop as
     Free from that moment), with a notice to the owner. Nothing about the shop changes: every free tool and the same
     visibility in search.
  4. send_reminders: 30, 7 and 1 days (IST calendar days) before ends_at, through notify, once per stage per end date.
  5. alert_unused_advances: an advance still not active NEARSHOP_ADVANCE_ALERT_DAYS (300) days after its payment
     raises an admin alert (in-app to every admin, and a warning event), once, because an advance must be used
     within 365 days (plan section 8.18).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Collection, Database, utcnow
from app.core.deps import DB, OwnerShop
from app.core.errors import AppError, Conflict, NotFound
from app.models import Shop
from app.models.billing import (
    PAID_SOURCES,
    Actor,
    Period,
    Plan,
    PlanCode,
    Subscription,
    SubscriptionEvent,
    SubscriptionSource,
    SubscriptionStatus,
)
from app.models.core import months_after
from app.services import leads
from app.services.notifications import notify

log = logging.getLogger(__name__)

PLANS = "plans"
SUBSCRIPTIONS = "subscriptions"
EVENTS = "subscription_events"
FOUNDING_COUNTER = "founding_offer"  # its document in counters: seq is the number of founding slots taken

REMINDER_DAYS = (30, 7, 1)  # reminders this many IST days before ends_at (plan section 8.18)
REMINDER_WINDOW = timedelta(days=max(REMINDER_DAYS) + 1)
QUARTER_MONTHS = 3
PERIOD_MONTHS = {Period.YEAR: 12, Period.MONTH: 1}
PLAN_LINK = "/owner/plan"
ADVANCE_ALERT_WARNING = "advance subscription not active"

# The kinds of the notifications this module sends.
KIND_REMINDER, KIND_LAPSED, KIND_GUARANTEE, KIND_ACTIVATED = (
    "plan_reminder", "plan_lapsed", "plan_guarantee_credit", "plan_activated")
KIND_ADVANCE_ALERT = "advance_unused"

# Never behind a payment (plan section 8.17): these endpoints never ask for an entitlement, on any plan, lapsed or not.
# The always-free tools that exist today: listings and stock edits, Excel and CSV import, reservation and order
# alerts and queues, and the trust signals (verification and reviews). WhatsApp stock replies (P4.4), the stock check
# (P9.3), the QR standee and the monthly statement (P9.9) add their endpoints here when they are built.
FREE_TOOLS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/owner/products"),
    ("POST", "/api/owner/products"),
    ("PATCH", "/api/owner/products/{product_id}"),
    ("PUT", "/api/owner/products/{product_id}/stock"),
    ("DELETE", "/api/owner/products/{product_id}"),
    ("POST", "/api/owner/products/{product_id}/image"),
    ("GET", "/api/owner/catalog"),
    ("POST", "/api/owner/catalog/add"),
    ("GET", "/api/owner/import/template"),
    ("POST", "/api/owner/import/preview"),
    ("POST", "/api/owner/import/commit"),
    ("GET", "/api/owner/overview"),
    ("GET", "/api/owner/reservations"),
    ("GET", "/api/owner/reservations/{reservation_id}"),
    ("POST", "/api/owner/reservations/{reservation_id}/{action}"),
    ("GET", "/api/owner/orders"),
    ("GET", "/api/owner/orders/{order_id}"),
    ("POST", "/api/owner/orders/{order_id}/{action}"),
    ("GET", "/api/owner/verification"),
    ("POST", "/api/owner/verification"),
    ("GET", "/api/owner/reviews"),
    ("POST", "/api/owner/reviews/{review_id}/reply"),
    ("PUT", "/api/owner/reviews/{review_id}/reply"),
    ("GET", "/api/owner/shop"),
    ("PATCH", "/api/owner/shop"),
)


class PlanRefused(AppError):
    status_code = 409
    code = "plan_refused"


class PlanRequired(AppError):
    status_code = 402
    code = "plan_required"


# ---------------------------------------------------------------- collections and times


def plans(db: Database) -> Collection[Plan]:
    return Collection(db, PLANS, Plan)


def subscriptions(db: Database) -> Collection[Subscription]:
    return Collection(db, SUBSCRIPTIONS, Subscription)


def events(db: Database) -> Collection[SubscriptionEvent]:
    return Collection(db, EVENTS, SubscriptionEvent)


def billing_start() -> datetime:
    """The start of NEARSHOP_BILLING_STARTS_ON (IST), as a naive UTC time."""
    return leads.ist_start(settings.billing_starts_on)


def _months(period: Period) -> int:
    return PERIOD_MONTHS[Period(period)]


# ---------------------------------------------------------------- the catalogue


def catalogue_version(db: Database, at: datetime | None = None) -> int:
    """The catalogue version in force at `at`: the highest version whose effective_from has passed. Raises
    LookupError when there is none (the migration inserts version 1)."""
    at = at or utcnow()
    row = plans(db).raw.find_one({"effective_from": {"$lte": at}}, {"version": 1}, sort=[("version", -1)],
                                 session=db.session)
    if row is None:
        raise LookupError("no plan catalogue is in force: run the migrations")
    return int(row["version"])


def catalogue(db: Database, at: datetime | None = None, version: int | None = None) -> dict[str, Plan]:
    """Every plan of a catalogue version (by default the one in force at `at`), by code."""
    version = catalogue_version(db, at) if version is None else version
    return {p.code.value: p for p in plans(db).find({"version": version}, sort=[("code", 1)])}


def plan_in_force(db: Database, code: PlanCode | str, at: datetime | None = None) -> Plan:
    row = catalogue(db, at).get(PlanCode(code).value)
    if row is None:
        raise NotFound(f"There is no {code} plan in the current catalogue")
    return row


PUBLISHABLE = ("name", "price_year_paise", "price_month_paise", "founding_year_paise", "on_sale")


def publish_catalogue(db: Database, *, admin_id: int, reason: str, changes: dict[str, dict[str, Any]],
                      effective_from: datetime | None = None, now: datetime | None = None) -> int:
    """Publish a new catalogue version: a copy of the latest version with `changes` (per plan code: any of
    PUBLISHABLE) applied, effective from `effective_from` (default now, never earlier). Entitlements are copied as
    they are. Existing subscriptions keep their version until they renew. Returns the new version number. Raises
    Conflict when another version was published at the same moment, AppError for an invalid change."""
    now = now or utcnow()
    effective_from = max(effective_from or now, now)
    latest = plans(db).raw.find_one({}, {"version": 1}, sort=[("version", -1)])
    if latest is None:
        raise LookupError("no plan catalogue to copy: run the migrations")
    base = catalogue(db, version=int(latest["version"]))
    unknown = set(changes) - set(base)
    if unknown:
        raise AppError(f"Unknown plan code(s): {', '.join(sorted(unknown))}", code="unknown_plan", status_code=422)
    version = int(latest["version"]) + 1
    rows: list[Plan] = []
    for code, row in base.items():
        change = {k: v for k, v in (changes.get(code) or {}).items() if k in PUBLISHABLE}
        new = row.model_copy(update={**change, "id": 0, "version": version, "effective_from": effective_from,
                                     "published_at": now, "published_by": admin_id, "reason": reason})
        if code == PlanCode.FREE and (new.price_year_paise or new.price_month_paise or not new.on_sale):
            raise AppError("Free stays free and on sale", code="free_is_free", status_code=422)
        rows.append(new)
    try:
        db.transaction(lambda: plans(db).insert_many(rows))
    except DuplicateKeyError:
        raise Conflict("Another catalogue version was published at the same moment; reload and try again",
                       code="catalogue_conflict") from None
    log.info("plan catalogue version %d published by admin %d, effective from %s", version, admin_id, effective_from)
    return version


def plan_out(p: Plan) -> dict[str, Any]:
    return {"id": p.id, "code": p.code.value, "version": p.version, "name": p.name,
            "price_year_paise": p.price_year_paise, "price_month_paise": p.price_month_paise,
            "founding_year_paise": p.founding_year_paise, "on_sale": p.on_sale,
            "entitlements": p.entitlements.model_dump(), "effective_from": _iso(p.effective_from),
            "published_at": _iso(p.published_at), "reason": p.reason}


# ---------------------------------------------------------------- entitlements


def active_subscription(db: Database, shop_id: int, at: datetime | None = None) -> Subscription | None:
    """The active subscription covering the shop at `at`, or None."""
    at = at or utcnow()
    return subscriptions(db).find_one({"shop_ids": shop_id, "status": SubscriptionStatus.ACTIVE.value,
                                       "starts_at": {"$lte": at}, "ends_at": {"$gt": at}}, sort=[("ends_at", -1)])


def effective_plan(db: Database, shop_id: int, at: datetime | None = None) -> dict[str, Any]:
    """The shop's plan and entitlements at `at` (default now): its active subscription's plan row (the version it
    was sold on), else Free from the catalogue in force. The one function every paid feature asks."""
    at = at or utcnow()
    sub = active_subscription(db, shop_id, at)
    plan = plans(db).get(sub.plan_id) if sub is not None else None
    if plan is None:
        sub, plan = None, plan_in_force(db, PlanCode.FREE, at)
    return {"code": plan.code.value, "name": plan.name, "version": plan.version,
            "entitlements": plan.entitlements.model_dump(), "subscription_id": sub.id if sub else None,
            "status": sub.status.value if sub else "free", "source": sub.source.value if sub else None,
            "starts_at": sub.starts_at if sub else None, "ends_at": sub.ends_at if sub else None}


def entitled(db: Database, shop_id: int, feature: str, at: datetime | None = None) -> Any:
    """One entitlement's value for the shop at `at` (through effective_plan)."""
    return effective_plan(db, shop_id, at)["entitlements"][feature]


def require_entitlement(feature: str) -> Callable[..., Any]:
    """A route dependency for a paid feature: 402 plan_required unless the signed-in owner's shop has `feature`
    (true, or a non-teaser demand report). Never used on a FREE_TOOLS route (tests/test_plans.py checks)."""
    def check(shop: OwnerShop, db: DB) -> Shop:
        value = entitled(db, shop.id, feature)
        if not value or value == "teaser":
            raise PlanRequired("This needs a paid plan")
        return shop

    check.__entitlement__ = feature  # type: ignore[attr-defined]
    return Depends(check)


# ---------------------------------------------------------------- the founding offer


def _founding_city(shop: Shop) -> bool:
    return (shop.city or "").strip().lower() in settings.founding_city_list


def founding_taken(db: Database) -> int:
    doc = db.mongo["counters"].find_one({"_id": FOUNDING_COUNTER}, session=db.session)
    return int(doc["seq"]) if doc else 0


def founding_counter(db: Database) -> dict[str, Any]:
    taken = founding_taken(db)
    cap = settings.founding_offer_cap
    return {"cap": cap, "taken": taken, "remaining": max(0, cap - taken), "cities": settings.founding_city_list}


def founding_eligibility(db: Database, shop: Shop) -> dict[str, Any]:
    """Whether the shop can still get the founding offer: in a founding city, never had it, and slots remain."""
    in_city = _founding_city(shop)
    claimed = subscriptions(db).exists({"founding_shop_id": shop.id})
    counter = founding_counter(db)
    return {"eligible": in_city and not claimed and counter["remaining"] > 0, "in_city": in_city,
            "already_claimed": claimed, "remaining": counter["remaining"], "cap": counter["cap"]}


def ensure_founding_counter(db: Database) -> None:
    """Create the counter document, outside any transaction: a transaction reads from the snapshot it started with,
    so a document created alongside it would not be seen."""
    db.mongo["counters"].update_one({"_id": FOUNDING_COUNTER}, {"$setOnInsert": {"seq": 0}}, upsert=True)


def claim_founding_slot(db: Database) -> int | None:
    """Take the next founding slot in db's transaction: its number (1 to the cap), or None when all are taken. One
    conditional increment of one counter document, so concurrent claims conflict and retry, and the cap holds. The
    caller has run ensure_founding_counter before its transaction began."""
    doc = db.mongo["counters"].find_one_and_update(
        {"_id": FOUNDING_COUNTER, "seq": {"$lt": settings.founding_offer_cap}}, {"$inc": {"seq": 1}},
        return_document=ReturnDocument.AFTER, session=db.session)
    return int(doc["seq"]) if doc else None


# ---------------------------------------------------------------- subscription changes


def _event(db: Database, sub: Subscription, kind: str, actor: Actor, reason: str, *,
           status_before: SubscriptionStatus | None, ends_at_before: datetime | None, at: datetime) -> None:
    events(db).insert(SubscriptionEvent(subscription_id=sub.id, shop_ids=sub.shop_ids, kind=kind, actor=actor,
                                        reason=reason, status_before=status_before, status_after=sub.status,
                                        ends_at_before=ends_at_before, ends_at_after=sub.ends_at, at=at))


def _guarantee_cursor(term_start: datetime, paid_until: datetime) -> dict[str, Any]:
    first = months_after(term_start, QUARTER_MONTHS)
    return {"term_start": term_start, "quarters_done": 0, "next_quarter_end": first if first <= paid_until else None}


def create_subscription(db: Database, *, shop_ids: list[int], plan_code: PlanCode | str, period: Period | str,
                        source: SubscriptionSource | str, actor: Actor, reason: str, paid_at: datetime | None = None,
                        months: int | None = None, now: datetime | None = None) -> Subscription:
    """Record a subscription (the first payment of a shop-paid plan or an advance, a sponsored seat, or a credit),
    with its created event, in one transaction. A Growth year paid by a shop in a founding city takes a founding slot
    while any remain, and is then priced at the founding price. Raises PlanRefused (409) when the rules forbid it."""
    now = now or utcnow()
    source, period = SubscriptionSource(source), Period(period)
    shops = db.shops.by_ids(shop_ids)
    if not shop_ids or len(shops) != len(set(shop_ids)):
        raise NotFound("Shop not found")
    owners = {s.owner_id for s in shops.values()}
    if len(owners) != 1:
        raise PlanRefused("The shops of one subscription must have the same owner")
    account_id = owners.pop()
    plan = plan_in_force(db, plan_code, now)
    if plan.code == PlanCode.FREE:
        raise PlanRefused("Free needs no subscription")
    if len(set(shop_ids)) > plan.entitlements.outlets:
        raise PlanRefused(f"{plan.name} covers at most {plan.entitlements.outlets} outlet(s)")
    paid = source in PAID_SOURCES
    if paid and months is not None:
        raise PlanRefused("A paid plan runs for its period; extend it afterwards for any extra months")
    if paid and not plan.on_sale:
        raise PlanRefused(f"{plan.name} is not on sale")
    list_price = plan.price_year_paise if period == Period.YEAR else plan.price_month_paise
    if paid and list_price is None:
        raise PlanRefused(f"{plan.name} is not sold by the {period.value}")
    start_billing = billing_start()
    if source == SubscriptionSource.ADVANCE:
        if now >= start_billing:
            raise PlanRefused("Billing has started: record a shop-paid plan instead of an advance")
        if period != Period.YEAR:
            raise PlanRefused("An advance is for a year")
        starts_at, status_ = start_billing, SubscriptionStatus.ADVANCE
    else:
        if source == SubscriptionSource.SHOP_PAID and now < start_billing:
            raise PlanRefused(f"Paid plans start on {settings.billing_starts_on:%d %b %Y}; record an advance until then")
        starts_at, status_ = now, SubscriptionStatus.ACTIVE
    ends_at = months_after(starts_at, months or _months(period))
    founding_shop = next(iter(shops.values())) if len(shops) == 1 else None
    may_found = (paid and plan.code == PlanCode.GROWTH and period == Period.YEAR and plan.founding_year_paise is not None
                 and founding_shop is not None and _founding_city(founding_shop))

    if may_found:
        ensure_founding_counter(db)

    def write() -> Subscription:
        live = subscriptions(db).find_one({"shop_ids": {"$in": list(shop_ids)}, "ends_at": {"$gt": now},
                                           "status": {"$in": [SubscriptionStatus.ACTIVE.value,
                                                              SubscriptionStatus.ADVANCE.value]}})
        if live is not None:
            raise PlanRefused(f"The shop already has subscription {live.id} ({live.status.value})")
        slot = None
        if may_found and not subscriptions(db).exists({"founding_shop_id": founding_shop.id}):
            slot = claim_founding_slot(db)
        price = plan.founding_year_paise if slot else (list_price or 0)
        if source == SubscriptionSource.GUARANTEE_CREDIT:
            price = 0
        sub = Subscription(
            account_id=account_id, shop_ids=sorted(set(shop_ids)), plan_id=plan.id,
            plan_code=plan.code, plan_version=plan.version, period=period, price_paise=price, founding=slot is not None,
            founding_slot=slot, founding_shop_id=founding_shop.id if slot else None, source=source, status=status_,
            starts_at=starts_at, ends_at=ends_at, paid_until=ends_at if paid else starts_at,
            paid_at=(paid_at or now) if paid else None, created_at=now, updated_at=now,
            **(_guarantee_cursor(starts_at, ends_at) if paid else {}))
        subscriptions(db).insert(sub)
        _event(db, sub, "created", actor, reason, status_before=None, ends_at_before=None, at=now)
        return sub

    try:
        return db.transaction(write)
    except DuplicateKeyError:
        raise PlanRefused("This shop has already had the founding offer") from None


def _load(db: Database, sub_id: int) -> Subscription:
    sub = subscriptions(db).get(sub_id)
    if sub is None:
        raise NotFound("Subscription not found")
    return sub


def _change(db: Database, sub_id: int, kind: str, actor: Actor, reason: str, now: datetime,
            apply: Callable[[Subscription], dict[str, Any] | None],
            after: Callable[[Subscription], None] | None = None) -> Subscription | None:
    """Load the subscription in a transaction, let apply return the fields to set (None: no change), and write them,
    the event and anything after (a notice) does, together. Returns the changed subscription, or None when apply made
    no change."""

    def write() -> Subscription | None:
        sub = _load(db, sub_id)
        before_status, before_end = sub.status, sub.ends_at
        fields = apply(sub)
        if fields is None:
            return None
        subscriptions(db).set(sub, **fields, updated_at=now)
        _event(db, sub, kind, actor, reason, status_before=before_status, ends_at_before=before_end, at=now)
        if after is not None:
            after(sub)
        return sub

    return db.transaction(write)


def extend(db: Database, sub_id: int, *, actor: Actor, reason: str, months: int | None = None,
           ends_at: datetime | None = None, now: datetime | None = None) -> Subscription:
    """Move the end date later, by months or to ends_at (admin goodwill: not paid, so no guarantee credit). A lapsed
    subscription extended past now is active again."""
    now = now or utcnow()

    def apply(sub: Subscription) -> dict[str, Any]:
        if sub.status in (SubscriptionStatus.CANCELLED, SubscriptionStatus.REFUNDED):
            raise PlanRefused(f"A {sub.status.value} subscription cannot be extended")
        new_end = ends_at or months_after(sub.ends_at, months or 0)
        if new_end <= sub.ends_at:
            raise PlanRefused("The new end date must be later than the current one")
        fields: dict[str, Any] = {"ends_at": new_end, "reminded": []}
        if sub.status == SubscriptionStatus.LAPSED and new_end > now:
            fields["status"] = SubscriptionStatus.ACTIVE
        return fields

    return _change(db, sub_id, "extended", actor, reason, now, apply)


def cancel(db: Database, sub_id: int, *, actor: Actor, reason: str, now: datetime | None = None) -> Subscription:
    """Cancel at once: the shop is on Free from now. Refunds are P9.5's."""
    now = now or utcnow()

    def apply(sub: Subscription) -> dict[str, Any]:
        if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.ADVANCE):
            raise PlanRefused(f"A {sub.status.value} subscription cannot be cancelled")
        return {"status": SubscriptionStatus.CANCELLED}

    return _change(db, sub_id, "cancelled", actor, reason, now, apply)


def renew(db: Database, sub_id: int, *, actor: Actor, reason: str, paid_at: datetime | None = None,
          now: datetime | None = None) -> Subscription:
    """A paid renewal (P9.5 and P9.6 call it once the payment is recorded): one more period on the catalogue version
    in force now, at its list price (the founding price is for the first year only). An active subscription's new term
    starts at its end date (after any guarantee credit); a lapsed one's starts now."""
    now = now or utcnow()

    def apply(sub: Subscription) -> dict[str, Any]:
        if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.LAPSED):
            raise PlanRefused(f"A {sub.status.value} subscription cannot be renewed")
        plan = plan_in_force(db, sub.plan_code, now)
        price = plan.price_year_paise if sub.period == Period.YEAR else plan.price_month_paise
        if price is None:
            raise PlanRefused(f"{plan.name} is not sold by the {sub.period.value}")
        contiguous = sub.status == SubscriptionStatus.ACTIVE and sub.ends_at > now
        term_start = sub.ends_at if contiguous else now
        new_end = months_after(term_start, _months(sub.period))
        fields: dict[str, Any] = {"plan_id": plan.id, "plan_code": plan.code, "plan_version": plan.version,
                                  "price_paise": price, "founding": False, "status": SubscriptionStatus.ACTIVE,
                                  "ends_at": new_end, "paid_until": new_end, "paid_at": paid_at or now, "reminded": []}
        if not contiguous:
            fields["starts_at"] = now
        if sub.source in PAID_SOURCES:
            # A monthly plan renewed without a break keeps counting its quarter; any other renewal starts a new one.
            keep = contiguous and sub.ends_at == sub.paid_until and sub.next_quarter_end is not None
            fields.update({"next_quarter_end": sub.next_quarter_end} if keep else _guarantee_cursor(term_start, new_end))
        return fields

    return _change(db, sub_id, "renewed", actor, reason, now, apply)


# ---------------------------------------------------------------- the subscriptions job

SYSTEM = Actor(type="system")


def _owner_notice(db: Database, sub: Subscription, kind: str, title: str, body: str) -> None:
    if sub.account_id:
        notify(db, sub.account_id, kind, title, body, PLAN_LINK)


def _date(at: datetime) -> str:
    day = leads.ist_day(at)
    return f"{day.day} {day:%B %Y}"


def activate_advances(db: Database, now: datetime) -> int:
    """From billing_starts_on, each advance becomes active for one year from that day."""
    start = billing_start()
    if now < start:
        return 0
    done = 0
    for row in subscriptions(db).find_raw({"status": SubscriptionStatus.ADVANCE.value}, {"_id": 1}):
        def apply(sub: Subscription) -> dict[str, Any] | None:
            if sub.status != SubscriptionStatus.ADVANCE:
                return None
            end = months_after(start, _months(sub.period))
            return {"status": SubscriptionStatus.ACTIVE, "starts_at": start, "ends_at": end, "paid_until": end,
                    "reminded": [], **_guarantee_cursor(start, end)}

        def after(sub: Subscription) -> None:
            _owner_notice(db, sub, KIND_ACTIVATED, "Your NearShop plan has started",
                          f"Your advance payment now covers your plan until {_date(sub.ends_at)}.")

        if _change(db, row["_id"], "activated", SYSTEM, "Billing started: the advance is used for its first year",
                   now, apply, after) is not None:
            done += 1
    return done


def quarter_requests(db: Database, shop_ids: Iterable[int], start: datetime, end: datetime) -> int:
    """Customer requests (P9.2) to the shops together in [start, end)."""
    counts = leads.compute_counts(db, start, end, list(shop_ids))
    return sum(c["customer_requests"] for c in counts.values())


GUARANTEE_CATCH_UP = 8  # quarters one run may settle per subscription (a job that was down for two years)


def apply_guarantees(db: Database, now: datetime) -> dict[str, int]:
    """The outcome guarantee for every paid quarter that has ended (module docstring), oldest quarter first, catching
    up on quarters a stopped job missed."""
    credited = met = 0
    for _ in range(GUARANTEE_CATCH_UP):
        c, m = _guarantee_pass(db, now)
        credited, met = credited + c, met + m
        if not c and not m:
            break
    return {"guarantee_credits": credited, "guarantee_met": met}


def _guarantee_pass(db: Database, now: datetime) -> tuple[int, int]:
    """One quarter for each subscription with a paid quarter that has ended."""
    credited = met = 0
    due = subscriptions(db).find({"status": SubscriptionStatus.ACTIVE.value, "next_quarter_end": {"$lte": now},
                                  "source": {"$in": [s.value for s in PAID_SOURCES]}})
    for row in due:
        plan = plans(db).get(row.plan_id)
        q_end = row.next_quarter_end
        q_start = months_after(row.term_start, QUARTER_MONTHS * row.quarters_done) if row.term_start else row.starts_at
        if plan is None or not plan.entitlements.outcome_guarantee:
            subscriptions(db).update_one({"_id": row.id, "next_quarter_end": q_end}, {"$set": {"next_quarter_end": None}})
            continue
        requests = quarter_requests(db, row.shop_ids, q_start, q_end)
        short = requests < settings.plan_guarantee_min_requests

        def apply(sub: Subscription, q_end=q_end, short=short) -> dict[str, Any] | None:
            if sub.next_quarter_end != q_end or sub.status != SubscriptionStatus.ACTIVE:
                return None  # another run did it
            done = sub.quarters_done + 1
            following = months_after(sub.term_start, QUARTER_MONTHS * (done + 1)) if sub.term_start else None
            fields: dict[str, Any] = {"quarters_done": done,
                                      "next_quarter_end": following if following and following <= sub.paid_until
                                      else None}
            if short:
                fields.update(ends_at=months_after(sub.ends_at, settings.plan_guarantee_credit_months), reminded=[])
            return fields

        def after(sub: Subscription, short=short, requests=requests) -> None:
            if short:
                _owner_notice(db, sub, KIND_GUARANTEE, f"{settings.plan_guarantee_credit_months} months added to "
                              f"your plan", f"Your shop had {requests} customer request(s) through NearShop last "
                              f"quarter, fewer than we promise. Your plan now runs until {_date(sub.ends_at)} at no "
                              f"cost.")

        kind = "guarantee_credit" if short else "guarantee_met"
        reason = (f"{requests} customer request(s) in the quarter {_date(q_start)} to {_date(q_end)}; "
                  f"the guarantee is {settings.plan_guarantee_min_requests}")
        if _change(db, row.id, kind, SYSTEM, reason, now, apply, after) is None:
            continue
        if short:
            credited += 1
        else:
            met += 1
    return credited, met


def lapse_due(db: Database, now: datetime) -> int:
    """Active subscriptions past their end date become lapsed: the shop is on Free, and nothing else changes."""
    done = 0
    for row in subscriptions(db).find_raw({"status": SubscriptionStatus.ACTIVE.value, "ends_at": {"$lte": now}},
                                          {"_id": 1}):
        def apply(sub: Subscription) -> dict[str, Any] | None:
            if sub.status != SubscriptionStatus.ACTIVE or sub.ends_at > now:
                return None
            return {"status": SubscriptionStatus.LAPSED}

        def after(sub: Subscription) -> None:
            _owner_notice(db, sub, KIND_LAPSED, "Your NearShop plan has ended",
                          "Your shop is now on the Free plan. Your listings, stock tools and place in search are "
                          "unchanged. Renew any time to get your plan's extras back.")

        if _change(db, row["_id"], "lapsed", SYSTEM, "The end date passed without a renewal", now, apply,
                   after) is not None:
            done += 1
    return done


def reminder_stage(ends_at: datetime, now: datetime) -> int | None:
    """The reminder stage due at now: the smallest of REMINDER_DAYS at least the IST days left, or None."""
    days_left = (leads.ist_day(ends_at) - leads.ist_day(now)).days
    stages = [d for d in REMINDER_DAYS if days_left <= d]
    return min(stages) if days_left >= 1 and stages else None


def send_reminders(db: Database, now: datetime) -> int:
    """Reminders 30, 7 and 1 days before the end, once per stage per end date."""
    sent = 0
    for row in subscriptions(db).find({"status": SubscriptionStatus.ACTIVE.value,
                                       "ends_at": {"$gt": now, "$lte": now + REMINDER_WINDOW}}):
        stage = reminder_stage(row.ends_at, now)
        if stage is None or stage in row.reminded:
            continue

        def apply(sub: Subscription, stage=stage) -> dict[str, Any] | None:
            if stage in sub.reminded or sub.status != SubscriptionStatus.ACTIVE:
                return None
            days_left = (leads.ist_day(sub.ends_at) - leads.ist_day(now)).days
            left = "tomorrow" if days_left == 1 else f"in {days_left} days"
            _owner_notice(db, sub, KIND_REMINDER, f"Your NearShop plan ends {left}",
                          f"Your plan ends on {_date(sub.ends_at)}. Renew to keep its extras; your shop stays listed "
                          f"and every stock tool stays free either way.")
            return {"reminded": [*sub.reminded, stage]}

        if _change(db, row.id, "reminder", SYSTEM, f"Reminder {stage} day(s) before the end", now, apply) is not None:
            sent += 1
    return sent


def alert_unused_advances(db: Database, now: datetime) -> int:
    """An advance still not active NEARSHOP_ADVANCE_ALERT_DAYS after its payment: alert the admins once."""
    cutoff = now - timedelta(days=settings.advance_alert_days)
    rows = subscriptions(db).find_raw({"status": SubscriptionStatus.ADVANCE.value, "paid_at": {"$lte": cutoff},
                                       "advance_alerted_at": None}, {"_id": 1})
    if not rows:
        return 0
    admins = [u["_id"] for u in db.users.find_raw({"role": "admin", "is_active": True}, {"_id": 1})]
    alerted = 0
    for row in rows:
        def apply(sub: Subscription) -> dict[str, Any] | None:
            if sub.status != SubscriptionStatus.ADVANCE or sub.advance_alerted_at is not None:
                return None
            used_by = sub.paid_at + timedelta(days=365) if sub.paid_at else None
            for admin_id in admins:
                notify(db, admin_id, KIND_ADVANCE_ALERT, f"Advance subscription {sub.id} is not active yet",
                       f"Paid on {_date(sub.paid_at)}, it must be used by {_date(used_by)} or refunded "
                       f"(plan section 8.18).", "/admin/subscriptions?status=advance")
            return {"advance_alerted_at": now}

        reason = f"Not active {settings.advance_alert_days} days after its payment"
        if _change(db, row["_id"], "advance_alert", SYSTEM, reason, now, apply) is not None:
            alerted += 1
            log.warning("%s: subscription %s, %d days after its payment", ADVANCE_ALERT_WARNING, row["_id"],
                        settings.advance_alert_days)
    return alerted


def run_jobs(db: Database, now: datetime | None = None) -> dict[str, int]:
    """One run of the subscriptions job (module docstring), in order."""
    now = now or utcnow()
    activated = activate_advances(db, now)
    guarantees = apply_guarantees(db, now)
    lapsed = lapse_due(db, now)
    reminders = send_reminders(db, now)
    alerts = alert_unused_advances(db, now)
    return {"activated": activated, **guarantees, "lapsed": lapsed, "reminders": reminders, "advance_alerts": alerts}


# ---------------------------------------------------------------- views


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() + "Z" if at else None


def subscription_out(sub: Subscription) -> dict[str, Any]:
    return {"id": sub.id, "account_type": sub.account_type.value, "account_id": sub.account_id,
            "shop_ids": sub.shop_ids, "plan_code": sub.plan_code.value, "plan_version": sub.plan_version,
            "period": sub.period.value, "price_paise": sub.price_paise, "founding": sub.founding,
            "founding_slot": sub.founding_slot, "source": sub.source.value, "status": sub.status.value,
            "starts_at": _iso(sub.starts_at), "ends_at": _iso(sub.ends_at), "paid_until": _iso(sub.paid_until),
            "paid_at": _iso(sub.paid_at), "created_at": _iso(sub.created_at), "updated_at": _iso(sub.updated_at)}


def event_out(e: SubscriptionEvent) -> dict[str, Any]:
    return {"id": e.id, "kind": e.kind, "actor": e.actor.model_dump(), "reason": e.reason,
            "status_before": e.status_before.value if e.status_before else None, "status_after": e.status_after.value,
            "ends_at_before": _iso(e.ends_at_before), "ends_at_after": _iso(e.ends_at_after), "at": _iso(e.at)}


def guarantee_progress(db: Database, sub: Subscription | None, now: datetime) -> dict[str, Any]:
    """This quarter's customer requests against the guarantee, for a subscription the guarantee covers."""
    needed = settings.plan_guarantee_min_requests
    plan = plans(db).get(sub.plan_id) if sub is not None else None
    if (sub is None or plan is None or not plan.entitlements.outcome_guarantee or sub.source not in PAID_SOURCES
            or sub.next_quarter_end is None or sub.term_start is None):
        return {"applies": False, "needed": needed}
    q_start = months_after(sub.term_start, QUARTER_MONTHS * sub.quarters_done)
    requests = quarter_requests(db, sub.shop_ids, q_start, min(now, sub.next_quarter_end)) if q_start < now else 0
    return {"applies": True, "quarter_start": _iso(q_start), "quarter_end": _iso(sub.next_quarter_end),
            "customer_requests": requests, "needed": needed, "met": requests >= needed,
            "credit_months": settings.plan_guarantee_credit_months}


def owner_plan(db: Database, shop: Shop, now: datetime | None = None) -> dict[str, Any]:
    """The owner's plan screen: the plan, its entitlements and end date, the founding offer, this quarter's guarantee
    progress, any advance waiting for billing to start, and the plans on sale."""
    now = now or utcnow()
    current = effective_plan(db, shop.id, now)
    sub = subscriptions(db).get(current["subscription_id"]) if current["subscription_id"] else None
    advance = subscriptions(db).find_one({"shop_ids": shop.id, "status": SubscriptionStatus.ADVANCE.value})
    plans_now = catalogue(db, now)
    growth = plans_now.get(PlanCode.GROWTH.value)
    return {"plan": {**current, "starts_at": _iso(current["starts_at"]), "ends_at": _iso(current["ends_at"])},
            "subscription": subscription_out(sub) if sub else None,
            "advance": subscription_out(advance) if advance else None,
            "founding": {**founding_eligibility(db, shop),
                         "price_year_paise": growth.founding_year_paise if growth else None},
            "guarantee": guarantee_progress(db, sub, now), "billing_starts_on": settings.billing_starts_on.isoformat(),
            "on_sale": [plan_out(p) for p in plans_now.values() if p.on_sale]}
