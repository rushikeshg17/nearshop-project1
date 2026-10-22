"""UPI Autopay for monthly plans (P9.6; plan section 8.18; docs/runbooks/billing.md).

A shop can pay for Growth by the month by UPI Autopay. NearShop never holds the mandate or any card or bank detail: an
RBI-authorised gateway does (Razorpay subscriptions with UPI Autopay; B22: an account in the company's name). This
module collects only NearShop's own fees, into NearShop's own account through the gateway's settlement; it never
collects money for a shop (plan section 8.8).

The flow
--------
  1. The owner chooses Growth monthly (POST /api/owner/billing/autopay). create_mandate makes a gateway subscription
     for the monthly price in force (plus GST once NearShop is registered, P9.5) and an autopay_mandates row (status
     created) holding the gateway's checkout link, where the owner approves the mandate in their UPI app. A shop
     already on Growth monthly (a cancelled mandate's month still running) is renewed from the end of that month.
  2. The gateway calls POST /api/webhooks/autopay (app/api/routes/webhooks.py). Every call must carry a valid
     X-Razorpay-Signature, the HMAC-SHA256 of the raw body keyed with NEARSHOP_AUTOPAY_WEBHOOK_SECRET; each event id
     (X-Razorpay-Event-Id) is processed once (gateway_events, unique event_id), in the same transaction as its effect,
     so a retried or replayed event changes nothing:
       * subscription.authenticated or subscription.activated: the mandate is authorised.
       * subscription.charged: a debit succeeded. billing.settle_autopay_debit writes the payment, the subscription's
         first or next month and the receipt or invoice, with the event, in one transaction. A debit that cannot be
         applied goes to P9.5's unmatched queue, with the reason, where admins already look.
       * subscription.pending: a debit failed and the gateway will retry; the owner is told once per run of failures.
         While the gateway retries, the plan is held, not lapsed (plans.autopay_hold, NEARSHOP_AUTOPAY_GRACE_DAYS).
       * subscription.halted: the gateway's last retry failed. The mandate is halted, the plan lapses to Free at once
         (plans.py: every free tool and the same place in search), and the owner is told how to restart.
       * subscription.cancelled or subscription.completed: the mandate ended at the gateway (the owner cancelled it in
         their UPI app, or its debits ran out). The plan ends at the end of the paid month: it is no longer renewed
         and lapses at its end date.
     Any other event is recorded and ignored.
  3. The owner can cancel in NearShop (POST /api/owner/billing/autopay/cancel): the gateway subscription is cancelled
     first, then the mandate, with the same effect as a cancellation at the gateway.
  4. reconcile (the billing_reconciliation job, once per IST day) reads the gateway's settlement reports of the last
     NEARSHOP_AUTOPAY_RECONCILE_DAYS days and compares them with the payment rows: a settled payment with no payment
     row, a settled amount that differs, and a payment not settled NEARSHOP_AUTOPAY_SETTLEMENT_DAYS after it was paid
     each become a credit in the unmatched queue (source autopay), once.

Clients
-------
get_client returns RazorpayClient when NEARSHOP_AUTOPAY_PROVIDER is razorpay and the keys are set (never in the test
environment), FAKE (an in-memory stand-in that also builds signed events and settlement reports) when the provider is
fake in development and test, and None while Autopay is off or not configured. Test-mode keys only outside production
(app/core/config.py refuses a live key id there).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pymongo.errors import DuplicateKeyError

from app.core.config import Settings, settings
from app.core.database import Collection, Database, utcnow
from app.core.errors import AppError, Conflict, NotFound
from app.models import Shop
from app.models.base import Document, Timestamps
from app.models.billing import Actor, Period, PlanCode, Subscription, SubscriptionStatus
from app.services import billing, leads, plans
from app.services.notifications import notify

log = logging.getLogger(__name__)

MANDATES = plans.AUTOPAY_MANDATES
EVENTS = "gateway_events"
PLAN_LINK = plans.PLAN_LINK
SYSTEM = Actor(type="system")
KIND_AUTOPAY = "autopay"  # the owner's Autopay notices
SIGNATURE_HEADER = "x-razorpay-signature"
EVENT_ID_HEADER = "x-razorpay-event-id"
# The reason on a credit the reconciliation records for a payment the gateway settled before its event arrived.
SETTLED_UNRECORDED = "settled by the gateway ({settlement_id}), but no Autopay payment is recorded"
SETTLED_UNRECORDED_ID = re.compile(r"settled by the gateway \(([^)]*)\), but no Autopay payment is recorded")

AUTHORISED_EVENTS = ("subscription.authenticated", "subscription.activated")
CHARGED_EVENT = "subscription.charged"
FAILED_EVENT = "subscription.pending"
HALTED_EVENT = "subscription.halted"
ENDED_EVENTS = ("subscription.cancelled", "subscription.completed")


class AutopayNotConfigured(AppError):
    status_code = 503
    code = "autopay_not_configured"


class GatewayError(AppError):
    status_code = 502
    code = "gateway_error"


# ---------------------------------------------------------------- models and collections


class MandateStatus(StrEnum):
    CREATED = "created"  # waiting for the owner to approve it in their UPI app
    AUTHORISED = plans.AUTOPAY_RENEWING  # the gateway debits each month
    HALTED = "halted"  # the gateway's last retry failed
    CANCELLED = "cancelled"  # by the owner (in NearShop or their UPI app), or its debits ran out


LIVE = (MandateStatus.CREATED, MandateStatus.AUTHORISED)


class Mandate(Document, Timestamps):
    """Collection autopay_mandates: one gateway subscription with a UPI Autopay mandate, for one shop. The gateway
    holds the mandate; nothing here is a card or bank detail."""

    shop_id: int
    owner_id: int
    billing_account_id: int
    provider: str
    gateway_subscription_id: str
    gateway_plan_id: str | None = None
    checkout_url: str | None = None
    plan_code: PlanCode = PlanCode.GROWTH
    plan_version: int
    amount_paise: int  # the monthly price before GST, as when it was set up
    gst_mode: billing.GstMode
    total_paise: int  # what the gateway debits each month
    status: MandateStatus = MandateStatus.CREATED
    subscription_id: int | None = None  # the subscription each debit renews (set by the first debit)
    debits: int = 0
    failed_debits: int = 0  # failures since the last successful debit
    created_by: Actor
    authorised_at: datetime | None = None
    last_debit_at: datetime | None = None
    last_failure_at: datetime | None = None
    halted_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None  # owner (in NearShop) or gateway (the UPI app, or the debits ran out)


class GatewayEvent(Document):
    """Collection gateway_events: each webhook event processed, once (unique event_id), with what it did."""

    event_id: str
    event: str
    gateway_subscription_id: str | None = None
    payment_id: str | None = None
    outcome: str = ""
    received_at: datetime


def mandates(db: Database) -> Collection[Mandate]:
    return Collection(db, MANDATES, Mandate)


def events(db: Database) -> Collection[GatewayEvent]:
    return Collection(db, EVENTS, GatewayEvent)


# ---------------------------------------------------------------- clients


@dataclass
class GatewaySubscription:
    id: str
    checkout_url: str | None
    plan_id: str | None = None


@dataclass
class Settlement:
    """One payment in the gateway's settlement report: gross amount, the gateway's fee and the GST on it, in paise."""

    payment_id: str
    amount_paise: int
    fee_paise: int
    tax_paise: int
    settlement_id: str
    settled_at: datetime


class GatewayClient(Protocol):
    def create_subscription(self, *, amount_paise: int, name: str, total_count: int,
                            notes: dict[str, str]) -> GatewaySubscription: ...

    def cancel_subscription(self, gateway_subscription_id: str) -> None: ...

    def settlements(self, day: date) -> list[Settlement]: ...


def _from_unix(value: Any, default: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return default


class RazorpayClient:
    """Razorpay's subscriptions API (UPI Autopay is a payment method of its subscriptions), with basic auth by the
    key id and secret. Confirm each call in the gateway's test mode on staging before switching on."""

    def __init__(self, config: Settings = settings):
        self.config = config

    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            res = httpx.request(method, f"{self.config.autopay_api_base.rstrip('/')}{path}", timeout=15.0,
                                auth=(self.config.autopay_key_id, self.config.autopay_key_secret.get_secret_value()),
                                **kw)
        except httpx.HTTPError as exc:
            log.warning("gateway %s %s failed: %s", method, path, type(exc).__name__)
            raise GatewayError("The payment gateway could not be reached; try again in a minute") from None
        if res.status_code >= 400:
            log.warning("gateway %s %s answered %s", method, path, res.status_code)
            raise GatewayError(f"The payment gateway refused the request ({res.status_code})")
        return res.json()

    def create_subscription(self, *, amount_paise: int, name: str, total_count: int,
                            notes: dict[str, str]) -> GatewaySubscription:
        plan = self._call("POST", "/plans", json={"period": "monthly", "interval": 1, "notes": notes, "item": {
            "name": name, "amount": amount_paise, "currency": "INR"}})
        sub = self._call("POST", "/subscriptions", json={"plan_id": plan["id"], "total_count": total_count,
                                                         "quantity": 1, "customer_notify": 1, "notes": notes})
        return GatewaySubscription(id=sub["id"], checkout_url=sub.get("short_url"), plan_id=plan["id"])

    def cancel_subscription(self, gateway_subscription_id: str) -> None:
        self._call("POST", f"/subscriptions/{gateway_subscription_id}/cancel", json={"cancel_at_cycle_end": 0})

    def settlements(self, day: date) -> list[Settlement]:
        found: list[Settlement] = []
        skip = 0
        while True:
            page = self._call("GET", "/settlements/recon/combined", params={
                "year": day.year, "month": day.month, "day": day.day, "count": 1000, "skip": skip})
            items = page.get("items") or []
            for item in items:
                if item.get("type") != "payment":
                    continue
                found.append(Settlement(
                    payment_id=str(item.get("entity_id")), amount_paise=int(item.get("amount") or 0),
                    fee_paise=int(item.get("fee") or 0), tax_paise=int(item.get("tax") or 0),
                    settlement_id=str(item.get("settlement_id") or ""),
                    settled_at=_from_unix(item.get("settled_at"), leads.ist_start(day))))
            if len(items) < 1000:
                return found
            skip += len(items)


