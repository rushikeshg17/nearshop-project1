"""Monthly statements: the shadow bill, the ROI statement and the shops' replies (P9.9; app/services/statements.py;
plan section 8.18).

The tests use their own owners and shops, live until 2040, and March 2032 (IST), built on 1 April 2032 at 10:30 IST,
so no other test's data is in them; everything they write is removed afterwards. Every WhatsApp message goes through
the fake client; nothing reaches Meta."""
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from string import Formatter
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app import jobs
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.main import app
from app.models import Product, Reservation, Role, Shop, User
from app.models.billing import Actor
from app.models.core import VerificationStatus
from app.models.enums import ReservationStatus
from app.services import billing, demand_report, leads, plans, statements, whatsapp
from app.services import notification_text as words
from app.services.privacy import set_consent
from tests.conftest import CENTER, PASSWORD, login

IST = ZoneInfo("Asia/Kolkata")
KANNADA = re.compile(r"[ಀ-೿]")
YEAR, MONTH = 2032, 3
KEY = "2032-03"


def ist(*args: int) -> datetime:
    """An IST wall-clock time as the naive UTC time the database stores."""
    return datetime(*args, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


BUILD_AT = ist(2032, 4, 1, 10, 30)
LIVE_UNTIL = datetime(2040, 1, 1)


def clear(db: Database) -> None:
    for name in (statements.STATEMENTS, statements.REPLIES, whatsapp.OUTBOX, whatsapp.SESSIONS, whatsapp.INBOUND,
                 whatsapp.PENDING):
        db.mongo[name].delete_many({})
    whatsapp.FAKE.reset()


class Town:
    def __init__(self, db: Database):
        self.db = db
        self.tag = secrets.token_hex(3)
        self.users: list[int] = []
        self.shops: list[int] = []
        self.reservations: list[int] = []
        self.phone_n = 0

    def owner(self, language: str = "en", phone: bool = True, email: str | None = None) -> User:
        self.phone_n += 1
        number = f"90008{int(self.tag, 16) % 1000:03d}{self.phone_n:02d}" if phone else None
        u = self.db.users.insert(User(
            name=f"Statement Owner {self.phone_n}", email=email or f"st-{self.tag}-{self.phone_n}@example.com",
            password_hash=hash_password(PASSWORD), role=Role.OWNER, phone=number, language=language,
            phone_verified_at=utcnow() - timedelta(days=30) if phone else None))
        self.users.append(u.id)
        if phone:
            set_consent(self.db, u.id, "whatsapp", True)
        return u

    def shop(self, owner: User, name: str, city: str = "Mysuru", fresh: bool = True) -> Shop:
        decided = utcnow()
        shop = self.db.shops.insert(Shop(
            owner_id=owner.id, name=name, slug=f"st-{self.tag}-{len(self.shops)}", address_line="5 Statement Road",
            city=city, lat=CENTER[0] + 1.5, lng=CENTER[1], category_ids=[],
            verification_status=VerificationStatus.VERIFIED, is_verified=True, kyc_type="udyam",
            kyc_number=f"UDYAM-KR-23-{8000000 + len(self.shops):07d}", phone_verified_at=decided,
            submitted_at=decided, decided_at=decided, verified_until=LIVE_UNTIL))
        self.shops.append(shop.id)
        category = self.db.categories.find_one()
        updated = BUILD_AT - timedelta(days=2 if fresh else 20)
        self.db.products.insert(Product(shop_id=shop.id, category_id=category.id, name=f"Statement item {name}",
                                        keywords="item", price=100, quantity=5, stock_updated_at=updated,
                                        created_at=updated, updated_at=updated))
        return shop

    def reserve(self, shop: Shop, customer: User, created: datetime, status=ReservationStatus.REQUESTED,
                completed: datetime | None = None) -> None:
        r = self.db.reservations.insert(Reservation(
            code=f"S{secrets.token_hex(4).upper()}", customer_id=customer.id, shop_id=shop.id, product_id=10**7 + len(self.reservations),
            quantity=1, unit_price=100.0, status=status, expires_at=created + timedelta(minutes=30),
            confirmed_at=completed, completed_at=completed, created_at=created, updated_at=created))
        self.reservations.append(r.id)

    def cleanup(self) -> None:
        db = self.db
        db.reservations.delete_many({"_id": {"$in": self.reservations}})
        db.products.delete_many({"shop_id": {"$in": self.shops}})
        db.mongo[leads.EVENTS].delete_many({"shop_id": {"$in": self.shops}})
        db.mongo[leads.COUNTS].delete_many({"shop_id": {"$in": self.shops}})
        db.mongo[plans.SUBSCRIPTIONS].delete_many({"shop_ids": {"$in": self.shops}})
        db.mongo[plans.EVENTS].delete_many({"shop_ids": {"$in": self.shops}})
        db.mongo[billing.REQUESTS].delete_many({"shop_id": {"$in": self.shops}})
        db.mongo[billing.ACCOUNTS].delete_many({"contact_user_id": {"$in": self.users}})
        db.notifications.delete_many({"user_id": {"$in": self.users}})
        db.mongo["consents"].delete_many({"user_id": {"$in": self.users}})
        db.shops.delete_many({"_id": {"$in": self.shops}})
        db.users.delete_many({"_id": {"$in": self.users}})


@pytest.fixture
def town(ids, monkeypatch):
    monkeypatch.setattr(statements, "FIRST_MONTH", (2000, 1))
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    # Keep the P9.7 report out unless a test puts items in it.
    monkeypatch.setattr(demand_report, "owner_module", lambda db, shop, now=None: {"items": []})
    db = Database()
    clear(db)
    t = Town(db)
    yield t
    t.cleanup()
    clear(db)


def mine(db: Database, town: Town, **filter) -> list[dict]:
    return list(db.mongo[statements.STATEMENTS].find({"shop_id": {"$in": town.shops}, **filter}, sort=[("_id", 1)]))


def statement_of(db: Database, shop: Shop) -> dict:
    return db.mongo[statements.STATEMENTS].find_one({"shop_id": shop.id, "month": KEY})


def message(db: Database, phone: str, at: datetime, **content) -> dict:
    """One message from this 10-digit phone, received at `at`, through the webhook's handler; the bot's answer."""
    whatsapp.FAKE.reset()
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "91" + phone, "id": f"wamid.in.{uuid.uuid4().hex}",
        "timestamp": str(int(at.replace(tzinfo=UTC).timestamp())), **content}]}}]}]}
    assert whatsapp.handle_messages(db, payload, client=whatsapp.FAKE, now=at)["handled"] == 1
    [reply] = list(whatsapp.FAKE.sent)
    return reply


