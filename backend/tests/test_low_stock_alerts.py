"""Low-stock alerts (P9.14, app/services/alerts.py; plan section 8.18): a shop on Growth or above gets one message a
day at its alert hour listing up to 10 listings at or below their low-stock level, lowest first, with a link to the
inventory page; inside the owner's open window as a free-form message, otherwise as the template. None when nothing
is low, none for a Free shop, none when switched off, and at most one per shop per day. Every message goes through
the fake WhatsApp client; nothing reaches Meta. The tests use their own owners, shops and listings, removed
afterwards."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.core.config import WEEKDAYS, settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.main import app
from app.models import Product, Role, Shop, User
from app.models.billing import Actor
from app.models.core import VerificationStatus, months_after
from app.services import alerts, plans, whatsapp
from app.services.privacy import set_consent
from tests.conftest import CENTER, HEADERS, PASSWORD, login

IST = ZoneInfo("Asia/Kolkata")
PW_HASH = hash_password(PASSWORD)
ADMIN = Actor(type="admin", id=0)
OUTBOX_COLLECTIONS = (whatsapp.OUTBOX, whatsapp.SESSIONS, whatsapp.STOCK_CHECKS, whatsapp.INBOUND, whatsapp.PENDING)


def ist(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A naive UTC time at hour:minute IST, day_offset days from today (IST)."""
    day = datetime.now(IST).date() + timedelta(days=day_offset)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def ist_day(at: datetime) -> str:
    return at.replace(tzinfo=UTC).astimezone(IST).date().isoformat()


class World:
    """Owners with verified phones and the WhatsApp consent, their shops and listings; removed afterwards."""

    def __init__(self, db: Database):
        self.db = db
        self.tag = uuid.uuid4().hex[:8]
        self.users: list[int] = []
        self.shops: list[int] = []
        self.n = 0

    def shop(self, *, growth: bool, language: str = "en", hour: int | None = None) -> tuple[User, Shop]:
        self.n += 1
        n, now = self.n, utcnow()
        owner = self.db.users.insert(User(
            name=f"Alert Owner {n}", email=f"alert-owner{n}-{self.tag}@example.com", phone=f"90000{71000 + n}",
            phone_verified_at=now - timedelta(days=30), password_hash=PW_HASH, role=Role.OWNER, language=language))
        self.users.append(owner.id)
        set_consent(self.db, owner.id, "whatsapp", True)
        shop = self.db.shops.insert(Shop(
            owner_id=owner.id, name=f"Alert Shop {n}", slug=f"alert-shop-{n}-{self.tag}", address_line=f"{n} Stock Road",
            city="Mysuru", lat=CENTER[0] + 0.003, lng=CENTER[1], category_ids=[],
            verification_status=VerificationStatus.VERIFIED, is_verified=True, kyc_type="udyam",
            kyc_number=f"UDYAM-KR-23-{8100000 + n:07d}", phone_verified_at=now, submitted_at=now, decided_at=now,
            verified_until=months_after(now), **({"low_stock_alert_hour": hour} if hour is not None else {})))
        self.shops.append(shop.id)
        if growth:
            plans.create_subscription(self.db, shop_ids=[shop.id], plan_code="growth", period="year",
                                      source="shop_paid", actor=ADMIN, reason="Test Growth plan",
                                      now=now - timedelta(days=5))
        return owner, shop

    def listing(self, shop: Shop, name: str, quantity: int, threshold: int = 5, active: bool = True) -> Product:
        return self.db.products.insert(Product(
            shop_id=shop.id, category_id=1, name=name, keywords="alerttest", price=10, quantity=quantity,
            low_stock_threshold=threshold, is_active=active))

    def cleanup(self) -> None:
        db = self.db
        db.mongo[plans.SUBSCRIPTIONS].delete_many({"shop_ids": {"$in": self.shops}})
        db.mongo[plans.EVENTS].delete_many({"shop_ids": {"$in": self.shops}})
        db.notifications.delete_many({"user_id": {"$in": self.users}})
        db.mongo["consents"].delete_many({"user_id": {"$in": self.users}})
        db.products.delete_many({"shop_id": {"$in": self.shops}})
        db.shops.delete_many({"_id": {"$in": self.shops}})
        db.users.delete_many({"_id": {"$in": self.users}})


def clear(db: Database) -> None:
    for name in OUTBOX_COLLECTIONS:
        db.mongo[name].delete_many({})
    whatsapp.FAKE.reset()
    # Other shops (the seeded ones) may have been considered on a run of these tests: forget their days.
    db.shops.update_many({}, {"$unset": {alerts.DAY_FIELD: "", alerts.OUTCOME_FIELD: ""}})


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    monkeypatch.setattr(settings, "billing_starts_on", date(2020, 1, 1))
    clear(db)
    w = World(db)
    yield w
    w.cleanup()
    clear(db)