@dataclass
class FakeGateway:
    """Development and test: subscriptions, debits and settlements kept in memory; nothing leaves the machine. It also
    builds the gateway's webhook events (event_body) and signs them (sign), so the whole flow runs without an account.
    failures makes the next create or cancel calls fail."""

    subscriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    settled: dict[date, list[Settlement]] = field(default_factory=dict)
    failures: int = 0
    _n: int = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_fake{self._n:06d}{time.time_ns() % 1_000_000:06d}"

    def _maybe_fail(self) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise GatewayError("The payment gateway refused the request (fake failure)")

    def create_subscription(self, *, amount_paise: int, name: str, total_count: int,
                            notes: dict[str, str]) -> GatewaySubscription:
        self._maybe_fail()
        sub_id, plan_id = self._next("sub"), self._next("plan")
        self.subscriptions[sub_id] = {"amount": amount_paise, "status": "created", "notes": notes,
                                      "total_count": total_count}
        log.info("fake gateway subscription %s for %s paise a month", sub_id, amount_paise)
        return GatewaySubscription(id=sub_id, checkout_url=f"https://checkout.fake.invalid/{sub_id}", plan_id=plan_id)

    def cancel_subscription(self, gateway_subscription_id: str) -> None:
        self._maybe_fail()
        if gateway_subscription_id in self.subscriptions:
            self.subscriptions[gateway_subscription_id]["status"] = "cancelled"

    def settlements(self, day: date) -> list[Settlement]:
        return list(self.settled.get(day, []))

    # ---- what the gateway would send (tests and local development)

    def event_body(self, event: str, gateway_subscription_id: str, *, payment: dict[str, Any] | None = None,
                   at: datetime | None = None) -> tuple[str, bytes]:
        """An event id and the raw body of one of the gateway's webhook events."""
        at = at or utcnow()
        created = int(at.replace(tzinfo=UTC).timestamp())
        payload: dict[str, Any] = {"subscription": {"entity": {"id": gateway_subscription_id, "entity": "subscription",
                                                               "status": event.split(".", 1)[1]}}}
        if payment is not None:
            payload["payment"] = {"entity": {"entity": "payment", "currency": "INR", "method": "upi",
                                             "created_at": created, **payment}}
        body = {"entity": "event", "account_id": "acc_fake", "event": event,
                "contains": list(payload), "payload": payload, "created_at": created}
        return self._next("evt"), json.dumps(body, separators=(",", ":")).encode()

    def debit(self, gateway_subscription_id: str, *, at: datetime, amount_paise: int | None = None,
              settle: bool = True, settle_days: int = 0) -> tuple[str, bytes, str]:
        """A successful debit: its event id, body and payment id, and (settle) its settlement settle_days later."""
        amount = amount_paise or self.subscriptions.get(gateway_subscription_id, {}).get("amount") or 0
        payment_id = self._next("pay")
        event_id, body = self.event_body(CHARGED_EVENT, gateway_subscription_id, at=at, payment={
            "id": payment_id, "amount": amount, "status": "captured"})
        if settle:
            self.settle(payment_id, amount, leads.ist_day(at) + timedelta(days=settle_days))
        return event_id, body, payment_id

    def settle(self, payment_id: str, amount_paise: int, day: date) -> None:
        fee = (amount_paise * 2 + 50) // 100  # 2% (plan section 11)
        self.settled.setdefault(day, []).append(Settlement(
            payment_id=payment_id, amount_paise=amount_paise, fee_paise=fee, tax_paise=(fee * 18 + 50) // 100,
            settlement_id=f"setl_fake{day:%Y%m%d}", settled_at=leads.ist_start(day)))

    def reset(self) -> None:
        self.subscriptions.clear()
        self.settled.clear()
        self.failures = 0