def button(db: Database, phone: str, at: datetime, payload: str) -> str:
    return message(db, phone, at, type="button", button={"payload": payload, "text": "x"})["text"]


def text(db: Database, phone: str, at: datetime, body: str) -> str:
    return message(db, phone, at, type="text", text={"body": body})["text"]


# ---------------------------------------------------------------- building


def test_the_counts_in_a_statement_equal_leads_counts(town):
    db = town.db
    owner = town.owner()
    shop = town.shop(owner, "Counted Shop")
    customer = town.owner(phone=False)  # any account other than the shop's own
    town.reserve(shop, customer, ist(2032, 3, 2, 10), ReservationStatus.COMPLETED, completed=ist(2032, 3, 2, 12))
    town.reserve(shop, customer, ist(2032, 3, 20, 10))
    town.reserve(shop, customer, ist(2032, 2, 28, 10))  # the month before: not counted
    for when, kind, ip in ((ist(2032, 3, 5, 10), "call_tap", "203.0.113.1"),
                           (ist(2032, 3, 6, 10), "call_tap", "203.0.113.1"),
                           (ist(2032, 3, 7, 10), "directions_tap", "203.0.113.2")):
        assert leads.record_tap(db, shop.id, kind, None, ip, "ua", now=when)

    result = statements.build_month(db, YEAR, MONTH, BUILD_AT)
    assert result["enabled"] and result["built"] >= 1
    st = statement_of(db, shop)
    expected = leads.month_counts(db, shop.id, YEAR, MONTH, BUILD_AT)
    assert st["counts"] == {f: expected[f] for f in leads.COUNT_FIELDS}
    assert (st["counts"]["reservation_requests"], st["counts"]["pickups_completed"], st["counts"]["call_taps"],
            st["counts"]["directions_taps"], st["counts"]["delivery_orders"]) == (2, 1, 2, 1, 0)
    words_en = statements.parts(st, shop.name, "en")["counts"]
    assert words_en == ("2 reservation requests (1 collected), 0 delivery orders, 2 call taps and 1 directions taps.")

    # A stored lead_counts row (the job at 02:00) is what the statement states.
    leads.rollup_month(db, YEAR, MONTH, now=ist(2032, 4, 1, 2))
    db.mongo[leads.COUNTS].update_one({"shop_id": shop.id, "month": KEY}, {"$set": {"call_taps": 9}})
    db.mongo[statements.STATEMENTS].delete_many({"shop_id": shop.id})
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    assert statement_of(db, shop)["counts"]["call_taps"] == 9 == leads.month_counts(
        db, shop.id, YEAR, MONTH, BUILD_AT)["call_taps"]


