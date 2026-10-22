"""The stock-check cadence (P9.3, plan section 8.17): daily as a free-form message inside the owner's open 24-hour
service window, otherwise the template once a week; at most one a day, none on the closed day, none when everything
is fresh; and each send records its route and the category Meta reports. Every message goes through the fake client;
nothing reaches Meta."""
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.config import WEEKDAYS, settings
from app.core.database import Database, utcnow
from app.services import whatsapp
from app.services.privacy import set_consent

IST = ZoneInfo("Asia/Kolkata")
PHONES = {"a@example.com": "9000061111", "b@example.com": "9000062222"}
SHOP_B_PHONE = "9000063333"
COLLECTIONS = (whatsapp.OUTBOX, whatsapp.SESSIONS, whatsapp.STOCK_CHECKS, whatsapp.INBOUND, whatsapp.PENDING)


def ist(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A naive UTC time at hour:minute IST, day_offset days from today (IST)."""
    day = datetime.now(IST).date() + timedelta(days=day_offset)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def weekday_name(day_offset: int) -> str:
    return WEEKDAYS[(datetime.now(IST).date() + timedelta(days=day_offset)).weekday()]


def clear(db) -> None:
    for name in COLLECTIONS:
        db.mongo[name].delete_many({})
    db.notifications.delete_many({"kind": whatsapp.STOCK_CHECK_KIND})
    whatsapp.FAKE.reset()


@pytest.fixture
def owners(ids, monkeypatch):
    """Owners A and B with verified phones (verified long ago) and the WhatsApp consent; Shops A and B open at 9:00,
    never closed, with every listing counted 10 days ago. Everything put back afterwards."""
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    monkeypatch.setattr(settings, "stock_check_weekly_day", weekday_name(3))  # neither tomorrow nor the day after
    db = Database()
    clear(db)
    shops = [db.shops.get(ids["shop_a"]), db.shops.get(ids["shop_b"])]
    saved_shops = {s.id: {"opening_hours": s.opening_hours, "closed_on": s.closed_on, "phone": s.phone,
                          "phone_verified_at": s.phone_verified_at} for s in shops}
    owners, saved_users, consents = {}, {}, {}
    for email, phone in PHONES.items():
        owner = db.users.find_one({"email": email})
        saved_users[owner.id] = {"phone": owner.phone, "phone_verified_at": owner.phone_verified_at}
        consents[owner.id] = {r["_id"] for r in db.mongo["consents"].find({"user_id": owner.id}, {"_id": 1})}
        db.users.update_one({"_id": owner.id}, {"$set": {"phone": phone,
                                                         "phone_verified_at": utcnow() - timedelta(days=30)}})
        set_consent(db, owner.id, "whatsapp", True)
        owners[email[0]] = db.users.get(owner.id)
    db.shops.update_many({"_id": {"$in": list(saved_shops)}}, {"$set": {"opening_hours": None, "closed_on": None}})
    stock = {p.id: p.stock_updated_at for p in db.products.find({"shop_id": {"$in": list(saved_shops)}})}
    db.products.update_many({"_id": {"$in": list(stock)}}, {"$set": {"stock_updated_at": ist(-10, 9)}})
    yield owners
    clear(db)
    for shop_id, fields in saved_shops.items():
        db.shops.update_one({"_id": shop_id}, {"$set": fields})
    for product_id, updated in stock.items():
        db.products.update_one({"_id": product_id}, {"$set": {"stock_updated_at": updated}})
    for user_id, fields in saved_users.items():
        db.users.update_one({"_id": user_id}, {"$set": fields})
        db.mongo["consents"].delete_many({"user_id": user_id, "_id": {"$nin": list(consents[user_id])}})


def message_from(db, phone: str, at: datetime) -> None:
    """A text from this 10-digit phone, received at `at`, through the webhook's handler."""
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "91" + phone, "id": f"wamid.in.{uuid.uuid4().hex}", "timestamp": str(int(at.replace(
            tzinfo=UTC).timestamp())), "type": "text", "text": {"body": "hello"}}]}}]}]}
    assert whatsapp.handle_messages(db, payload, client=whatsapp.FAKE, now=at)["handled"] == 1


def opening_now(db, monkeypatch, *shop_ids: int) -> datetime:
    """Shops that opened this minute (IST) and today as the weekly day, so a check runs on the real clock, which
    send_due also reads. Returns now."""
    now = utcnow()
    local = now.replace(tzinfo=UTC).astimezone(IST)
    db.shops.update_many({"_id": {"$in": list(shop_ids)}}, {"$set": {"opening_hours": local.strftime("%H:%M-23:59")}})
    monkeypatch.setattr(settings, "stock_check_weekly_day", WEEKDAYS[local.weekday()])
    return now