FAKE = FakeGateway()


def get_client(config: Settings = settings) -> GatewayClient | None:
    """The gateway client, or None while Autopay is off or not configured here (module docstring)."""
    if not config.autopay_configured:
        return None
    if config.autopay_provider == "razorpay" and config.environment != "test":
        return RazorpayClient(config)
    if config.environment in ("development", "test"):
        return FAKE
    return None


def sign(secret: str, body: bytes) -> str:
    """The X-Razorpay-Signature of a body: the HMAC-SHA256 of the raw body keyed with the webhook secret, in hex."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    """Compared in constant time; no secret or no header is invalid."""
    if not secret or not header:
        return False
    return hmac.compare_digest(sign(secret, body), header.strip().lower())


def body_event_id(body: bytes) -> str:
    """The event id when the gateway sends none: the body's hash, so a replayed body is still processed once."""
    return "body:" + hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------- mandates


def _live_mandate(db: Database, shop_id: int) -> Mandate | None:
    return mandates(db).find_one({"shop_id": shop_id, "status": {"$in": [s.value for s in LIVE]}})


def create_mandate(db: Database, shop: Shop, *, actor: Actor, now: datetime | None = None,
                   client: GatewayClient | None = None) -> Mandate:
    """Growth monthly by UPI Autopay (module docstring, step 1). A mandate still waiting for approval is returned as
    it is, with the same checkout link."""
    now = now or utcnow()
    client = client or get_client()
    if client is None:
        raise AutopayNotConfigured("UPI Autopay is not available yet")
    if now < plans.billing_start():
        raise billing.BillingRefused(f"Monthly plans start on {settings.billing_starts_on:%d %b %Y}; until then a "
                                     f"shop can pay a yearly advance")
    plan = plans.plan_in_force(db, PlanCode.GROWTH, now)
    if not plan.on_sale or plan.price_month_paise is None:
        raise billing.BillingRefused(f"{plan.name} is not sold by the month")
    existing = _live_mandate(db, shop.id)
    if existing is not None:
        if existing.status == MandateStatus.CREATED:
            return existing
        raise Conflict("UPI Autopay is already set up for this shop", code="autopay_exists")
    live = plans.subscriptions(db).find_one({"shop_ids": shop.id, "status": {"$in": [
        SubscriptionStatus.ACTIVE.value, SubscriptionStatus.ADVANCE.value]}})
    renews = None
    if live is not None:
        if live.status == SubscriptionStatus.ADVANCE or (live.plan_code, live.period) != (PlanCode.GROWTH,
                                                                                       Period.MONTH):
            raise billing.BillingRefused(f"This shop is on {live.plan_code.value} by the {live.period.value} until "
                                         f"{plans._date(live.ends_at)}; monthly Autopay can start when it ends")
        renews = live.id
    account = billing.account_for_owner(db, shop.owner_id, shop)
    mode = billing.gst_mode_at(now)
    gst = billing.gst_amount(plan.price_month_paise, mode)
    mandate_id = db.next_id(MANDATES)
    gs = client.create_subscription(amount_paise=plan.price_month_paise + gst,
                                    name=f"NearShop {plan.name} monthly", total_count=settings.autopay_total_count,
                                    notes={"shop_id": str(shop.id), "mandate_id": str(mandate_id)})
    mandate = Mandate(
        id=mandate_id, shop_id=shop.id, owner_id=shop.owner_id, billing_account_id=account.id,
        provider=settings.autopay_provider, gateway_subscription_id=gs.id, gateway_plan_id=gs.plan_id,
        checkout_url=gs.checkout_url, plan_code=plan.code, plan_version=plan.version,
        amount_paise=plan.price_month_paise, gst_mode=mode, total_paise=plan.price_month_paise + gst,
        subscription_id=renews, created_by=actor, created_at=now, updated_at=now)
    try:
        return mandates(db).insert(mandate)
    except DuplicateKeyError:
        _cancel_quietly(client, gs.id)
        raise Conflict("UPI Autopay was set up for this shop at the same moment; reload", code="autopay_exists") \
            from None