def test_only_active_shops_get_a_statement_and_never_from_demo_data(town, monkeypatch):
    db = town.db
    active = town.shop(town.owner(), "Active Shop")
    stale = town.shop(town.owner(), "Stale Shop", fresh=False)  # no listing changed in 14 days
    suspended = town.shop(town.owner(), "Suspended Shop")
    db.shops.update_one({"_id": suspended.id}, {"$set": {"verification_status": VerificationStatus.SUSPENDED.value}})
    seeded = town.shop(town.owner(email=f"st-{town.tag}@shops.nearshop.demo"), "Seeded Shop")
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    assert {s["shop_id"] for s in mine(db, town)} == {active.id}
    assert not {stale.id, suspended.id, seeded.id} & {s["shop_id"] for s in mine(db, town)}

    db.mongo[statements.STATEMENTS].delete_many({})
    monkeypatch.setattr(settings, "demo_mode", True)
    assert statements.build_month(db, YEAR, MONTH, BUILD_AT) == {"month": KEY, "enabled": False,
                                                                  "reason": "demo_data"}
    assert mine(db, town) == []
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(statements, "FIRST_MONTH", (2032, 4))
    assert statements.build_month(db, YEAR, MONTH, BUILD_AT)["reason"] == "before_first_month"
    assert mine(db, town) == []


def test_one_statement_per_shop_per_month_even_on_a_rerun(town):
    db = town.db
    owner = town.owner()
    shop = town.shop(owner, "Rerun Shop")
    first = statements.build_month(db, YEAR, MONTH, BUILD_AT)
    again = statements.build_month(db, YEAR, MONTH, BUILD_AT + timedelta(hours=1))
    assert first["built"] >= 1 and again["built"] == 0 and again["existing"] == first["built"]
    assert len(mine(db, town, shop_id=shop.id)) == 1
    assert len(db.notifications.find({"user_id": owner.id, "kind": "statement_free"})) == 1
    assert db.mongo[whatsapp.OUTBOX].count_documents({"user_id": owner.id}) == 1
    # A concurrent run that read the list before this one wrote: the unique index refuses the second statement,
    # and its notification and outbox row roll back with it.
    st = statement_of(db, shop)
    with pytest.raises(DuplicateKeyError):
        db.transaction(lambda: statements._write(db, shop, {k: v for k, v in st.items() if k not in (
            "_id", "language", "notification_id", "outbox_id", "route", "delivery", "built_at", "keep_until")},
            db.next_id(statements.STATEMENTS), BUILD_AT))
    assert len(db.notifications.find({"user_id": owner.id, "kind": "statement_free"})) == 1
    assert db.mongo[whatsapp.OUTBOX].count_documents({"user_id": owner.id}) == 1


def test_the_job_states_the_previous_month_from_the_first_at_1000_ist(town, monkeypatch):
    db = town.db
    shop = town.shop(town.owner(), "Job Shop")
    assert statements.statement_month(ist(2032, 4, 1, 9, 59)) == (2032, 2)
    assert statements.statement_month(ist(2032, 4, 1, 10, 0)) == (2032, 3)
    assert statements.statement_month(ist(2032, 1, 5)) == (2031, 12)
    db.mongo[jobs.LEASES].delete_many({"_id": jobs.STATEMENTS_JOB})
    first = jobs.statements(lambda: BUILD_AT)
    assert first["ran_for"] == KEY and first["built"] >= 1 and statement_of(db, shop) is not None
    db.mongo[jobs.LEASES].update_one({"_id": jobs.STATEMENTS_JOB}, {"$set": {
        "last_result": {**first, "ok": True}, "holder": "test", "expires_at": utcnow()}}, upsert=True)
    called = []
    monkeypatch.setattr(statements, "build_month", lambda *a, **k: called.append(a) or {})
    again = jobs.statements(lambda: BUILD_AT + timedelta(hours=2))
    assert called == [] and again["ran_for"] == KEY and again["built"] == first["built"]
    assert jobs.JOBS[jobs.STATEMENTS_JOB] is jobs.statements
    assert jobs.RETRY_AFTER_FAILURE[jobs.STATEMENTS_JOB] == timedelta(minutes=15)


# ---------------------------------------------------------------- shadow bill and ROI statement


