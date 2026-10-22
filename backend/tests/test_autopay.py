"""UPI Autopay for monthly plans (P9.6; app/services/gateway.py; plan section 8.18).

The tests use the fake gateway (gateway.FAKE: subscriptions, debits and settlement reports in memory, and the
gateway's webhook events built and signed with a test secret), their own shops and owners, and a controlled clock
(the now passed to the service) after billing starts on 1 Aug 2027. No test reaches a real gateway, and no key is a
real one. Everything they write is removed afterwards."""
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app import jobs
from app.core.config import Settings, settings
from app.core.database import Database, utcnow
from app.core.errors import AppError
from app.core.security import hash_password
from app.main import app
from app.models import Role, Shop, User
from app.models.billing import Actor, Period, SubscriptionStatus
from app.models.core import VerificationStatus, months_after
from app.services import billing, gateway, plans
from tests.conftest import CENTER, HEADERS, PASSWORD, login

IST = ZoneInfo("Asia/Kolkata")
PW_HASH = hash_password(PASSWORD)
OWNER = Actor(type="owner", id=0)
SECRET = "test-webhook-secret"  # a test value; never a real gateway's
MONTHLY = 14_900  # Growth by the month in catalogue version 1 (P9.4)
NOTICE_KINDS = [gateway.KIND_AUTOPAY, billing.KIND_PAYMENT_RECEIVED, billing.KIND_REFUNDED, billing.KIND_UNMATCHED,
                plans.KIND_ACTIVATED, plans.KIND_LAPSED, plans.KIND_REMINDER]
COLLECTIONS = (billing.ACCOUNTS, billing.REQUESTS, billing.CREDITS, billing.PAYMENTS, billing.DOCUMENTS,
               billing.STATEMENTS, gateway.MANDATES, gateway.EVENTS, plans.SUBSCRIPTIONS, plans.EVENTS)