def _cancel_quietly(client: GatewayClient, gateway_subscription_id: str) -> None:
    try:
        client.cancel_subscription(gateway_subscription_id)
    except AppError:
        log.warning("could not cancel gateway subscription %s; cancel it in the gateway's dashboard",
                    gateway_subscription_id)


def _end_mandate(db: Database, mandate_id: int, *, by: str, reason: str, now: datetime) -> str:
    """Inside db's transaction: the mandate is cancelled, and its plan ends at the end of the paid month (no more
    renewals; it lapses at its end date), with an autopay_cancelled event and a notice to the owner."""
    m = mandates(db).get(mandate_id)
    if m is None or m.status == MandateStatus.CANCELLED:
        return "unchanged"
    mandates(db).set(m, status=MandateStatus.CANCELLED, cancelled_at=now, cancelled_by=by, checkout_url=None,
                     updated_at=now)
    sub = plans.subscriptions(db).get(m.subscription_id) if m.subscription_id else None
    if sub is not None and sub.status == SubscriptionStatus.ACTIVE:
        plans._change(db, sub.id, "autopay_cancelled", SYSTEM if by != "owner" else Actor(type="owner", id=m.owner_id),
                      reason, now, lambda _s: {})
        when = (f"Your plan runs until {plans._date(sub.ends_at)}, then your shop moves to the Free plan"
                if sub.ends_at > now else "Your shop is on the Free plan")
        body = (f"{when}, with every stock tool and the same place in search. Set up Autopay again, or pay for a year, "
                f"to keep the plan's extras.")
    else:
        body = "No more payments will be taken. Your shop keeps every stock tool and its place in search."
    notify(db, m.owner_id, KIND_AUTOPAY, "UPI Autopay is cancelled", body, PLAN_LINK)
    return "cancelled"