def check_row(db, shop_id: int, day_offset: int) -> dict | None:
    day = (datetime.now(IST).date() + timedelta(days=day_offset)).isoformat()
    return db.mongo[whatsapp.STOCK_CHECKS].find_one({"_id": f"{shop_id}:{day}"})


def outbox(db, user_id: int) -> list[dict]:
    return list(db.mongo[whatsapp.OUTBOX].find({"user_id": user_id, "kind": whatsapp.STOCK_CHECK_KIND},
                                               sort=[("_id", 1)]))


def test_an_open_window_gets_the_daily_check_as_a_free_form_message(owners, ids):
    db = Database()
    owner = owners["a"]
    message_from(db, owner.phone, ist(0, 20))  # last evening: the window is open until 20:00 the next day
    assert whatsapp.window_closes_at(db, owner.id) == ist(0, 20) + timedelta(hours=24)

    result = whatsapp.send_stock_checks(db, ist(1, 9, 5))  # tomorrow is not the weekly template day
    assert result["in_window"] >= 1 and check_row(db, ids["shop_a"], 1)["route"] == "in_window"
    [row] = outbox(db, owner.id)
    assert (row["route"], row["category"]) == ("in_window", "service")
    # The shop without a window waits for its weekly day.
    assert check_row(db, ids["shop_b"], 1)["decision"] == "waiting" and outbox(db, owners["b"].id) == []

    whatsapp.FAKE.reset()
    sent = whatsapp.send_due(db, client=whatsapp.FAKE)  # the notification is dated by the real clock
    assert sent["sent"] == 1
    [msg] = list(whatsapp.FAKE.sent)
    assert msg["template"] is None and msg["phone"] == "91" + owner.phone
    assert msg["text"].startswith("NearShop: Stock check for Shop A.") and "(counted 11 days ago)" in msg["text"]
    assert "reply Yes" in msg["text"]  # how to reply, as the template's last line says
    done = db.mongo[whatsapp.OUTBOX].find_one({"_id": row["_id"]})
    assert done["status"] == "sent" and done["cost_estimate"] == 0


def test_a_window_closing_within_30_minutes_does_not_count(owners, ids):
    db = Database()
    message_from(db, owners["a"].phone, ist(0, 9, 20))  # closes at 9:20 tomorrow, 15 minutes after the check
    whatsapp.send_stock_checks(db, ist(1, 9, 5))
    assert check_row(db, ids["shop_a"], 1)["decision"] == "waiting" and outbox(db, owners["a"].id) == []


def test_a_message_from_the_shops_own_number_does_not_open_the_owners_window(owners, ids):
    db = Database()
    db.shops.update_one({"_id": ids["shop_b"]}, {"$set": {"phone": SHOP_B_PHONE, "phone_verified_at": utcnow()}})
    message_from(db, SHOP_B_PHONE, ist(0, 20))
    assert db.mongo[whatsapp.SESSIONS].find_one({"_id": owners["b"].id})["last_inbound_at"] == ist(0, 20)
    assert whatsapp.window_closes_at(db, owners["b"].id) is None
    whatsapp.send_stock_checks(db, ist(1, 9, 5))
    assert check_row(db, ids["shop_b"], 1)["decision"] == "waiting"


def test_a_shop_without_a_window_gets_the_template_only_on_the_weekly_day(owners, ids):
    """Seven days on a controlled clock: Owner A messages every evening and gets 7 in-window checks and no
    template; Owner B never messages and gets 1 template, on the weekly day."""
    db = Database()
    a, b = owners["a"], owners["b"]
    for day in range(1, 8):
        message_from(db, a.phone, ist(day - 1, 20))
        whatsapp.send_stock_checks(db, ist(day, 9, 5))
        whatsapp.send_stock_checks(db, ist(day, 10))  # a later tick the same day changes nothing
    a_rows, b_rows = outbox(db, a.id), outbox(db, b.id)
    assert [r["route"] for r in a_rows] == ["in_window"] * 7
    assert [(r["route"], r["category"], r["template"]) for r in b_rows] == [
        ("template", "utility", "nearshop_shop_stock_check")]
    assert check_row(db, ids["shop_b"], 3)["route"] == "template"
    assert [check_row(db, ids["shop_b"], d)["decision"] for d in (1, 2, 4, 5, 6, 7)] == ["waiting"] * 6