def ist(*args: int) -> datetime:
    """An IST wall-clock time as the naive UTC time the database stores."""
    return datetime(*args, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


START = ist(2027, 8, 10, 10)  # after billing starts


def _clean(db: Database) -> None:
    for name in COLLECTIONS:
        db.mongo[name].delete_many({})
    db.mongo[plans.PLANS].delete_many({"version": {"$gt": 1}})  # catalogue versions a test published
    db.mongo["counters"].delete_many({"_id": {"$regex": f"^({billing.SERIES_PREFIX}|{plans.FOUNDING_COUNTER}$)"}})
    db.notifications.delete_many({"kind": {"$in": NOTICE_KINDS}})


@pytest.fixture(autouse=True)
def clean(ids, monkeypatch):
    monkeypatch.setattr(settings, "autopay_enabled", True)
    monkeypatch.setattr(settings, "autopay_provider", "fake")
    monkeypatch.setattr(settings, "autopay_webhook_secret", SecretStr(SECRET))
    gateway.FAKE.reset()
    db = Database()
    _clean(db)
    yield db
    _clean(db)
    gateway.FAKE.reset()


@pytest.fixture
def db(clean):
    return clean


class World:
    """Shops with their own owners, removed afterwards."""

    def __init__(self, db: Database):
        self.db = db
        self.tag = uuid.uuid4().hex[:8]
        self.users: list[int] = []
        self.shops: list[int] = []

    def shop(self, n: int) -> Shop:
        owner = self.db.users.insert(User(name=f"Autopay Owner {n}", email=f"autopay{n}-{self.tag}@example.com",
                                          password_hash=PW_HASH, role=Role.OWNER))
        self.users.append(owner.id)
        now = utcnow()
        shop = self.db.shops.insert(Shop(
            owner_id=owner.id, name=f"Autopay Shop {n} {self.tag}", slug=f"autopay-shop-{n}-{self.tag}",
            address_line=f"{n} Mandate Road", locality="Cowl Bazaar", city="Ballari", pincode="583101",
            lat=CENTER[0] + 0.004, lng=CENTER[1], category_ids=[], verification_status=VerificationStatus.VERIFIED,
            is_verified=True, kyc_type="udyam", kyc_number="UDYAM-KR-23-0000777", phone_verified_at=now,
            submitted_at=now, decided_at=now, verified_until=months_after(now)))
        self.shops.append(shop.id)
        return shop

    def email(self, shop: Shop) -> str:
        return self.db.users.get(shop.owner_id).email

    def cleanup(self) -> None:
        self.db.notifications.delete_many({"user_id": {"$in": self.users}})
        self.db.shops.delete_many({"_id": {"$in": self.shops}})
        self.db.users.delete_many({"_id": {"$in": self.users}})


@pytest.fixture
def world(db):
    w = World(db)
    yield w
    w.cleanup()


def deliver(db: Database, event: tuple, now: datetime) -> str:
    """Process one of the fake gateway's events, as the webhook would after checking its signature."""
    event_id, body = event[0], event[1]
    return gateway.process_event(db, event_id, json.loads(body), now)["outcome"]


def mandate_of(db: Database, m: gateway.Mandate) -> gateway.Mandate:
    return gateway.mandates(db).get(m.id)


def sub_of(db: Database, m: gateway.Mandate) -> plans.Subscription:
    return plans.subscriptions(db).get(mandate_of(db, m).subscription_id)


def authorised_with_first_debit(db: Database, shop: Shop, at: datetime = START) -> gateway.Mandate:
    """A mandate set up, approved and debited for its first month at `at`."""
    m = gateway.create_mandate(db, shop, actor=OWNER, now=at)
    assert deliver(db, gateway.FAKE.event_body("subscription.authenticated", m.gateway_subscription_id, at=at),
                   at) == "authorised"
    assert deliver(db, gateway.FAKE.debit(m.gateway_subscription_id, at=at), at + timedelta(minutes=1)) == "paid"
    return mandate_of(db, m)


def notices(db: Database, shop: Shop, kind: str = gateway.KIND_AUTOPAY) -> list[str]:
    return [n.title for n in db.notifications.find({"user_id": shop.owner_id, "kind": kind}, sort=[("_id", 1)])]


# ---------------------------------------------------------------- signatures and replays


def test_signatures_are_checked_and_replayed_events_are_ignored(db, world, monkeypatch):
    shop = world.shop(1)
    m = gateway.create_mandate(db, shop, actor=OWNER, now=START)
    monkeypatch.setattr(gateway, "utcnow", lambda: START + timedelta(minutes=1))
    event_id, body, payment_id = gateway.FAKE.debit(m.gateway_subscription_id, at=START)
    good = {"X-Razorpay-Signature": gateway.sign(SECRET, body), "X-Razorpay-Event-Id": event_id,
            "Content-Type": "application/json"}
    with TestClient(app) as c:
        url = "/api/webhooks/autopay"
        # No signature, a wrong one, or one made with another secret: refused, and nothing is read.
        for headers in ({"X-Razorpay-Event-Id": event_id},
                        {**good, "X-Razorpay-Signature": "0" * 64},
                        {**good, "X-Razorpay-Signature": gateway.sign("another-secret", body)}):
            res = c.post(url, content=body, headers=headers)
            assert res.status_code == 403 and res.json()["error"]["code"] == "bad_signature"
        # A body changed after signing is refused too.
        tampered = body.replace(str(MONTHLY).encode(), b"100")
        assert c.post(url, content=tampered, headers=good).status_code == 403
        assert billing.payments(db).count() == 0 and gateway.events(db).count() == 0

        res = c.post(url, content=body, headers=good)
        assert res.status_code == 200 and res.json()["outcome"] == "paid", res.text
        # The same event again (the gateway retries, or someone replays it): ignored.
        again = c.post(url, content=body, headers=good)
        assert again.status_code == 200 and again.json()["outcome"] == "duplicate"
        # The same debit under another event id: recorded once.
        other = c.post(url, content=body, headers={**good, "X-Razorpay-Event-Id": "evt_other"})
        assert other.json()["outcome"] == "payment_already_recorded"
        # Without an event id the body's hash is the id, so a replayed body is still processed once.
        failed_id, failed_body = gateway.FAKE.event_body("subscription.pending", m.gateway_subscription_id)
        no_id = {"X-Razorpay-Signature": gateway.sign(SECRET, failed_body), "Content-Type": "application/json"}
        assert c.post(url, content=failed_body, headers=no_id).json()["outcome"] == "debit_failed"
        assert c.post(url, content=failed_body, headers=no_id).json()["outcome"] == "duplicate"

        # With no webhook secret configured, every call is refused.
        monkeypatch.setattr(settings, "autopay_webhook_secret", SecretStr(""))
        assert c.post(url, content=body, headers=good).status_code == 403

    assert billing.payments(db).count() == 1
    assert billing.payments(db).find_one({}).upi_reference == payment_id
    assert mandate_of(db, m).debits == 1 and mandate_of(db, m).failed_debits == 1
    assert sub_of(db, m).ends_at == months_after(START + timedelta(minutes=1), 1)


def test_live_keys_are_refused_outside_production():
    with pytest.raises(ValidationError, match="live key"):
        Settings(_env_file=None, environment="test", autopay_key_id="rzp_live_notarealkey")
    Settings(_env_file=None, environment="test", autopay_key_id="rzp_test_notarealkey")
    with pytest.raises(ValidationError, match="development and test only"):
        Settings(_env_file=None, environment="staging", autopay_enabled=True, autopay_provider="fake",
                 cookie_secure=True)


# ---------------------------------------------------------------- mandates


def test_growth_monthly_mandate_needs_billing_to_have_started_and_no_other_plan(db, world, monkeypatch):
    shop, yearly = world.shop(1), world.shop(2)
    with pytest.raises(AppError, match="Monthly plans start on"):
        gateway.create_mandate(db, shop, actor=OWNER, now=ist(2027, 7, 1, 10))

    m = gateway.create_mandate(db, shop, actor=OWNER, now=START)
    assert (m.status, m.amount_paise, m.total_paise, m.subscription_id) == ("created", MONTHLY, MONTHLY, None)
    assert m.checkout_url and gateway.FAKE.subscriptions[m.gateway_subscription_id]["amount"] == MONTHLY
    # Asking again while it waits for approval returns the same mandate and checkout link.
    assert gateway.create_mandate(db, shop, actor=OWNER, now=START).id == m.id
    assert len(gateway.FAKE.subscriptions) == 1

    # A shop on Growth by the year cannot add a monthly mandate until that plan ends.
    plans.create_subscription(db, shop_ids=[yearly.id], plan_code="growth", period="year", source="shop_paid",
                              actor=Actor(type="admin", id=0), reason="Paid for a year", now=START)
    with pytest.raises(AppError, match="by the year"):
        gateway.create_mandate(db, yearly, actor=OWNER, now=START)

    # Off, or not configured: 503.
    monkeypatch.setattr(settings, "autopay_enabled", False)
    with pytest.raises(gateway.AutopayNotConfigured):
        gateway.create_mandate(db, yearly, actor=OWNER, now=START)


def test_the_owner_sets_up_and_cancels_autopay(db, world, monkeypatch):
    shop = world.shop(1)
    monkeypatch.setattr(gateway, "utcnow", lambda: START)
    monkeypatch.setattr(plans, "utcnow", lambda: START)
    with TestClient(app) as c:
        owner = login(c, world.email(shop))
        view = owner.get("/api/owner/billing", headers=HEADERS).json()["autopay"]
        assert view["mandate"] is None and view["price_month_paise"] == MONTHLY
        res = owner.post("/api/owner/billing/autopay", headers=HEADERS)
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "created" and res.json()["checkout_url"].startswith("https://")
        assert owner.get("/api/owner/billing", headers=HEADERS).json()["autopay"]["mandate"]["id"] == res.json()["id"]
        cancelled = owner.post("/api/owner/billing/autopay/cancel", headers=HEADERS)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cancelled_by"] == "owner"
        assert owner.post("/api/owner/billing/autopay/cancel", headers=HEADERS).status_code == 404
    gid = gateway.mandates(db).get(res.json()["id"]).gateway_subscription_id
    assert gateway.FAKE.subscriptions[gid]["status"] == "cancelled"


# ---------------------------------------------------------------- debits


def test_a_debit_writes_the_payment_the_extension_and_the_document_together(db, world):
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    sub = sub_of(db, m)
    first_end = months_after(START + timedelta(minutes=1), 1)
    assert (sub.status, sub.period, sub.source, sub.price_paise, sub.ends_at) == (
        "active", Period.MONTH, "shop_paid", MONTHLY, first_end)
    assert plans.effective_plan(db, shop.id, START + timedelta(days=1))["code"] == "growth"
    pay = billing.payments(db).find_one({"shop_id": shop.id})
    doc = billing.documents(db).get(pay.document_id)
    assert (pay.match, pay.amount_paise, pay.subscription_id) == ("autopay", MONTHLY, sub.id)
    assert doc.type == "receipt" and doc.total_paise == MONTHLY and "one month" in doc.description
    assert "UPI Autopay" in doc.description and billing.verify_document(doc)
    assert notices(db, shop) == ["UPI Autopay is set up"]

    # The next month's debit, around the end date: the plan is held, not lapsed, while it is due ...
    due = first_end + timedelta(hours=2)
    assert plans.lapse_due(db, due) == 0 and sub_of(db, m).status == "active"
    # ... and the debit renews it from the end date, without a break.
    assert deliver(db, gateway.FAKE.debit(m.gateway_subscription_id, at=due), due) == "paid"
    assert sub_of(db, m).ends_at == months_after(first_end, 1) and mandate_of(db, m).debits == 2

    # A failure while writing the document: nothing of the debit is kept, and the event is not marked processed, so
    # the gateway's retry applies it once.
    before = (billing.payments(db).count(), billing.documents(db).count(), billing.credits(db).count(),
              billing.requests(db).count(), sub_of(db, m).ends_at, plans.events(db).count())
    third = gateway.FAKE.debit(m.gateway_subscription_id, at=months_after(first_end, 1))
    original = billing._issue

    def broken(*args, **kw):
        raise RuntimeError("disk full")

    billing._issue = broken
    try:
        with pytest.raises(RuntimeError):
            deliver(db, third, months_after(first_end, 1) + timedelta(hours=1))
    finally:
        billing._issue = original
    assert (billing.payments(db).count(), billing.documents(db).count(), billing.credits(db).count(),
            billing.requests(db).count(), sub_of(db, m).ends_at, plans.events(db).count()) == before
    assert not gateway.events(db).exists({"event_id": third[0]}) and mandate_of(db, m).debits == 2
    assert deliver(db, third, months_after(first_end, 1) + timedelta(hours=2)) == "paid"
    assert sub_of(db, m).ends_at == months_after(first_end, 2) and billing.payments(db).count() == 3


def test_a_debit_that_cannot_be_applied_goes_to_the_unmatched_queue(db, world):
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    # The monthly price changes after the mandate was set up: the debit is the old price.
    plans.publish_catalogue(db, admin_id=0, reason="Monthly price change", changes={
        "growth": {"price_month_paise": 19_900}}, now=START + timedelta(days=2))
    at = sub_of(db, m).ends_at
    event = gateway.FAKE.debit(m.gateway_subscription_id, at=at)
    assert deliver(db, event, at + timedelta(hours=1)) == "unmatched"
    credit = billing.credits(db).find_one({"upi_reference": event[2]})
    assert credit.status == "unmatched" and credit.source == "autopay" and "price" in credit.note
    assert billing.payments(db).count() == 1 and gateway.events(db).find_one({"event_id": event[0]}).outcome == \
        "unmatched"
    assert deliver(db, event, at + timedelta(hours=2)) == "duplicate"
    # An event for a gateway subscription NearShop does not know is recorded and changes nothing.
    assert deliver(db, gateway.FAKE.event_body("subscription.activated", "sub_unknown"), at) == \
        "unknown_subscription"


def test_a_final_failed_debit_lapses_the_shop_to_free(db, world):
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    end = sub_of(db, m).ends_at
    # No "your plan ends" reminders under Autopay: the gateway sends the pre-debit notice.
    assert plans.send_reminders(db, end - timedelta(days=1)) == 0
    # The debit fails and the gateway retries: the owner is told once, and the plan is held meanwhile.
    gid = m.gateway_subscription_id
    assert deliver(db, gateway.FAKE.event_body("subscription.pending", gid), end + timedelta(hours=3)) == "debit_failed"
    assert deliver(db, gateway.FAKE.event_body("subscription.pending", gid), end + timedelta(days=1)) == "debit_failed"
    assert notices(db, shop)[-1] == "Your UPI Autopay payment did not go through"
    assert notices(db, shop).count("Your UPI Autopay payment did not go through") == 1
    assert plans.lapse_due(db, end + timedelta(days=1, hours=1)) == 0
    assert plans.effective_plan(db, shop.id, end + timedelta(days=1, hours=1))["status"] == "active"

    # The last retry fails: the mandate is halted and the shop is on Free at once.
    halted_at = end + timedelta(days=2)
    assert deliver(db, gateway.FAKE.event_body("subscription.halted", gid), halted_at) == "halted"
    assert mandate_of(db, m).status == "halted" and sub_of(db, m).status == SubscriptionStatus.LAPSED
    now_plan = plans.effective_plan(db, shop.id, halted_at)
    assert (now_plan["code"], now_plan["status"]) == ("free", "free")
    assert plans.events(db).find_one({"subscription_id": sub_of(db, m).id, "kind": "lapsed"}) is not None
    assert notices(db, shop)[-1] == "Your NearShop plan has ended: UPI Autopay failed"

    # Restarting: a new mandate, whose first debit starts a new month from then.
    again = gateway.create_mandate(db, shop, actor=OWNER, now=halted_at + timedelta(hours=1))
    assert again.id != m.id and again.subscription_id is None


def test_without_a_debit_the_hold_ends_after_the_grace_days(db, world):
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    end = sub_of(db, m).ends_at
    assert plans.lapse_due(db, end + timedelta(days=settings.autopay_grace_days) - timedelta(minutes=1)) == 0
    assert plans.lapse_due(db, end + timedelta(days=settings.autopay_grace_days)) == 1
    assert sub_of(db, m).status == "lapsed"


def test_a_cancellation_ends_the_plan_at_the_end_of_the_paid_month(db, world):
    in_app, in_upi_app = world.shop(1), world.shop(2)
    a = authorised_with_first_debit(db, in_app)
    b = authorised_with_first_debit(db, in_upi_app)
    end = sub_of(db, a).ends_at
    mid_month = START + timedelta(days=10)

    gateway.cancel_mandate(db, in_app, actor=Actor(type="owner", id=in_app.owner_id), now=mid_month)
    assert gateway.FAKE.subscriptions[a.gateway_subscription_id]["status"] == "cancelled"
    # The gateway's own cancellation event that follows changes nothing more.
    assert deliver(db, gateway.FAKE.event_body("subscription.cancelled", a.gateway_subscription_id),
                   mid_month + timedelta(minutes=1)) == "unchanged"
    # The owner cancels the mandate in their UPI app: the gateway tells NearShop.
    assert deliver(db, gateway.FAKE.event_body("subscription.cancelled", b.gateway_subscription_id),
                   mid_month) == "cancelled"
    assert mandate_of(db, b).cancelled_by == "gateway"

    for m, shop in ((a, in_app), (b, in_upi_app)):
        assert mandate_of(db, m).status == "cancelled"
        sub = sub_of(db, m)
        assert sub.status == "active" and sub.ends_at == end  # the paid month is kept
        assert plans.events(db).find_one({"subscription_id": sub.id, "kind": "autopay_cancelled"}) is not None
        assert plans.effective_plan(db, shop.id, end - timedelta(minutes=1))["code"] == "growth"
        assert notices(db, shop)[-1] == "UPI Autopay is cancelled"
    # No hold any more: both lapse at the end of the paid month.
    assert plans.lapse_due(db, end) == 2
    assert plans.effective_plan(db, in_app.id, end)["code"] == "free"


# ---------------------------------------------------------------- reconciliation


def test_the_reconciliation_finds_planted_mismatches(db, world):
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)  # settled the same day by the fake gateway
    day = START.date()
    clean = gateway.reconcile(db, ist(2027, 8, 11, 9))
    assert (clean["settlements"], clean["settled"], clean["mismatches"]) == (1, 1, 0)
    assert billing.payments(db).find_one({}).settlement_id

    # Planted: a payment the gateway settled that NearShop never recorded, an amount that differs, and a debit
    # recorded but missing from every settlement report.
    gateway.FAKE.settle("pay_planted_unknown", MONTHLY, day + timedelta(days=1))
    second_at = sub_of(db, m).ends_at
    event = gateway.FAKE.debit(m.gateway_subscription_id, at=second_at, settle=False)
    deliver(db, event, second_at + timedelta(minutes=5))
    gateway.FAKE.settle(event[2], MONTHLY - 100, billing.leads.ist_day(second_at) + timedelta(days=1))
    third_at = sub_of(db, m).ends_at
    unsettled = gateway.FAKE.debit(m.gateway_subscription_id, at=third_at, settle=False)
    deliver(db, unsettled, third_at + timedelta(minutes=5))

    found = gateway.reconcile(db, ist(2027, 8, 12, 9))
    assert found["mismatches"] == 1  # the unknown payment
    found = gateway.reconcile(db, second_at + timedelta(days=2))
    assert found["mismatches"] == 1  # the amount
    found = gateway.reconcile(db, third_at + timedelta(days=settings.autopay_settlement_days + 1))
    assert found["mismatches"] == 1  # never settled
    queue = billing.credits(db).find({"status": "unmatched"}, sort=[("_id", 1)])
    assert [(c.upi_reference, c.source) for c in queue] == [
        ("pay_planted_unknown", "autopay"), (f"{event[2]}/amount", "reconciliation"),
        (f"{unsettled[2]}/unsettled", "reconciliation")]
    assert all(c.note.startswith("Gateway reconciliation") for c in queue)
    # The event of a debit the reconciliation found first (a lost webhook, sent again): applied, and out of the queue.
    lost = gateway.FAKE.debit(m.gateway_subscription_id, at=sub_of(db, m).ends_at, settle_days=1)
    lost_day = billing.leads.ist_day(sub_of(db, m).ends_at)
    assert gateway.reconcile(db, ist(lost_day.year, lost_day.month, lost_day.day, 9) + timedelta(days=2))[
        "mismatches"] == 1
    assert billing.credits(db).find_one({"upi_reference": lost[2]}).status == "unmatched"
    assert deliver(db, lost, sub_of(db, m).ends_at + timedelta(days=2, hours=1)) == "paid"
    credit = billing.credits(db).find_one({"upi_reference": lost[2]})
    applied = billing.payments(db).get(credit.payment_id)
    assert credit.status == "matched" and applied.upi_reference == lost[2] and applied.settlement_id
    # settled_at is the day the gateway settled it (the day after it was paid), not the day it was paid.
    assert applied.settled_at == billing.leads.ist_start(lost_day + timedelta(days=1))
    assert applied.paid_at < applied.settled_at
    assert gateway.reconcile(db, sub_of(db, m).ends_at + timedelta(days=30))["mismatches"] == 0  # not "unsettled"
    # Each mismatch is reported once, however often the job runs.
    assert gateway.reconcile(db, ist(2027, 8, 12, 9))["mismatches"] == 0
    assert gateway.reconcile(db, third_at + timedelta(days=9))["mismatches"] == 0