def cancel_mandate(db: Database, shop: Shop, *, actor: Actor, now: datetime | None = None,
                   client: GatewayClient | None = None) -> Mandate:
    """The owner cancels Autopay in NearShop: the gateway subscription first (nothing changes here if that fails),
    then the mandate; the plan ends at the end of the paid month."""
    now = now or utcnow()
    mandate = _live_mandate(db, shop.id)
    if mandate is None:
        raise NotFound("This shop has no UPI Autopay to cancel")
    client = client or get_client()
    if client is None:
        raise AutopayNotConfigured("UPI Autopay is not available right now; try again later")
    client.cancel_subscription(mandate.gateway_subscription_id)
    who = "owner" if actor.type == "owner" else actor.type
    db.transaction(lambda: _end_mandate(db, mandate.id, by=who, reason="UPI Autopay cancelled in NearShop", now=now))
    found = mandates(db).get(mandate.id)
    assert found is not None
    return found


# ---------------------------------------------------------------- webhook events


def _entity(payload: dict[str, Any], name: str) -> dict[str, Any]:
    part = (payload.get("payload") or {}).get(name) or {}
    entity = part.get("entity") if isinstance(part, dict) else None
    return entity if isinstance(entity, dict) else {}


def _authorised(db: Database, mandate_id: int, now: datetime) -> str:
    m = mandates(db).get(mandate_id)
    if m is None or m.status in (MandateStatus.AUTHORISED, MandateStatus.CANCELLED):
        return "unchanged"
    first = m.authorised_at is None
    mandates(db).set(m, status=MandateStatus.AUTHORISED, authorised_at=m.authorised_at or now, checkout_url=None,
                     updated_at=now)
    if first:
        notify(db, m.owner_id, KIND_AUTOPAY, "UPI Autopay is set up",
               f"₹{billing.rupees(m.total_paise)} will be debited each month for your NearShop plan. You get a notice "
               f"from your UPI app before each debit, and can cancel any time in NearShop or in your UPI app.",
               PLAN_LINK)
    return "authorised"