def test_a_free_shop_before_billing_gets_the_shadow_bill_with_the_founding_price(town, monkeypatch):
    db = town.db
    monkeypatch.setattr(settings, "billing_starts_on", datetime(2033, 1, 1).date())
    founding = town.shop(town.owner(), "Founding Shop", city="Ballari")
    standard = town.shop(town.owner(), "Standard Shop", city="Mysuru")
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = statement_of(db, founding)
    assert st["kind"] == "shadow_bill" and st["plan_code"] == "free" and st["price"]["founding"] is True
    assert st["pay_link"].endswith(billing.BILLING_LINK)
    p = statements.parts(st, founding.name, "en")
    assert p["amount_due"] == "Amount due: ₹0. You are on the Free plan, and every stock tool stays free."
    assert p["price"] == "Growth would cost ₹83 a month at the founding price (₹999 for the first year)."
    other = statements.parts(statement_of(db, standard), standard.name, "en")
    assert other["price"] == "Growth would cost ₹125 a month at the yearly price (₹1,499 a year)."

    # A shop that paid in advance is told when its plan starts, with no link to pay again.
    advance = town.shop(town.owner(), "Advance Shop")
    plans.create_subscription(db, shop_ids=[advance.id], plan_code="growth", period="year", source="advance",
                              actor=Actor(type="admin", id=1), reason="Advance paid for the test",
                              now=BUILD_AT - timedelta(days=3))
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = statement_of(db, advance)
    assert st["kind"] == "shadow_bill" and st["pay_link"] is None
    assert statements.parts(st, advance.name, "en")["price"] == ("You have paid for Growth in advance: it starts on "
                                                                 "1 Jan 2033.")


def test_after_billing_a_paying_shop_gets_the_roi_statement_and_a_free_shop_the_shadow_bill(town):
    db = town.db
    paying = town.shop(town.owner(), "Paying Shop")
    free = town.shop(town.owner(), "Free Shop")
    customer = town.owner(phone=False)
    sub = plans.create_subscription(db, shop_ids=[paying.id], plan_code="growth", period="year", source="shop_paid",
                                    actor=Actor(type="admin", id=1), reason="Paid for the test",
                                    now=ist(2032, 3, 1, 9))
    town.reserve(paying, customer, ist(2032, 3, 10, 10))
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = statement_of(db, paying)
    assert st["kind"] == "roi" and st["plan_code"] == "growth" and st["price"] is None and st["pay_link"] is None
    assert st["plan"]["ends_at"] == plans.subscriptions(db).get(sub.id).ends_at
    assert st["guarantee"]["applies"] is True and st["guarantee"]["needed"] == settings.plan_guarantee_min_requests
    p = statements.parts(st, paying.name, "en")
    last_day = leads.ist_day(st["plan"]["ends_at"] - timedelta(microseconds=1))
    assert p["plan"] == f"Growth, paid until {last_day.day} {last_day:%b} {last_day.year}"
    assert p["counts"].endswith(f" Customer requests this quarter: {st['guarantee']['customer_requests']}.")
    assert p["guarantee"].startswith(f"{st['guarantee']['customer_requests']} customer requests this quarter, "
                                     f"{settings.plan_guarantee_min_requests} needed: ")
    assert statement_of(db, free)["kind"] == "shadow_bill"
    row = db.mongo[whatsapp.OUTBOX].find_one({"_id": st["outbox_id"]})
    assert row["template"] == "nearshop_shop_statement_roi" and len(row["params"]) == 4
    assert row["buttons"] == [f"statement:{st['_id']}:accurate", f"statement:{st['_id']}:not_accurate"]


def test_no_rupee_amount_appears_apart_from_the_price_lines(town, monkeypatch):
    db = town.db
    monkeypatch.setattr(demand_report, "owner_module", lambda db, shop, now=None: {"items": [
        {"query": "pvc elbow"}, {"query": "tap rs 500"}, {"query": "wall putty"}, {"query": "led bulb"},
        {"query": "fan"}]})
    free = town.shop(town.owner(), "Money Free Shop", city="Ballari")
    paying = town.shop(town.owner(), "Money Paying Shop")
    plans.create_subscription(db, shop_ids=[paying.id], plan_code="growth", period="year", source="shop_paid",
                              actor=Actor(type="admin", id=1), reason="Paid for the test", now=BUILD_AT)
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = statement_of(db, free)
    assert st["asked_for"] == ["pvc elbow", "wall putty", "led bulb"]  # the top 3; one naming money is left out
    money = re.compile(r"₹|\bRs\b|\bINR\b|rupee", re.IGNORECASE)
    for language in ("en", "kn"):
        p = statements.parts(st, free.name, language)
        assert not money.search(p["month_shop"]) and not money.search(p["counts"])
        assert money.search(p["amount_due"]) and money.search(p["price"])
        title, body = statements.notification_text(st, free.name, language)
        priced = [line for line in body.split("\n") if money.search(line)]
        assert priced == [p["amount_due"], p["price"]] and not money.search(title)
    roi = statement_of(db, paying)
    for language in ("en", "kn"):
        p = statements.parts(roi, paying.name, language)
        assert not any(money.search(v) for v in p.values())  # an ROI statement states no rupee amount at all
        assert not money.search(" ".join(statements.notification_text(roi, paying.name, language)))


# ---------------------------------------------------------------- delivery