def test_the_daily_job_reconciles_then_alerts(db, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(gateway, "reconcile", lambda _db, now: seen.append("reconcile") or {"mismatches": 0})
    monkeypatch.setattr(billing, "alert_unmatched", lambda _db, now: seen.append("alert") or {"alerted": 0})
    db.mongo[jobs.LEASES].delete_many({"_id": jobs.BILLING_JOB})
    result = jobs.billing_reconciliation(lambda: ist(2027, 9, 2, 9))
    assert seen == ["reconcile", "alert"] and result["autopay"] == {"mismatches": 0}

    def unreachable(_db, now):
        raise gateway.GatewayError("The payment gateway could not be reached")

    monkeypatch.setattr(gateway, "reconcile", unreachable)
    assert jobs.billing_reconciliation(lambda: ist(2027, 9, 3, 9))["autopay"] == {"error": "gateway_error"}
    assert seen[-1] == "alert"


def test_ten_mandates_with_three_debits_each_reconcile_with_no_mismatch(db, world):
    """The staging acceptance run, with the fake gateway: 10 mandates, 3 debits each, including 2 failed debits
    (retried and then paid) and 1 cancellation, reconcile with 0 mismatches."""
    shops = [world.shop(n) for n in range(10)]
    mandates = [authorised_with_first_debit(db, s) for s in shops]
    total = {"settled": 0, "mismatches": 0}

    def reconcile(at: datetime) -> None:
        result = gateway.reconcile(db, at)
        total["settled"] += result["settled"]
        total["mismatches"] += result["mismatches"]

    reconcile(START + timedelta(days=1))
    for month in (1, 2):
        due = max(sub_of(db, m).ends_at for m in mandates)
        for i, m in enumerate(mandates):
            at = sub_of(db, m).ends_at
            if (i, month) in ((0, 1), (1, 2)):  # a failed debit, retried by the gateway the next day
                assert deliver(db, gateway.FAKE.event_body("subscription.pending", m.gateway_subscription_id),
                               at + timedelta(hours=1)) == "debit_failed"
                at += timedelta(days=1)
            assert deliver(db, gateway.FAKE.debit(m.gateway_subscription_id, at=at), at + timedelta(minutes=5)) \
                == "paid"
        reconcile(due + timedelta(days=2, hours=12))
    gateway.cancel_mandate(db, shops[9], actor=Actor(type="owner", id=shops[9].owner_id),
                           now=sub_of(db, mandates[9]).ends_at - timedelta(days=5))

    assert billing.payments(db).count({"match": "autopay"}) == 30
    assert billing.documents(db).count() == 30 and all(mandate_of(db, m).debits == 3 for m in mandates)
    assert total == {"settled": 30, "mismatches": 0}
    assert gateway.reconcile(db, max(sub_of(db, m).ends_at for m in mandates) + timedelta(days=10))["mismatches"] == 0
    assert billing.credits(db).count({"status": "unmatched"}) == 0
    assert mandate_of(db, mandates[9]).status == "cancelled"
    assert sum(mandate_of(db, m).status == "authorised" for m in mandates) == 9
    # The fee per debit at the gateway's 2% plus GST on the fee (plan section 11): about ₹3.52 on ₹149.
    settled = [s for day in gateway.FAKE.settled.values() for s in day]
    assert {s.fee_paise + s.tax_paise for s in settled} == {298 + 54}
    assert date(2027, 8, 10) in gateway.FAKE.settled


# ---------------------------------------------------------------- review fixes


def test_reconciliation_mismatches_can_only_be_closed(db, world):
    """A mismatch about money already recorded can never become a second payment, nor be "returned"."""
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    assert gateway.reconcile(db, START + timedelta(days=1))["mismatches"] == 0  # the first debit settles cleanly
    at = sub_of(db, m).ends_at
    event = gateway.FAKE.debit(m.gateway_subscription_id, at=at, settle=False)
    assert deliver(db, event, at + timedelta(minutes=5)) == "paid"
    gateway.FAKE.settle(event[2], MONTHLY - 100, billing.leads.ist_day(at) + timedelta(days=1))
    assert gateway.reconcile(db, at + timedelta(days=2))["mismatches"] == 1
    credit = billing.credits(db).find_one({"upi_reference": f"{event[2]}/amount"})
    assert credit.source == "reconciliation" and credit.status == "unmatched"

    # Matching it by hand is refused before any request is looked at, so no second payment or document is made.
    admin = Actor(type="admin", id=0)
    with pytest.raises(AppError, match="reconciliation mismatch"):
        billing.match_by_hand(db, upi_reference=credit.upi_reference, request_number="NSP000001",
                              reason="Matching a reconciliation mismatch", actor=admin, now=at + timedelta(days=3))
    with pytest.raises(AppError, match="close it instead"):
        billing.return_credit(db, credit.id, reason="Returning a mismatch", bank_reference="UTR0001", actor=admin)
    assert billing.payments(db).count() == 2 and billing.documents(db).count() == 2

    # Money received (source autopay) cannot be closed instead of matched or returned.
    gateway.FAKE.settle("pay_unknown_money", MONTHLY, billing.leads.ist_day(at) + timedelta(days=1))
    gateway.reconcile(db, at + timedelta(days=2, hours=1))
    money = billing.credits(db).find_one({"upi_reference": "pay_unknown_money"})
    assert money.source == "autopay"
    with pytest.raises(AppError, match="money received"):
        billing.close_credit(db, money.id, reason="Closing money received", reference="setl_x", actor=admin)

    closed = billing.close_credit(db, credit.id, reason="Gateway confirmed the settled amount", reference="setl_ticket1",
                                  actor=admin, now=at + timedelta(days=4))
    assert closed.status == "closed" and closed.closed.reference == "setl_ticket1" and closed.returned is None
    assert billing.credit_out(closed)["closed"]["reason"] == "Gateway confirmed the settled amount"
    with pytest.raises(AppError, match="closed"):
        billing.close_credit(db, credit.id, reason="Closing it a second time", reference="setl_x", actor=admin)


def test_a_halt_cancels_the_gateway_subscription_and_a_later_debit_is_queued(db, world):
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    gid = m.gateway_subscription_id
    end = sub_of(db, m).ends_at
    halted_at = end + timedelta(days=2)
    assert deliver(db, gateway.FAKE.event_body("subscription.halted", gid), halted_at) == "halted"
    assert gateway.FAKE.subscriptions[gid]["status"] == "cancelled"  # no longer registered in the UPI app

    # A late authorisation event does not bring the halted mandate (and its plan hold) back.
    assert deliver(db, gateway.FAKE.event_body("subscription.activated", gid), halted_at + timedelta(minutes=1)) == \
        "unchanged"
    assert mandate_of(db, m).status == "halted"

    # The owner sets up Autopay again; then a debit on the old, halted subscription arrives.
    again = gateway.create_mandate(db, shop, actor=OWNER, now=halted_at + timedelta(hours=1))
    assert deliver(db, gateway.FAKE.event_body("subscription.authenticated", again.gateway_subscription_id),
                   halted_at + timedelta(hours=2)) == "authorised"
    late = gateway.FAKE.debit(gid, at=halted_at + timedelta(hours=3), amount_paise=MONTHLY)
    assert deliver(db, late, halted_at + timedelta(hours=3)) == "unmatched"  # no DuplicateKeyError, no 500 loop
    assert deliver(db, late, halted_at + timedelta(hours=4)) == "duplicate"
    credit = billing.credits(db).find_one({"upi_reference": late[2]})
    assert credit.source == "autopay" and "halted" in credit.note
    assert mandate_of(db, m).status == "halted" and mandate_of(db, again).status == "authorised"
    assert billing.payments(db).count() == 1
    # A late authorisation for the old one still changes nothing while the new one is live.
    assert deliver(db, gateway.FAKE.event_body("subscription.authenticated", gid), halted_at + timedelta(hours=5)) == \
        "unchanged"

    # The gateway's cancellation event that follows the halt ends the mandate without another notice.
    told = len(notices(db, shop))
    assert deliver(db, gateway.FAKE.event_body("subscription.cancelled", gid), halted_at + timedelta(hours=6)) == \
        "cancelled"
    assert mandate_of(db, m).status == "cancelled" and len(notices(db, shop)) == told


def test_a_halt_whose_gateway_cancel_fails_is_still_recorded(db, world):
    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    gateway.FAKE.failures = 1
    halted_at = sub_of(db, m).ends_at + timedelta(days=2)
    assert deliver(db, gateway.FAKE.event_body("subscription.halted", m.gateway_subscription_id), halted_at) == \
        "halted"
    assert mandate_of(db, m).status == "halted"
    assert gateway.FAKE.subscriptions[m.gateway_subscription_id]["status"] != "cancelled"


def test_a_debit_for_an_unknown_subscription_is_queued_at_once(db, world):
    at = START + timedelta(days=1)
    event = gateway.FAKE.debit("sub_unknown_to_nearshop", at=at, amount_paise=MONTHLY, settle=False)
    assert deliver(db, event, at) == "unmatched"
    credit = billing.credits(db).find_one({"upi_reference": event[2]})
    assert credit.status == "unmatched" and credit.source == "autopay"
    assert "No Autopay mandate" in credit.note and credit.amount_paise == MONTHLY
    assert gateway.events(db).find_one({"event_id": event[0]}).outcome == "unmatched"
    # The reconciliation that later sees it settled adds nothing more.
    gateway.FAKE.settle(event[2], MONTHLY, billing.leads.ist_day(at) + timedelta(days=1))
    assert gateway.reconcile(db, at + timedelta(days=2))["mismatches"] == 0
    assert billing.credits(db).count({"status": "unmatched"}) == 1


def test_deleting_an_owner_cancels_their_mandates_first(db, world, monkeypatch):
    from app.services import privacy

    shop, waiting = world.shop(1), world.shop(2)
    m = authorised_with_first_debit(db, shop)
    pending = gateway.create_mandate(db, waiting, actor=OWNER, now=START)
    owner = db.users.get(shop.owner_id)

    # The gateway cannot be reached: nothing is deleted, and the mandate stays.
    gateway.FAKE.failures = 1
    with pytest.raises(gateway.GatewayError):
        privacy.request_deletion(db, owner, cancel_open=False, now=START + timedelta(days=3))
    assert db.users.get(owner.id).deleted_at is None and mandate_of(db, m).status == "authorised"
    # Autopay switched off with a mandate still registered: the deletion waits too.
    monkeypatch.setattr(settings, "autopay_enabled", False)
    with pytest.raises(gateway.AutopayNotConfigured):
        privacy.request_deletion(db, owner, cancel_open=False, now=START + timedelta(days=3))
    monkeypatch.setattr(settings, "autopay_enabled", True)

    assert privacy.request_deletion(db, owner, cancel_open=False, now=START + timedelta(days=3)) == \
        {"status": "deleted"}
    assert gateway.FAKE.subscriptions[m.gateway_subscription_id]["status"] == "cancelled"
    ended = mandate_of(db, m)
    assert ended.status == "cancelled" and ended.cancelled_by == "deletion"
    assert db.users.get(owner.id).deleted_at is not None
    # Another owner's mandate is untouched; an owner without one is deleted without the gateway.
    assert mandate_of(db, pending).status == "created"
    gateway.FAKE.failures = 5
    plain = world.shop(3)
    assert privacy.request_deletion(db, db.users.get(plain.owner_id), cancel_open=False) == {"status": "deleted"}
    gateway.FAKE.failures = 0


def test_a_scheduled_deletion_waits_while_the_gateway_cannot_cancel(db, world):
    from app.services import privacy

    shop = world.shop(1)
    m = authorised_with_first_debit(db, shop)
    now = START + timedelta(days=3)
    db.users.update_one({"_id": shop.owner_id}, {"$set": {"deletion_requested_at": now, "deletion_due_at": now}})
    gateway.FAKE.failures = 1
    assert privacy.process_scheduled_deletions(db, now)["deleted"] == 0
    assert db.users.get(shop.owner_id).deleted_at is None and mandate_of(db, m).status == "authorised"
    assert privacy.process_scheduled_deletions(db, now + timedelta(minutes=1))["deleted"] == 1
    assert mandate_of(db, m).status == "cancelled"


class _Res:
    def __init__(self, status: int, body: bytes):
        self.status_code, self._body = status, body

    def json(self):
        return json.loads(self._body)


@pytest.mark.parametrize("body", [b"<html>bad gateway</html>", b"[]", b'{"entity": "plan"}'])
def test_unreadable_gateway_answers_become_gateway_errors(monkeypatch, body):
    monkeypatch.setattr(gateway.httpx, "request", lambda *a, **kw: _Res(200, body))
    client = gateway.RazorpayClient(Settings(_env_file=None, environment="test", autopay_key_id="rzp_test_x",
                                             autopay_key_secret="not-a-real-secret"))
    with pytest.raises(gateway.GatewayError):
        client.create_subscription(amount_paise=MONTHLY, name="NearShop Growth monthly", total_count=60, notes={})
    if body.startswith(b"{"):
        return
    with pytest.raises(gateway.GatewayError):
        client.settlements(date(2027, 8, 10))


def test_a_settlement_row_that_cannot_be_read_is_a_gateway_error(monkeypatch):
    page = json.dumps({"items": [{"type": "payment", "entity_id": "pay_x", "amount": "not a number"}]}).encode()
    monkeypatch.setattr(gateway.httpx, "request", lambda *a, **kw: _Res(200, page))
    client = gateway.RazorpayClient(Settings(_env_file=None, environment="test", autopay_key_id="rzp_test_x",
                                             autopay_key_secret="not-a-real-secret"))
    with pytest.raises(gateway.GatewayError):
        client.settlements(date(2027, 8, 10))


def test_the_daily_job_still_alerts_after_an_unexpected_reconciliation_error(db, monkeypatch):
    seen: list[str] = []

    def broken(_db, now):
        raise KeyError("id")

    monkeypatch.setattr(gateway, "reconcile", broken)
    monkeypatch.setattr(billing, "alert_unmatched", lambda _db, now: seen.append("alert") or {"alerted": 0})
    db.mongo[jobs.LEASES].delete_many({"_id": jobs.BILLING_JOB})
    result = jobs.billing_reconciliation(lambda: ist(2027, 9, 4, 9))
    assert seen == ["alert"] and result["autopay"] == {"error": "unexpected"}