def _charged(db: Database, mandate_id: int, payment: dict[str, Any], now: datetime,
             credit_id: int | None = None) -> str:
    m = mandates(db).get(mandate_id)
    if m is None:
        raise NotFound("Mandate not found")
    paid_at = _from_unix(payment.get("created_at"), now)
    pay = billing.settle_autopay_debit(
        db, shop_id=m.shop_id, billing_account_id=m.billing_account_id, plan_code=m.plan_code,
        subscription_id=m.subscription_id, amount_paise=m.amount_paise, total_paise=m.total_paise,
        received_paise=int(payment.get("amount") or 0), gst_mode=m.gst_mode, payment_reference=str(payment["id"]),
        paid_at=paid_at, mandate_id=m.id, now=now, credit_id=credit_id)
    known = billing.credits(db).get(credit_id) if credit_id else None
    settled = SETTLED_UNRECORDED_ID.search(known.narration) if known is not None else None
    if settled:  # the reconciliation already saw this payment settled
        billing.payments(db).set(pay, settled_at=known.credited_at, settlement_id=settled.group(1))
    fields: dict[str, Any] = {"subscription_id": pay.subscription_id, "debits": m.debits + 1, "failed_debits": 0,
                              "last_debit_at": now, "checkout_url": None, "updated_at": now}
    if m.status in (MandateStatus.CREATED, MandateStatus.HALTED):  # a debit proves the mandate is in force
        fields.update(status=MandateStatus.AUTHORISED, authorised_at=m.authorised_at or now)
    mandates(db).set(m, **fields)
    return "paid"


def _failed(db: Database, mandate_id: int, now: datetime) -> str:
    m = mandates(db).get(mandate_id)
    if m is None or m.status == MandateStatus.CANCELLED:
        return "unchanged"
    mandates(db).set(m, failed_debits=m.failed_debits + 1, last_failure_at=now, updated_at=now)
    if m.failed_debits == 1:
        notify(db, m.owner_id, KIND_AUTOPAY, "Your UPI Autopay payment did not go through",
               f"The ₹{billing.rupees(m.total_paise)} debit for your NearShop plan failed. The payment gateway will try "
               f"again over the next few days: keep enough balance in the linked account.", PLAN_LINK)
    return "debit_failed"


def _halted(db: Database, mandate_id: int, now: datetime) -> str:
    """The gateway's last retry failed: the mandate is halted and the plan lapses to Free at once (time it already
    has beyond now, credited or granted, is kept: it lapses at its end date)."""
    m = mandates(db).get(mandate_id)
    if m is None or m.status in (MandateStatus.HALTED, MandateStatus.CANCELLED):
        return "unchanged"
    mandates(db).set(m, status=MandateStatus.HALTED, halted_at=now, updated_at=now)
    if m.subscription_id:
        def apply(sub: Subscription) -> dict[str, Any] | None:
            if sub.status != SubscriptionStatus.ACTIVE or sub.ends_at > now:
                return None
            return {"status": SubscriptionStatus.LAPSED}

        plans._change(db, m.subscription_id, "lapsed", SYSTEM, "UPI Autopay: the gateway's last retry failed", now,
                      apply)
    notify(db, m.owner_id, KIND_AUTOPAY, "Your NearShop plan has ended: UPI Autopay failed",
           "The monthly debit failed after the payment gateway's retries, so your shop is now on the Free plan, with "
           "every stock tool and the same place in search. To restart, set up UPI Autopay again from your Plan page "
           "(check the UPI account first), or pay for a year.", PLAN_LINK)
    return "halted"