def test_english_and_kannada_templates_and_free_form_inside_the_window(town, monkeypatch):
    db = town.db
    monkeypatch.setattr(settings, "billing_starts_on", datetime(2033, 1, 1).date())
    en_owner, kn_owner, window_owner, no_phone = (town.owner("en"), town.owner("kn"), town.owner("en"),
                                                  town.owner(phone=False))
    en_shop = town.shop(en_owner, "English Shop", city="Ballari")
    kn_shop = town.shop(kn_owner, "Kannada Shop", city="Ballari")
    window_shop = town.shop(window_owner, "Window Shop")
    app_only = town.shop(no_phone, "App Only Shop")
    # The window owner wrote an hour before the statements: the window stays open well past the 30-minute margin.
    db.mongo[whatsapp.SESSIONS].insert_one({"_id": window_owner.id, "last_inbound_at": BUILD_AT - timedelta(hours=1),
                                            "owner_inbound_at": BUILD_AT - timedelta(hours=1)})
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = {s["shop_id"]: s for s in mine(db, town)}
    assert st[app_only.id]["delivery"] == "not_sent" and st[app_only.id]["outbox_id"] is None
    assert db.notifications.find({"user_id": no_phone.id, "kind": "statement_free"})  # in the app all the same
    assert (st[en_shop.id]["route"], st[kn_shop.id]["route"], st[window_shop.id]["route"]) == (
        "template", "template", "in_window")
    window_row = db.mongo[whatsapp.OUTBOX].find_one({"_id": st[window_shop.id]["outbox_id"]})
    assert (window_row["route"], window_row["category"]) == ("in_window", "service")

    whatsapp.FAKE.reset()
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["sent"] == 3
    sent = {m["phone"]: m for m in whatsapp.FAKE.sent}
    en = sent[en_owner.phone]
    assert (en["template"], en["language"]) == ("nearshop_shop_statement_free", "en")
    body = [p["text"] for p in en["components"][0]["parameters"]]
    assert body == ["March 2032, English Shop",
                    "0 reservation requests (0 collected), 0 delivery orders, 0 call taps and 0 directions taps.",
                    "Growth would cost ₹83 a month at the founding price (₹999 for the first year)."]
    sid = st[en_shop.id]["_id"]
    assert [b["parameters"][0]["payload"] for b in en["components"][1:]] == [
        f"statement:{sid}:{k}" for k in ("accurate", "not_accurate", "would_pay", "not_now")]
    kn = sent[kn_owner.phone]
    assert (kn["template"], kn["language"]) == ("nearshop_shop_statement_free", "kn")
    kn_body = [p["text"] for p in kn["components"][0]["parameters"]]
    assert kn_body[0] == "ಮಾರ್ಚ್ 2032, Kannada Shop" and all(KANNADA.search(v) for v in kn_body)
    window = sent["91" + window_owner.phone]
    assert window["template"] is None
    assert window["text"].startswith("NearShop: your statement for March 2032, Window Shop.\n\n0 reservation requests")
    assert "Amount due: ₹0." in window["text"] and window["text"].endswith(
        "Reply Accurate, Not accurate, I'd pay or Not now.")
    kn_text = whatsapp.in_window_text({"kind": "statement_free", "language": "kn", "params": kn_body})
    assert kn_text.startswith("NearShop: ಮಾರ್ಚ್ 2032, Kannada Shop ರ ನಿಮ್ಮ ಹೇಳಿಕೆ.") and "ಪಾವತಿಸುತ್ತೇನೆ" in kn_text

    # Delivery statuses come back from the outbox.
    statements.sync_delivery(db)
    assert {s["delivery"] for s in mine(db, town) if s["outbox_id"]} == {"sent"}
    whatsapp.record_statuses(db, {"entry": [{"changes": [{"value": {"statuses": [
        {"id": en["message_id"], "status": "delivered", "timestamp": str(int(datetime.now(UTC).timestamp()))}]}}]}]})
    assert statements.sync_delivery(db) == 1
    assert statement_of(db, en_shop)["delivery"] == "delivered"


# ---------------------------------------------------------------- replies