def alerts_to(db: Database, owner: User) -> list[dict]:
    return list(db.notifications.raw.find({"user_id": owner.id, "kind": alerts.KIND}, sort=[("_id", 1)]))


def outbox(db: Database, owner: User) -> list[dict]:
    return list(db.mongo[whatsapp.OUTBOX].find({"user_id": owner.id, "kind": alerts.KIND}, sort=[("_id", 1)]))


def message_from(db: Database, phone: str, at: datetime) -> None:
    """A text from this 10-digit phone, received at `at`, through the webhook's handler: opens the owner's window."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "91" + phone, "id": f"wamid.in.{uuid.uuid4().hex}", "timestamp": str(int(at.replace(
            tzinfo=UTC).timestamp())), "type": "text", "text": {"body": "hello"}}]}}]}]}
    assert whatsapp.handle_messages(db, payload, client=whatsapp.FAKE, now=at)["handled"] == 1


# ---------------------------------------------------------------- one message at the hour


def test_one_message_at_the_hour_with_at_most_10_items_lowest_first(db, world):
    owner, shop = world.shop(growth=True)
    for q in (7, 3, 11, 0, 9, 1, 5, 2, 8, 4, 6, 10):  # 12 listings at or below a threshold of 12
        world.listing(shop, f"Low item {q:02d}", q, threshold=12)
    world.listing(shop, "Plenty item", 50, threshold=5)  # above its level
    world.listing(shop, "Edge item", 5, threshold=5)  # at its level: low
    world.listing(shop, "Hidden item", 0, threshold=5, active=False)  # not an active listing

    assert alerts.send_low_stock_alerts(db, ist(0, 17, 59))["sent"] == 0  # before the hour (18:00 by default)
    assert alerts_to(db, owner) == []

    result = alerts.send_low_stock_alerts(db, ist(0, 18, 5))
    assert result["sent"] >= 1 and result["template"] >= 1
    [note] = alerts_to(db, owner)
    assert note["title"] == f"Low stock at {shop.name}" and note["link"] == "/shop/inventory"
    items = note["body"].split("; ")
    assert len(items) == 11 and items[-1] == "and 3 more"  # 13 low listings: 10 listed, the rest counted
    assert items[:10] == ["Low item 00: 0 (alert at 12)", "Low item 01: 1 (alert at 12)",
                          "Low item 02: 2 (alert at 12)", "Low item 03: 3 (alert at 12)",
                          "Low item 04: 4 (alert at 12)", "Edge item: 5 (alert at 5)",
                          "Low item 05: 5 (alert at 12)", "Low item 06: 6 (alert at 12)",
                          "Low item 07: 7 (alert at 12)", "Low item 08: 8 (alert at 12)"]
    assert db.shops.get(shop.id).low_stock_alert_outcome == "sent"

    # Outside a window it is the template: {{1}} the shop, {{2}} the list.
    [row] = outbox(db, owner)
    assert (row["template"], row["route"], row["category"]) == ("nearshop_shop_low_stock", "template", "utility")
    assert row["params"] == [shop.name, note["body"]] and row["language"] == "en"
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["sent"] == 1
    [msg] = [m for m in whatsapp.FAKE.sent if m.get("template") == "nearshop_shop_low_stock"]
    assert msg["phone"] == owner.phone
    assert [p["text"] for p in msg["components"][0]["parameters"]] == [shop.name, note["body"]]


def test_inside_an_open_window_it_goes_as_a_free_form_message(db, world):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Thread seal tape", 1, threshold=2)
    world.listing(shop, "LED bulb 9W", 0, threshold=3)
    message_from(db, owner.phone, utcnow() - timedelta(hours=1))  # the window stays open for 23 hours

    now = utcnow()
    local_hour = now.replace(tzinfo=UTC).astimezone(IST).hour
    db.shops.update_one({"_id": shop.id}, {"$set": {"low_stock_alert_hour": local_hour}})
    assert alerts.send_low_stock_alerts(db, now)["in_window"] >= 1
    [row] = outbox(db, owner)
    assert (row["route"], row["category"]) == ("in_window", "service")

    whatsapp.FAKE.reset()
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["sent"] == 1
    [msg] = list(whatsapp.FAKE.sent)
    assert msg["template"] is None and msg["phone"] == "91" + owner.phone
    assert msg["text"].startswith(f"NearShop: Low stock at {shop.name}.\n"
                                  "LED bulb 9W: 0 (alert at 3); Thread seal tape: 1 (alert at 2)")
    assert "reply Yes" in msg["text"]  # how to send the new counts, as the template's last line says


def test_a_kannada_owner_gets_the_english_alert_until_the_team_supplies_the_kannada(db, world):
    owner, shop = world.shop(growth=True, language="kn")
    world.listing(shop, "Thread seal tape", 1, threshold=2)
    alerts.send_low_stock_alerts(db, ist(0, 18, 5))
    [note] = alerts_to(db, owner)
    assert note["title"] == f"Low stock at {shop.name}" and note["body"] == "Thread seal tape: 1 (alert at 2)"
    [row] = outbox(db, owner)
    assert row["language"] == "en"  # the template in the language of its words, not the unapproved kn version


def test_a_long_list_fits_one_template_variable(db, world):
    owner, shop = world.shop(growth=True)
    for q in range(10):
        world.listing(shop, f"Extremely long product name number {q} with many words in it, size XL", q, threshold=10)
    alerts.send_low_stock_alerts(db, ist(0, 18, 5))
    [row] = outbox(db, owner)
    assert len(row["params"][1]) <= whatsapp.PARAM_MAX and row["params"][1].count(": ") == 10


# ---------------------------------------------------------------- none


def test_none_when_nothing_is_low(db, world):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Plenty item", 40, threshold=5)
    world.listing(shop, "Hidden low item", 0, threshold=5, active=False)
    result = alerts.send_low_stock_alerts(db, ist(0, 18, 5))
    assert result["nothing_low"] >= 1 and alerts_to(db, owner) == [] and outbox(db, owner) == []
    assert db.shops.get(shop.id).low_stock_alert_outcome == "nothing_low"


def test_none_for_a_free_shop(db, world):
    owner, shop = world.shop(growth=False)
    world.listing(shop, "Low item", 1, threshold=5)
    result = alerts.send_low_stock_alerts(db, ist(0, 18, 5))
    assert result["not_entitled"] >= 1 and alerts_to(db, owner) == [] and outbox(db, owner) == []
    assert db.shops.get(shop.id).low_stock_alert_outcome == "not_entitled"


def test_none_for_a_lapsed_growth_shop(db, world):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Low item", 1, threshold=5)
    sub = plans.subscriptions(db).find_one({"shop_ids": shop.id})
    after = sub.ends_at.replace(tzinfo=UTC).astimezone(IST).date() + timedelta(days=1)
    at = datetime(after.year, after.month, after.day, 18, 5, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)
    result = alerts.send_low_stock_alerts(db, at)
    assert result["not_entitled"] >= 1 and alerts_to(db, owner) == []


def test_none_when_switched_off(db, world):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Low item", 1, threshold=5)
    db.shops.update_one({"_id": shop.id}, {"$set": {"low_stock_alerts_on": False}})
    alerts.send_low_stock_alerts(db, ist(0, 18, 5))
    assert alerts_to(db, owner) == [] and db.shops.get(shop.id).low_stock_alert_day is None


def test_none_before_the_hour_or_after_the_grace_period(db, world):
    owner, shop = world.shop(growth=True, hour=9)
    world.listing(shop, "Low item", 1, threshold=5)
    alerts.send_low_stock_alerts(db, ist(0, 8, 59))
    alerts.send_low_stock_alerts(db, ist(0, 12, 0))  # 3 hours after: the slot closed
    assert alerts_to(db, owner) == []
    alerts.send_low_stock_alerts(db, ist(0, 11, 59))  # a tick missed at 9:00 is caught up within 3 hours
    assert len(alerts_to(db, owner)) == 1


# ---------------------------------------------------------------- at most one per shop per day


def test_at_most_one_per_shop_per_day(db, world):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Low item", 1, threshold=5)
    for minute in (0, 1, 30):
        alerts.send_low_stock_alerts(db, ist(0, 18, minute))
    alerts.send_low_stock_alerts(db, ist(0, 20, 59))
    assert len(alerts_to(db, owner)) == 1 and len(outbox(db, owner)) == 1
    assert db.shops.get(shop.id).low_stock_alert_day == ist_day(ist(0, 18))

    # The owner moves the hour later the same day: still one today. The next day brings the next one.
    db.shops.update_one({"_id": shop.id}, {"$set": {"low_stock_alert_hour": 21}})
    alerts.send_low_stock_alerts(db, ist(0, 21, 5))
    assert len(alerts_to(db, owner)) == 1
    alerts.send_low_stock_alerts(db, ist(1, 21, 5))
    assert len(alerts_to(db, owner)) == 2


def test_concurrent_runs_send_one(db, world):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Low item", 1, threshold=5)
    at = ist(0, 18, 5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: alerts.send_low_stock_alerts(Database(), at), range(4)))
    assert sum(r["sent"] for r in results) >= 1
    assert len(alerts_to(db, owner)) == 1 and len(outbox(db, owner)) == 1


def test_a_failed_send_leaves_the_day_for_the_next_tick(db, world, monkeypatch):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Low item", 1, threshold=5)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr(alerts, "notify", broken)
        with pytest.raises(RuntimeError):
            alerts.send_low_stock_alerts(db, ist(0, 18, 5))
    assert db.shops.get(shop.id).low_stock_alert_day is None  # the claim rolled back with the notification
    alerts.send_low_stock_alerts(db, ist(0, 18, 10))
    assert len(alerts_to(db, owner)) == 1


def test_the_job_runs_the_alert_under_its_lease(db, world):
    owner, shop = world.shop(growth=True)
    world.listing(shop, "Low item", 1, threshold=5)
    at = ist(0, 18, 5)
    assert jobs.RETRY_AFTER_FAILURE[jobs.LOW_STOCK_ALERTS_JOB] == timedelta(minutes=15)
    result = jobs.low_stock_alerts(lambda: at)
    assert result["day"] == ist_day(at) and result["sent"] >= 1
    assert jobs.JOBS[jobs.LOW_STOCK_ALERTS_JOB] is jobs.low_stock_alerts
    jobs.low_stock_alerts(lambda: at)
    assert len(alerts_to(db, owner)) == 1


# ---------------------------------------------------------------- the stock check stays


def test_the_alert_never_replaces_the_stock_check(db, world, monkeypatch):
    owner, shop = world.shop(growth=True)
    product = world.listing(shop, "Low item", 1, threshold=5)
    db.products.update_one({"_id": product.id}, {"$set": {"stock_updated_at": utcnow() - timedelta(days=10)}})
    now = utcnow()
    local = now.replace(tzinfo=UTC).astimezone(IST)
    db.shops.update_one({"_id": shop.id}, {"$set": {"opening_hours": local.strftime("%H:%M-23:59"),
                                                    "low_stock_alert_hour": local.hour}})
    monkeypatch.setattr(settings, "stock_check_weekly_day", WEEKDAYS[local.weekday()])

    assert alerts.send_low_stock_alerts(db, now)["sent"] >= 1
    assert whatsapp.send_stock_checks(db, now)[whatsapp.ROUTE_TEMPLATE] >= 1
    kinds = sorted(r["kind"] for r in db.mongo[whatsapp.OUTBOX].find({"user_id": owner.id}))
    assert kinds == [alerts.KIND, whatsapp.STOCK_CHECK_KIND]


# ---------------------------------------------------------------- the owner's settings


def test_the_settings_default_on_at_18_and_say_whether_the_plan_includes_the_alert(db, world):
    free_owner, _ = world.shop(growth=False)
    growth_owner, _ = world.shop(growth=True)
    with TestClient(app) as c:
        free = login(c, free_owner.email).get("/api/owner/shop", headers=HEADERS).json()
    assert (free["low_stock_alerts_on"], free["low_stock_alert_hour"], free["low_stock_alerts_entitled"]) == (
        True, 18, False)
    assert "low_stock_alert_day" not in free and "low_stock_alert_outcome" not in free
    with TestClient(app) as c:
        client = login(c, growth_owner.email)
        mine = client.get("/api/owner/shop", headers=HEADERS).json()
        assert mine["low_stock_alerts_entitled"] is True
        res = client.patch("/api/owner/shop", json={"low_stock_alerts_on": False, "low_stock_alert_hour": 7},
                           headers=HEADERS)
        assert res.status_code == 200, res.text
        assert (res.json()["low_stock_alerts_on"], res.json()["low_stock_alert_hour"]) == (False, 7)
        for bad in ({"low_stock_alert_hour": 24}, {"low_stock_alert_hour": -1}, {"low_stock_alert_hour": None},
                    {"low_stock_alerts_on": None}, {"low_stock_alert_day": "2027-01-01"}):
            assert client.patch("/api/owner/shop", json=bad, headers=HEADERS).status_code == 422, bad
    shop = db.shops.find_one({"owner_id": growth_owner.id})
    assert (shop.low_stock_alerts_on, shop.low_stock_alert_hour) == (False, 7)