def prepare(db: Database, now: datetime) -> None:
    billing.prepare_autopay(db, now)


def process_event(db: Database, event_id: str, payload: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Process one verified webhook event, once (module docstring, step 2). Returns its outcome. An error other than
    a refusal (AppError) propagates, so the webhook answers 500, nothing is recorded, and the gateway retries."""
    now = now or utcnow()
    kind = str(payload.get("event") or "")[:80]
    sub_entity, payment = _entity(payload, "subscription"), _entity(payload, "payment")
    gid = str(sub_entity.get("id") or payment.get("subscription_id") or "")[:80] or None
    payment_id = str(payment["id"])[:64] if payment.get("id") else None
    event_id = event_id[:120]
    if events(db).exists({"event_id": event_id}):
        return {"event_id": event_id, "outcome": "duplicate"}
    mandate = mandates(db).find_one({"gateway_subscription_id": gid}) if gid else None
    if kind == CHARGED_EVENT:
        prepare(db, now)

    def record(outcome: str) -> None:
        events(db).insert(GatewayEvent(event_id=event_id, event=kind, gateway_subscription_id=gid,
                                       payment_id=payment_id, outcome=outcome, received_at=now))

    def write() -> str:
        if events(db).exists({"event_id": event_id}):
            return "duplicate"
        if mandate is None:
            outcome = "unknown_subscription"
        elif kind in AUTHORISED_EVENTS:
            outcome = _authorised(db, mandate.id, now)
        elif kind == CHARGED_EVENT:
            if not payment_id:
                outcome = "no_payment"
            else:
                known = billing.credits(db).find_one({"upi_reference": payment_id})
                if known is None or (known.status == billing.CreditStatus.UNMATCHED
                                     and known.source == billing.CreditSource.AUTOPAY):
                    # New, or in the unmatched queue (the reconciliation saw it settled before this event came, or
                    # an earlier attempt could not apply it): applied now.
                    outcome = _charged(db, mandate.id, payment, now, known.id if known else None)
                else:
                    outcome = "payment_already_recorded"  # the same debit sent again under another event id
        elif kind == FAILED_EVENT:
            outcome = _failed(db, mandate.id, now)
        elif kind == HALTED_EVENT:
            outcome = _halted(db, mandate.id, now)
        elif kind in ENDED_EVENTS:
            outcome = _end_mandate(db, mandate.id, by="gateway", reason=f"UPI Autopay ended at the gateway ({kind})",
                                   now=now)
        else:
            outcome = "ignored"
        record(outcome)
        return outcome

    try:
        outcome = db.transaction(write)
    except DuplicateKeyError:
        if events(db).exists({"event_id": event_id}):
            return {"event_id": event_id, "outcome": "duplicate"}
        raise
    except AppError as exc:
        if kind != CHARGED_EVENT or not payment_id:
            raise
        # The money was taken but cannot be applied: it goes to the unmatched queue, where admins look.
        outcome = _unapplied(db, exc, event_id=event_id, gid=gid, payment_id=payment_id, payment=payment,
                             record=record, mandate=mandate, now=now)
    log.info("gateway event %s (%s): %s", event_id, kind, outcome)
    return {"event_id": event_id, "outcome": outcome}


def _unapplied(db: Database, exc: AppError, *, event_id: str, gid: str | None, payment_id: str,
               payment: dict[str, Any], record: Callable[[str], None], mandate: Mandate | None, now: datetime) -> str:
    """A debit the gateway took that cannot be applied: a credit in the unmatched queue, with the event, together."""
    def write() -> str:
        if events(db).exists({"event_id": event_id}):
            return "duplicate"
        billing.record_unmatched_autopay(
            db, reference=payment_id, amount_paise=int(payment.get("amount") or 0),
            at=_from_unix(payment.get("created_at"), now),
            narration=f"UPI Autopay debit, gateway subscription {gid}" + (f", mandate {mandate.id}" if mandate else ""),
            note=f"Autopay debit not applied: {exc.message}", now=now)
        record("unmatched")
        return "unmatched"

    log.warning("Autopay debit %s not applied: %s", payment_id, exc.message)
    try:
        return db.transaction(write)
    except DuplicateKeyError:
        return "duplicate"


# ---------------------------------------------------------------- the daily reconciliation


def reconcile(db: Database, now: datetime | None = None, client: GatewayClient | None = None) -> dict[str, int]:
    """Compare the gateway's settlement reports with the payment rows (module docstring, step 4). Every mismatch
    becomes, once, a credit in P9.5's unmatched queue. Returns the counts of this run."""
    now = now or utcnow()
    client = client or get_client()
    if client is None:
        return {"settlements": 0, "settled": 0, "mismatches": 0}
    today = leads.ist_day(now)
    seen = settled = mismatches = 0
    for back in range(settings.autopay_reconcile_days):
        day = today - timedelta(days=back + 1)
        for s in client.settlements(day):
            seen += 1
            pay = billing.payments(db).find_one({"upi_reference": s.payment_id})
            if pay is None or pay.match != billing.CreditSource.AUTOPAY:
                mismatches += _mismatch(db, s.payment_id, s.amount_paise, s.settled_at,
                                        SETTLED_UNRECORDED.format(settlement_id=s.settlement_id), now)
                continue
            if s.amount_paise != pay.amount_paise:
                mismatches += _mismatch(db, f"{s.payment_id}/amount", s.amount_paise, s.settled_at,
                                        f"settled ₹{billing.rupees(s.amount_paise)} ({s.settlement_id}), but payment "
                                        f"{pay.id} recorded ₹{billing.rupees(pay.amount_paise)}", now)
            if pay.settled_at is None:
                billing.payments(db).set(pay, settled_at=s.settled_at, settlement_id=s.settlement_id)
                settled += 1
    cutoff = now - timedelta(days=settings.autopay_settlement_days)
    for pay in billing.payments(db).find({"match": billing.CreditSource.AUTOPAY.value, "settled_at": None,
                                          "paid_at": {"$lte": cutoff}}):
        mismatches += _mismatch(db, f"{pay.upi_reference}/unsettled", pay.amount_paise, pay.paid_at,
                                f"payment {pay.id} ({pay.request_number}) is in no gateway settlement report "
                                f"{settings.autopay_settlement_days} days after it was paid", now)
    if mismatches:
        log.warning("Autopay reconciliation: %d mismatch(es) put in the unmatched queue", mismatches)
    return {"settlements": seen, "settled": settled, "mismatches": mismatches}


def _mismatch(db: Database, reference: str, amount_paise: int, at: datetime, what: str, now: datetime) -> int:
    credit = billing.record_unmatched_autopay(db, reference=reference, amount_paise=amount_paise, at=at,
                                              narration=f"Gateway reconciliation: {what}",
                                              note=f"Gateway reconciliation: {what}", now=now)
    return 1 if credit is not None else 0


# ---------------------------------------------------------------- views


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() + "Z" if at else None


def mandate_out(m: Mandate) -> dict[str, Any]:
    return {"id": m.id, "status": m.status.value, "plan_code": m.plan_code.value, "plan_version": m.plan_version,
            "amount_paise": m.amount_paise, "gst_paise": m.total_paise - m.amount_paise,
            "total_paise": m.total_paise, "checkout_url": m.checkout_url if m.status == MandateStatus.CREATED else None,
            "subscription_id": m.subscription_id, "debits": m.debits, "failed_debits": m.failed_debits,
            "authorised_at": _iso(m.authorised_at), "last_debit_at": _iso(m.last_debit_at),
            "halted_at": _iso(m.halted_at), "cancelled_at": _iso(m.cancelled_at), "cancelled_by": m.cancelled_by,
            "created_at": _iso(m.created_at)}


def owner_autopay(db: Database, shop: Shop, now: datetime | None = None) -> dict[str, Any]:
    """Whether Growth monthly by Autopay can be chosen now, its price, and the shop's latest mandate."""
    now = now or utcnow()
    try:
        growth = plans.plan_in_force(db, PlanCode.GROWTH, now)
    except NotFound:
        growth = None
    price = growth.price_month_paise if growth is not None and growth.on_sale else None
    latest = mandates(db).find_one({"shop_id": shop.id}, sort=[("_id", -1)])
    return {"available": settings.autopay_configured and now >= plans.billing_start() and price is not None,
            "price_month_paise": price, "mandate": mandate_out(latest) if latest else None}