def test_each_reply_is_stored_once_and_the_not_accurate_reason_is_captured(town, monkeypatch):
    db = town.db
    monkeypatch.setattr(settings, "billing_starts_on", datetime(2033, 1, 1).date())
    monkeypatch.setattr(settings, "billing_upi_id", "nearshop-test@testbank")
    monkeypatch.setattr(settings, "billing_payee_name", "NearShop Test")
    owner = town.owner()
    shop = town.shop(owner, "Reply Shop", city="Ballari")
    other = town.shop(town.owner(), "Other Reply Shop")
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    sid = statement_of(db, shop)["_id"]
    at = BUILD_AT + timedelta(hours=1)

    assert button(db, owner.phone, at, f"statement:{sid}:accurate") == (
        "Thank you. We have recorded that your statement is accurate.")
    assert button(db, owner.phone, at, f"statement:{sid}:accurate") == (
        "We have already recorded this answer for your statement.")
    assert button(db, owner.phone, at, f"statement:{sid}:not_accurate").startswith(
        "Thank you for telling us. What is not right?")
    assert text(db, owner.phone, at + timedelta(minutes=2), "  The call taps look   too high ") == (
        "Thank you. We have recorded your reason and will look into it.")
    # Only the next message is the reason: a later one goes to the stock bot as before.
    assert text(db, owner.phone, at + timedelta(minutes=3), "hello").startswith("To update your stock on NearShop")

    reply = button(db, owner.phone, at, f"statement:{sid}:would_pay")
    req = billing.requests(db).find_one({"shop_id": shop.id})
    assert req is not None and req.kind == billing.RequestKind.ADVANCE and req.founding and req.amount_paise == 99900
    assert reply == (f"Thank you. To pay for Growth (Growth would cost ₹83 a month at the founding price (₹999 for the "
                     f"first year)), pay ₹999.00 by UPI to nearshop-test@testbank (NearShop Test) with the note "
                     f"{req.number}. You can also pay from {statements.site_link('/owner/billing')}.")
    assert button(db, owner.phone, at, f"statement:{sid}:not_now").startswith("Thank you. You stay on the Free plan")
    # Another shop's statement, a made-up one and a reply the statement has no button for change nothing.
    other_sid = statement_of(db, other)["_id"]
    assert button(db, owner.phone, at, f"statement:{other_sid}:accurate") == (
        "We could not find that statement for your shop.")
    assert button(db, owner.phone, at, "statement:abc:accurate") == "We could not find that statement for your shop."

    rows = {r["kind"]: r for r in db.mongo[statements.REPLIES].find({"statement_id": sid})}
    assert set(rows) == {"accurate", "not_accurate", "would_pay", "not_now"}
    assert db.mongo[statements.REPLIES].count_documents({"statement_id": sid}) == 4
    assert rows["not_accurate"]["reason"] == "The call taps look too high"
    assert rows["would_pay"]["payment_request_id"] == req.id
    assert db.mongo[statements.REPLIES].count_documents({"statement_id": other_sid}) == 0
    assert db.mongo[whatsapp.SESSIONS].find_one({"_id": owner.id})["statement_reason"] is None


def test_a_reply_typed_inside_the_window_answers_the_latest_statement(town):
    db = town.db
    owner = town.owner("kn")
    shop = town.shop(owner, "Typed Reply Shop")
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    sid = statement_of(db, shop)["_id"]
    at = BUILD_AT + timedelta(hours=2)
    assert text(db, owner.phone, at, "ನಿಖರ") == words.STATEMENTS["reply_accurate"]["kn"]
    assert text(db, owner.phone, at, "Not now!") == words.STATEMENTS["reply_not_now"]["kn"]
    assert text(db, owner.phone, at, "I’d pay").startswith("ಧನ್ಯವಾದಗಳು. Growth ಗೆ ಪಾವತಿಸಲು")  # billing not set up here
    assert {r["kind"] for r in db.mongo[statements.REPLIES].find({"statement_id": sid})} == {
        "accurate", "not_now", "would_pay"}


# ---------------------------------------------------------------- the owner and admin endpoints


def test_owner_and_admin_endpoints(town, admin):
    db = town.db
    owner = town.owner()
    shop = town.shop(owner, "Endpoint Shop")
    second = town.owner()
    second_shop = town.shop(second, "Second Endpoint Shop")
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    with TestClient(app) as c:
        login(c, owner.email)
        res = c.get("/api/owner/statements")
        assert res.status_code == 200, res.text
        data = res.json()
        [st] = data["statements"]
        assert st["month"] == KEY and st["kind"] == "shadow_bill" and set(st["counts"]) == set(leads.COUNT_FIELDS)
        assert st["text"]["month_shop"] == "March 2032, Endpoint Shop" and st["replies"] == []
        local = leads.ist_day(utcnow())
        assert data["current"]["month"] == leads.month_key(local.year, local.month)
        assert data["current"]["complete"] is False
        assert c.get(f"/api/owner/statements/{KEY}").json()["statement"]["id"] == st["id"]
        assert c.get("/api/owner/statements/2031-01").status_code == 404
        assert c.get("/api/owner/statements/2031-13").status_code == 422
        now_key = leads.month_key(local.year, local.month)
        assert c.get(f"/api/owner/statements/{now_key}").json()["current"]["month"] == now_key

    at = BUILD_AT + timedelta(hours=1)
    sid, second_sid = statement_of(db, shop)["_id"], statement_of(db, second_shop)["_id"]
    button(db, owner.phone, at, f"statement:{sid}:accurate")
    button(db, second.phone, at, f"statement:{second_sid}:accurate")
    button(db, second.phone, at + timedelta(minutes=1), f"statement:{second_sid}:not_accurate")  # the later answer
    whatsapp.FAKE.reset()
    whatsapp.send_due(db, client=whatsapp.FAKE)
    res = admin.get("/api/admin/statements", params={"month": KEY})
    assert res.status_code == 200, res.text
    summary = res.json()
    assert summary["built"] == 2 and summary["by_kind"] == {"shadow_bill": 2, "roi": 0}
    assert summary["sent"] == 2 and summary["failed"] == 0
    # Delivery is measured over the active shops (the statements built), not over the statements sent.
    assert summary["sent_pct"] == 100.0 and summary["delivered_pct"] == 0.0
    assert summary["replies"] == {"accurate": 2, "not_accurate": 1, "would_pay": 0, "not_now": 0}
    assert summary["answered_accurate"] == 1 and summary["accurate_share_pct"] == 50.0
    assert admin.get("/api/admin/statements").json()["month"] == KEY  # the latest month by default
    with TestClient(app) as c:
        login(c, owner.email)
        assert c.get("/api/admin/statements").status_code == 403