def test_at_most_one_check_a_day(owners, ids):
    db = Database()
    message_from(db, owners["a"].phone, ist(0, 20))
    for minute in (0, 1, 30, 90, 170):
        whatsapp.send_stock_checks(db, ist(1, 9) + timedelta(minutes=minute))
    assert len(outbox(db, owners["a"].id)) == 1
    assert db.mongo[whatsapp.STOCK_CHECKS].count_documents({"shop_id": ids["shop_a"]}) == 1


def test_none_on_the_closed_day_and_the_template_moves_to_the_next_day(owners, ids, monkeypatch):
    db = Database()
    message_from(db, owners["a"].phone, ist(0, 20))
    db.shops.update_many({"_id": {"$in": [ids["shop_a"], ids["shop_b"]]}},
                         {"$set": {"closed_on": weekday_name(1) + "s"}})
    monkeypatch.setattr(settings, "stock_check_weekly_day", weekday_name(1).lower())  # the closed day itself
    whatsapp.send_stock_checks(db, ist(1, 9, 5))
    assert check_row(db, ids["shop_a"], 1)["decision"] == "closed"  # even with the window open
    assert check_row(db, ids["shop_b"], 1)["decision"] == "closed"
    assert outbox(db, owners["a"].id) == [] == outbox(db, owners["b"].id)
    whatsapp.send_stock_checks(db, ist(2, 9, 5))  # the weekly template goes the day after instead
    assert [r["route"] for r in outbox(db, owners["b"].id)] == ["template"]


def test_none_when_every_listing_is_fresh(owners, ids, monkeypatch):
    db = Database()
    message_from(db, owners["a"].phone, ist(0, 20))  # A's window is open
    monkeypatch.setattr(settings, "stock_check_weekly_day", weekday_name(1))  # and it is B's template day
    db.products.update_many({"shop_id": {"$in": [ids["shop_a"], ids["shop_b"]]}},
                            {"$set": {"stock_updated_at": ist(-1, 10)}})  # 47 hours before the check
    whatsapp.send_stock_checks(db, ist(1, 9, 5))
    assert check_row(db, ids["shop_a"], 1)["outcome"] == "fresh"
    assert check_row(db, ids["shop_b"], 1)["outcome"] == "fresh"
    assert outbox(db, owners["a"].id) == [] == outbox(db, owners["b"].id)


def test_each_send_records_its_route_and_the_category_meta_reports(owners, ids, monkeypatch):
    db = Database()
    a, b = owners["a"], owners["b"]
    message_from(db, a.phone, utcnow() - timedelta(hours=1))  # A's window is open now, by the real clock
    now = opening_now(db, monkeypatch, ids["shop_a"], ids["shop_b"])
    assert whatsapp.send_stock_checks(db, now)["sent"] >= 2
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["sent"] == 2
    [in_window], [template] = outbox(db, a.id), outbox(db, b.id)
    assert (in_window["route"], in_window["category"]) == ("in_window", "service")
    assert (template["route"], template["category"]) == ("template", "utility")

    def status(row, category, billable, kind):
        return {"id": row["message_id"], "status": "sent", "timestamp": str(int(time.time())),
                "pricing": {"billable": billable, "pricing_model": "PMP", "category": category, "type": kind}}

    # Meta classed the template as marketing: the row records it and the marketing rate.
    payload = {"entry": [{"changes": [{"value": {"statuses": [status(template, "marketing", True, "regular"),
                                                              status(in_window, "service", False,
                                                                     "free_customer_service")]}}]}]}
    assert whatsapp.record_statuses(db, payload)["recorded"] == 2
    [in_window], [template] = outbox(db, a.id), outbox(db, b.id)
    assert (template["meta_category"], template["billable"], template["pricing_type"]) == (
        "marketing", True, "regular")
    assert template["cost_estimate"] == settings.whatsapp_marketing_cost_inr == 0.97
    assert (in_window["meta_category"], in_window["billable"], in_window["cost_estimate"]) == ("service", False, 0)
    day = now.replace(tzinfo=UTC).astimezone(IST).date().isoformat()
    assert db.mongo[whatsapp.STOCK_CHECKS].find_one({"_id": f"{ids['shop_a']}:{day}"})["route"] == "in_window"
    assert db.mongo[whatsapp.STOCK_CHECKS].find_one({"_id": f"{ids['shop_b']}:{day}"})["route"] == "template"