# ---------------------------------------------------------------- review fixes


def test_an_roi_statement_at_a_quarter_boundary_reports_the_quarter_that_ended_with_the_month(town):
    db = town.db
    owner = town.owner()
    shop = town.shop(owner, "Quarter Shop")
    customer = town.owner(phone=False)
    # Paid from 1 Jan 2032 IST: the first quarter is January to March, which ends as March (the stated month) ends.
    sub = plans.create_subscription(db, shop_ids=[shop.id], plan_code="growth", period="year", source="shop_paid",
                                    actor=Actor(type="admin", id=1), reason="Paid for the test",
                                    now=ist(2032, 1, 1, 0, 0))
    for day in (5, 15, 25):
        town.reserve(shop, customer, ist(2032, 2, day, 11))
    town.reserve(shop, customer, ist(2032, 3, 20, 11))
    town.reserve(shop, customer, ist(2032, 4, 1, 9))  # after the month: not in it
    # By 10:30 IST on 1 Apr the subscriptions job has closed the quarter and started the next one.
    plans.run_jobs(db, BUILD_AT)
    assert plans.subscriptions(db).get(sub.id).quarters_done == 1
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = statement_of(db, shop)
    assert st["kind"] == "roi"
    g = st["guarantee"]
    assert g["applies"] is True and g["customer_requests"] == 4 and g["met"] is True
    p = statements.parts(st, shop.name, "en")
    assert p["counts"].endswith(" Customer requests this quarter: 4.")
    assert p["guarantee"].startswith(f"4 customer requests this quarter, {settings.plan_guarantee_min_requests} "
                                     "needed: ")
    # Mid-quarter, before the job closes anything, the same quarter is counted up to the month's end.
    assert statements.guarantee_for_month(db, plans.subscriptions(db).get(sub.id), 2032, 4,
                                          ist(2032, 5, 1, 10, 30))["customer_requests"] == 1


def test_a_statement_cancelled_unsent_is_sent_again_by_template(town):
    db = town.db
    owner = town.owner()
    shop = town.shop(owner, "Resend Shop")
    # The owner wrote an hour before: the statement is routed in the window.
    db.mongo[whatsapp.SESSIONS].insert_one({"_id": owner.id, "last_inbound_at": BUILD_AT - timedelta(hours=1),
                                            "owner_inbound_at": BUILD_AT - timedelta(hours=1)})
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = statement_of(db, shop)
    assert st["route"] == "in_window"
    first = st["outbox_id"]
    # The sender comes to it after the window closed.
    db.mongo[whatsapp.OUTBOX].update_one({"_id": first}, {"$set": {"created_at": utcnow()}})
    db.mongo[whatsapp.SESSIONS].update_one({"_id": owner.id}, {"$set": {
        "last_inbound_at": utcnow() - timedelta(days=2), "owner_inbound_at": utcnow() - timedelta(days=2)}})
    whatsapp.FAKE.reset()
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["cancelled"] == 1
    assert db.mongo[whatsapp.OUTBOX].find_one({"_id": first})["error"] == "window_closed"
    assert statements.sync_delivery(db) == 1
    st = statement_of(db, shop)
    assert st["outbox_id"] != first and st["route"] == "template" and st["delivery"] == "queued"
    assert st["resends"] == 1
    row = db.mongo[whatsapp.OUTBOX].find_one({"_id": st["outbox_id"]})
    assert (row["template"], row["route"], row["status"]) == ("nearshop_shop_statement_free", "template", "queued")
    assert row["buttons"] == [f"statement:{st['_id']}:{k}" for k in statements.REPLY_KINDS]
    whatsapp.FAKE.reset()
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["sent"] == 1
    [sent] = whatsapp.FAKE.sent
    assert sent["template"] == "nearshop_shop_statement_free"
    statements.sync_delivery(db)
    assert statement_of(db, shop)["delivery"] == "sent"

    # A row left unsent past STALE_AFTER is sent again too, but only RESEND_MAX times in all.
    for n in range(2, statements.RESEND_MAX + 2):
        row_id = statement_of(db, shop)["outbox_id"]
        db.mongo[whatsapp.OUTBOX].update_one({"_id": row_id}, {"$set": {"status": "cancelled", "error": "stale"}})
        db.mongo[statements.STATEMENTS].update_one({"_id": st["_id"]}, {"$set": {"delivery": "queued"}})
        statements.sync_delivery(db)
        after = statement_of(db, shop)
        if n <= statements.RESEND_MAX:
            assert after["resends"] == n and after["outbox_id"] != row_id and after["delivery"] == "queued"
        else:
            assert after["resends"] == statements.RESEND_MAX and after["outbox_id"] == row_id
            assert after["delivery"] == "cancelled" and after["delivery_error"] == "stale"