def test_a_callback_held_before_the_id_was_saved_keeps_its_pricing(owners):
    db = Database()
    message_id = f"wamid.held.{uuid.uuid4().hex}"
    early = {"id": message_id, "status": "sent", "timestamp": str(int(time.time())),
             "pricing": {"billable": True, "category": "utility", "type": "regular"}}
    assert whatsapp.record_status(db, early, utcnow()) == "held"
    row_id = db.next_id(whatsapp.OUTBOX)
    now = utcnow()
    db.mongo[whatsapp.OUTBOX].insert_one({"_id": row_id, "notification_id": 0, "user_id": owners["b"].id,
                                          "channel": "whatsapp", "kind": "stock_check", "template": "t",
                                          "language": "en", "status": "sent", "attempts": 1, "created_at": now,
                                          "next_attempt_at": now, "message_id": message_id})
    assert whatsapp.apply_pending(db, message_id, now) == 1
    row = db.mongo[whatsapp.OUTBOX].find_one({"_id": row_id})
    assert row["meta_category"] == "utility" and row["cost_estimate"] == settings.whatsapp_utility_cost_inr


def test_an_in_window_check_is_not_sent_once_the_window_has_closed(owners, ids, monkeypatch):
    db = Database()
    message_from(db, owners["a"].phone, utcnow() - timedelta(hours=1))
    whatsapp.send_stock_checks(db, opening_now(db, monkeypatch, ids["shop_a"]))
    assert [r["route"] for r in outbox(db, owners["a"].id)] == ["in_window"]
    db.mongo[whatsapp.SESSIONS].update_one({"_id": owners["a"].id},
                                           {"$set": {"owner_inbound_at": utcnow() - timedelta(hours=25)}})
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["cancelled"] == 1
    [row] = outbox(db, owners["a"].id)
    assert (row["status"], row["error"]) == ("cancelled", "window_closed")
    assert not [m for m in whatsapp.FAKE.sent if m.get("template") is None and m["text"].startswith("NearShop: Stock")]


def test_the_weekly_day_and_the_closed_day_are_read_from_names():
    assert whatsapp.closed_weekday("Sunday") == whatsapp.closed_weekday(" sundays ") == whatsapp.closed_weekday("Sun")
    assert whatsapp.closed_weekday("Sunday") == 6 and whatsapp.closed_weekday(None) is None
    assert whatsapp.closed_weekday("never") is None
    assert type(settings).model_fields["stock_check_weekly_day"].default == "Monday"
    assert type(settings)._weekday_name("  tuesday ") == "Tuesday"

    class Config:
        stock_check_weekly_day = "Monday"

    assert whatsapp.template_weekday({"closed_on": None}, Config) == 0
    assert whatsapp.template_weekday({"closed_on": "Monday"}, Config) == 1
    assert whatsapp.template_weekday({"closed_on": "Friday"}, Config) == 0


def test_a_free_pricing_type_costs_nothing_without_the_billable_flag():
    """Meta is deprecating billable: a utility message sent free inside a window is told apart by its type."""
    for kind in ("free_customer_service", "free_entry_point"):
        assert whatsapp.priced_cost({"meta_category": "utility", "pricing_type": kind}) == 0
    assert whatsapp.priced_cost({"meta_category": "utility", "pricing_type": "regular"}) == \
        settings.whatsapp_utility_cost_inr
    assert whatsapp.priced_cost({"meta_category": "marketing"}) == settings.whatsapp_marketing_cost_inr


def test_an_in_window_check_never_relabels_an_older_queued_template(owners, ids, monkeypatch):
    """Only the outbox row written for the new notification becomes in_window; when none is written, an older
    queued template of the same owner (still retrying) stays a template."""
    db = Database()
    monkeypatch.setattr(settings, "stock_check_weekly_day", weekday_name(1))
    whatsapp.send_stock_checks(db, ist(1, 9, 5))  # no window: the weekly template, left queued
    [older] = outbox(db, owners["a"].id)
    assert (older["route"], older["status"]) == ("template", "queued")

    message_from(db, owners["a"].phone, ist(1, 20))
    from app.services import notifications
    monkeypatch.setattr(notifications, "OUTBOX_CHANNELS", ())  # this time no outbox row is written
    whatsapp.send_stock_checks(db, ist(2, 9, 5))
    assert check_row(db, ids["shop_a"], 2)["route"] == "in_window"
    [row] = outbox(db, owners["a"].id)
    assert (row["_id"], row["route"], row["category"]) == (older["_id"], "template", "utility")