def test_sync_skips_old_statements_and_closes_those_whose_outbox_row_is_gone(town):
    db = town.db
    shops = [town.shop(town.owner(), f"Sync Shop {i}") for i in range(2)]
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    gone, old = (statement_of(db, s) for s in shops)
    db.mongo[whatsapp.OUTBOX].delete_one({"_id": gone["outbox_id"]})  # removed by its TTL
    db.mongo[statements.STATEMENTS].update_one({"_id": old["_id"]}, {"$set": {
        "built_at": utcnow() - timedelta(days=statements.SYNC_DAYS + 1)}})
    db.mongo[whatsapp.OUTBOX].update_one({"_id": old["outbox_id"]}, {"$set": {"status": "sent"}})
    assert statements.sync_delivery(db) == 0
    assert statement_of(db, shops[0])["delivery_final"] is True
    assert statement_of(db, shops[1])["delivery"] == "queued"  # too old to sync: left as it was
    assert db.mongo[statements.STATEMENTS].count_documents({
        "_id": {"$in": [gone["_id"], old["_id"]]}, "delivery": "queued", "delivery_final": {"$ne": True},
        "built_at": {"$gte": utcnow() - timedelta(days=statements.SYNC_DAYS)}}) == 0


def test_delivered_pct_is_over_the_statements_built(town):
    db = town.db
    whatsapp_shop = town.shop(town.owner(), "Delivered Shop")
    town.shop(town.owner(phone=False), "App Only Delivered Shop")  # in the app only: built, never sent
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    whatsapp.FAKE.reset()
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["sent"] == 1
    db.mongo[whatsapp.OUTBOX].update_one({"_id": statement_of(db, whatsapp_shop)["outbox_id"]},
                                         {"$set": {"status": "delivered"}})
    summary = statements.admin_summary(db, KEY)
    assert (summary["built"], summary["sent"], summary["delivered"]) == (2, 1, 1)
    assert summary["sent_pct"] == 50.0 and summary["delivered_pct"] == 50.0
    assert summary["delivered_of_sent_pct"] == 100.0


def test_not_now_typed_to_an_roi_statement_goes_to_the_stock_bot(town):
    db = town.db
    owner = town.owner()
    shop = town.shop(owner, "ROI Typed Shop")
    plans.create_subscription(db, shop_ids=[shop.id], plan_code="growth", period="year", source="shop_paid",
                              actor=Actor(type="admin", id=1), reason="Paid for the test", now=ist(2032, 3, 1, 9))
    statements.build_month(db, YEAR, MONTH, BUILD_AT)
    st = statement_of(db, shop)
    assert st["kind"] == "roi"
    at = BUILD_AT + timedelta(hours=1)
    for words_typed in ("Not now", "I'd pay"):
        reply = text(db, owner.phone, at, words_typed)
        assert reply != "We could not find that statement for your shop."
        assert reply.startswith("To update your stock on NearShop")
    assert text(db, owner.phone, at, "Accurate") == "Thank you. We have recorded that your statement is accurate."
    assert {r["kind"] for r in db.mongo[statements.REPLIES].find({"statement_id": st["_id"]})} == {"accurate"}


# ---------------------------------------------------------------- the words


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


@pytest.mark.parametrize("key", sorted(words.STATEMENTS))
def test_every_statement_text_has_both_languages_with_the_same_parameters(key):
    entry = words.STATEMENTS[key]
    assert set(entry) == set(words.LANGUAGES)
    assert placeholders(entry["kn"]) == placeholders(entry["en"])
    if placeholders(entry["en"]) - {"month", "shop", "counts", "amount_due", "price", "plan", "guarantee",
                                    "month_shop"} or not placeholders(entry["en"]):
        assert KANNADA.search(entry["kn"]), "the Kannada text is Kannada"
