"""WhatsApp replies from shops (P4.4): the Confirm and Decline buttons, stock updates with a preview and Yes, ambiguous
lines, unknown phones, replayed message ids, the service window and the daily stock check. Every call goes through
the signed webhook without the CSRF header, and every reply through the fake client; nothing reaches Meta."""
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import jobs
from app.core.config import settings
from app.core.database import Database, utcnow
from app.models import InventoryReason, Product
from app.services import whatsapp
from app.services.privacy import set_consent
from tests.conftest import HEADERS

APP_SECRET = "test-app-secret-" + "y" * 16
OWNER_PHONE = "9000054321"
OWNER_WA = "91" + OWNER_PHONE
STRANGER_WA = "919111122222"
SUPPORT_PHONE = "08012345678"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def whatsapp_on(ids, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    monkeypatch.setattr(settings, "whatsapp_app_secret", type(settings.whatsapp_app_secret)(APP_SECRET))
    db = Database()
    collections = (whatsapp.OUTBOX, whatsapp.INBOUND, whatsapp.SESSIONS, whatsapp.STOCK_CHECKS)
    for name in collections:
        db.mongo[name].delete_many({})
    whatsapp.FAKE.reset()
    yield
    for name in collections:
        db.mongo[name].delete_many({})
    db.mongo[jobs.LEASES].delete_many({"_id": jobs.STOCK_CHECK_JOB})
    whatsapp.FAKE.reset()


@pytest.fixture
def shop_owner(ids):
    """Owner A with a verified phone and the WhatsApp consent; Shop A's stock and the owner put back afterwards."""
    db = Database()
    owner = db.users.find_one({"email": "a@example.com"})
    saved = {"phone": owner.phone, "phone_verified_at": owner.phone_verified_at}
    db.users.update_one({"_id": owner.id}, {"$set": {"phone": OWNER_PHONE, "phone_verified_at": utcnow()}})
    before_consents = {r["_id"] for r in db.mongo["consents"].find({"user_id": owner.id}, {"_id": 1})}
    set_consent(db, owner.id, "whatsapp", True)
    stock = {p.id: (p.quantity, p.stock_updated_at) for p in db.products.find({"shop_id": ids["shop_a"]})}
    yield owner
    db.mongo["consents"].delete_many({"user_id": owner.id, "_id": {"$nin": list(before_consents)}})
    db.users.update_one({"_id": owner.id}, {"$set": saved})
    for product_id, (quantity, updated) in stock.items():
        db.products.update_one({"_id": product_id}, {"$set": {"quantity": quantity, "stock_updated_at": updated}})


@pytest.fixture
def tapes(ids, shop_owner):
    """Four more tapes in Shop A, so a bare "tape" line is ambiguous with more candidates than are offered."""
    db = Database()
    shop = db.shops.get(ids["shop_a"])
    names = ["Insulation Tape Black", "Double Sided Foam Tape", "Masking Tape 24 mm", "Duct Tape Silver"]
    made = db.products.insert_many([Product(shop_id=shop.id, category_id=shop.category_ids[0], name=n, keywords="tape",
                                            price=50, quantity=4) for n in names])
    yield made
    db.products.delete_many({"_id": {"$in": [p.id for p in made]}})


def signed(body: bytes) -> dict[str, str]:
    """The headers Meta sends: the signature, and no X-NearShop-Client (the webhook is outside the CSRF guard)."""
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def message(kind: str, content: dict, sender: str = OWNER_WA, message_id: str | None = None,
            timestamp: int | None = None) -> dict:
    return {"from": sender, "id": message_id or f"wamid.in.{uuid.uuid4().hex}",
            "timestamp": str(timestamp if timestamp is not None else int(time.time())), "type": kind, kind: content}


def text(body: str, **kwargs) -> dict:
    return message("text", {"body": body}, **kwargs)


def button(payload: str, label: str = "Confirm", **kwargs) -> dict:
    return message("button", {"payload": payload, "text": label}, **kwargs)


def post(client, *messages: dict):
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
        "field": "messages", "value": {"messaging_product": "whatsapp",
                                       "contacts": [{"wa_id": m["from"]} for m in messages],
                                       "messages": list(messages)}}]}]}).encode()
    res = client.post("/api/webhooks/whatsapp", content=body, headers=signed(body))
    assert res.status_code == 200, res.text
    return res.json()


def replies(to: str = OWNER_WA) -> list[str]:
    return [m["text"] for m in whatsapp.FAKE.sent if m.get("template") is None and m["phone"] == to]


def tape_events(db, product_id: int, since: datetime) -> list[dict]:
    """The listing's stock events written by WhatsApp updates since a time."""
    return list(db.inventory_events.raw.find({"product_id": product_id, "created_at": {"$gte": since},
                                              "reason": "whatsapp_update"}, sort=[("_id", 1)]))


# ---------------------------------------------------------------- Confirm and Decline


def test_the_confirm_button_moves_the_reservation_to_confirmed_with_stock_held(shop_owner, customer, client, ids):
    db = Database()
    before = db.products.get(ids["tape"]).quantity
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 2}, headers=HEADERS).json()
    try:
        result = post(client, button(f"reservation:{r['code']}:confirm"))
        assert result["messages"]["handled"] == 1
        res = db.reservations.get(r["id"])
        assert res.status == "CONFIRMED" and res.stock_held is True
        assert db.products.get(ids["tape"]).quantity == before - 2
        event = db.status_events.raw.find_one({"entity": "reservation", "entity_id": r["id"], "to_status": "CONFIRMED"})
        assert event["actor_role"] == "owner" and event["actor_id"] == shop_owner.id
        assert event["note"] == whatsapp.VIA_WHATSAPP
        hold = db.inventory_events.raw.find_one({"product_id": ids["tape"], "reason": "reservation_hold"},
                                                sort=[("_id", -1)])
        assert hold["delta"] == -2 and hold["actor_id"] == shop_owner.id
        [reply] = replies()
        assert reply.startswith(f"Confirmed reservation {r['code']}")
        # Tapping Confirm again (a new message) changes nothing and says why.
        post(client, button(f"reservation:{r['code']}:confirm"))
        assert db.products.get(ids["tape"]).quantity == before - 2
        assert "was not confirmed" in replies()[-1]
    finally:
        customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)
    assert db.products.get(ids["tape"]).quantity == before


def test_the_decline_button_rejects_the_reservation_without_touching_stock(shop_owner, customer, client, ids):
    db = Database()
    before = db.products.get(ids["tape"]).quantity
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    result = post(client, button(f"reservation:{r['code']}:decline", label="Decline"))
    assert result["messages"]["handled"] == 1
    res = db.reservations.get(r["id"])
    assert res.status == "REJECTED" and res.stock_held is False and res.close_reason is None
    assert db.products.get(ids["tape"]).quantity == before
    event = db.status_events.raw.find_one({"entity": "reservation", "entity_id": r["id"], "to_status": "REJECTED"})
    assert event["note"] == whatsapp.VIA_WHATSAPP and event["actor_role"] == "owner"
    # The customer is told in the usual words, not "via WhatsApp".
    note = db.notifications.find_one({"user_id": res.customer_id, "kind": "reservation_rejected"}, sort=[("_id", -1)])
    assert "WhatsApp" not in (note.body or "")
    assert replies()[-1].startswith(f"Declined reservation {r['code']}")


def test_a_button_for_another_shops_reservation_changes_nothing(shop_owner, client, ids):
    db = Database()
    customer = db.users.find_one({"email": "other@example.com"})
    from app.services.reservations import create_reservation

    res = create_reservation(db, customer, ids["tape_far"], 1, None)  # Shop B's
    try:
        post(client, button(f"reservation:{res.code}:confirm"))
        assert db.reservations.get(res.id).status == "REQUESTED"
        assert "could not find that reservation" in replies()[-1]
    finally:
        db.reservations.update_one({"_id": res.id}, {"$set": {"status": "CANCELLED"}})


def test_a_request_past_its_time_is_expired_not_confirmed(shop_owner, customer, client, ids):
    db = Database()
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    db.reservations.update_one({"_id": r["id"]}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})
    post(client, button(f"reservation:{r['code']}:confirm"))
    assert db.reservations.get(r["id"]).status == "EXPIRED"
    assert "was not confirmed" in replies()[-1]


# ---------------------------------------------------------------- stock updates


def test_a_stock_message_previews_then_yes_writes_whatsapp_update_events(shop_owner, client, ids):
    db = Database()
    start = utcnow() - timedelta(seconds=1)
    tape, charger = db.products.get(ids["tape"]), db.products.get(ids["charger"])
    post(client, text("Astral PTFE thread seal tape 45\nSamsung 25W charger: 7"))
    preview = replies()[-1]
    assert f"- {tape.name}: 45 (now {tape.quantity})" in preview
    assert f"- {charger.name}: 7 (now {charger.quantity})" in preview and "Reply Yes" in preview
    # The preview changes nothing.
    assert db.products.get(ids["tape"]).quantity == tape.quantity and tape_events(db, tape.id, start) == []

    post(client, text("Yes"))
    assert db.products.get(ids["tape"]).quantity == 45 and db.products.get(ids["charger"]).quantity == 7
    [event] = tape_events(db, tape.id, start)
    assert event["reason"] == InventoryReason.WHATSAPP_UPDATE == "whatsapp_update"
    assert event["quantity_after"] == 45 and event["delta"] == 45 - tape.quantity
    assert event["actor_id"] == shop_owner.id
    [charger_event] = tape_events(db, charger.id, start)
    assert charger_event["reason"] == "whatsapp_update" and charger_event["quantity_after"] == 7
    assert db.shops.get(ids["shop_a"]).inventory_updated_at >= start
    assert replies()[-1].startswith("Saved.")
    # A second Yes finds nothing waiting: the counts are applied once.
    post(client, text("yes"))
    assert len(tape_events(db, tape.id, start)) == 1 and "no stock update waiting" in replies()[-1]


def test_no_cancels_the_preview(shop_owner, client, ids):
    db = Database()
    before = db.products.get(ids["tape"]).quantity
    post(client, text("Astral PTFE thread seal tape 12"))
    post(client, text("No"))
    assert "Cancelled" in replies()[-1]
    post(client, text("Yes"))
    assert db.products.get(ids["tape"]).quantity == before


def test_an_ambiguous_line_offers_up_to_three_choices(shop_owner, tapes, client, ids):
    db = Database()
    start = utcnow() - timedelta(seconds=1)
    post(client, text("tape 20"))
    offer = replies()[-1]
    assert 'Which item is "tape" (20)?' in offer
    options = [line for line in offer.splitlines() if line[:2] in ("1.", "2.", "3.", "4.", "5.")]
    assert [o[:2] for o in options] == ["1.", "2.", "3."]  # five tapes in the shop, three offered
    session = db.mongo[whatsapp.SESSIONS].find_one({"_id": shop_owner.id})
    candidates = session["pending"]["queue"][0]["candidates"]
    assert len(candidates) == 3

    post(client, text("9"))  # not one of the choices
    assert "number from 1 to 3" in replies()[-1]
    post(client, text("Yes"))  # a choice is still open
    assert replies()[-1].startswith("Please choose the item first.")
    post(client, text("2"))
    chosen = candidates[1]
    assert f"- {chosen['name']}: 20" in replies()[-1] and "Reply Yes" in replies()[-1]
    post(client, text("ok"))
    assert db.products.get(chosen["product_id"]).quantity == 20
    [event] = tape_events(db, chosen["product_id"], start)
    assert event["reason"] == "whatsapp_update" and event["quantity_after"] == 20


def test_unmatched_and_unreadable_lines_are_left_out(shop_owner, client, ids):
    post(client, text("Astral PTFE thread seal tape 30\nGarden hose pipe 5\njust some words"))
    preview = replies()[-1]
    assert "Garden hose pipe" in preview and "just some words (no count)" in preview
    assert "Astral PTFE Thread Seal Tape: 30" in preview


def test_a_message_that_is_not_a_stock_update_gets_the_help_text(shop_owner, client):
    post(client, text("hello"))
    assert replies()[-1] == whatsapp.HELP_TEXT
    post(client, message("image", {"id": "media-1"}))
    assert replies()[-1] == whatsapp.HELP_TEXT


def test_stock_lines_are_read_either_way_round():
    lines, unread = whatsapp.parse_stock_lines(
        "Tape 20\nBox: 3\n4 x Samsung 25W charger\nNotebook - 10 pcs\n\nno count here\n12\nbig 9999999")
    assert lines == [("Tape", 20), ("Box", 3), ("Samsung 25W charger", 4), ("Notebook", 10)]
    assert unread == ["no count here", "12", "big 9999999"]


# ---------------------------------------------------------------- phones, replays and the service window


def test_an_unknown_phone_gets_the_support_reply(shop_owner, client, monkeypatch):
    monkeypatch.setattr(settings, "grievance_officer_phone", SUPPORT_PHONE)
    result = post(client, text("Tape 20", sender=STRANGER_WA))
    assert result["messages"]["handled"] == 1
    [reply] = replies(STRANGER_WA)
    assert reply == whatsapp.support_reply() and SUPPORT_PHONE in reply
    assert Database().mongo[whatsapp.SESSIONS].count_documents({}) == 0  # no session for a stranger


def test_an_unverified_owner_phone_is_unknown(shop_owner, client):
    Database().users.update_one({"_id": shop_owner.id}, {"$set": {"phone_verified_at": None}})
    post(client, text("Tape 20"))
    assert replies()[-1] == whatsapp.support_reply()


def test_a_replayed_message_id_is_ignored(shop_owner, customer, client, ids):
    db = Database()
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    try:
        tap = button(f"reservation:{r['code']}:confirm", message_id="wamid.replayed.1")
        assert post(client, tap)["messages"]["handled"] == 1
        assert post(client, tap)["messages"] == {"handled": 0, "duplicate": 1, "ignored": 0}
        assert len(replies()) == 1
        assert db.status_events.count({"entity": "reservation", "entity_id": r["id"]}) == 2  # requested, confirmed

        start = utcnow() - timedelta(seconds=1)
        post(client, text("Astral PTFE thread seal tape 33"))
        yes = text("Yes", message_id="wamid.replayed.2")
        post(client, yes)
        post(client, yes)
        assert len(tape_events(db, ids["tape"], start)) == 1
        row = db.mongo[whatsapp.INBOUND].find_one({"_id": "wamid.replayed.2"})
        assert row["outcome"] == "saved" and row["reply"] == "sent" and "from" not in row
    finally:
        customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)


def test_a_failure_while_acting_releases_the_message_for_metas_retry(shop_owner, ids, monkeypatch):
    db = Database()
    tap = text("Astral PTFE thread seal tape 5", message_id="wamid.retry.1")

    def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    original = whatsapp.answer_text
    monkeypatch.setattr(whatsapp, "answer_text", broken)
    with pytest.raises(RuntimeError):
        whatsapp.handle_message(db, tap, whatsapp.FAKE, utcnow())
    assert db.mongo[whatsapp.INBOUND].count_documents({"_id": "wamid.retry.1"}) == 0
    monkeypatch.setattr(whatsapp, "answer_text", original)
    assert whatsapp.handle_message(db, tap, whatsapp.FAKE, utcnow()) == "handled"


def test_no_reply_is_sent_outside_the_service_window(shop_owner, client):
    old = int(time.time()) - 25 * 3600  # Meta retried a call more than a day later
    post(client, text("hello", sender=STRANGER_WA, message_id="wamid.late.1", timestamp=old))
    assert replies(STRANGER_WA) == []
    assert Database().mongo[whatsapp.INBOUND].find_one({"_id": "wamid.late.1"})["reply"] == "window_closed"


def test_the_cloud_client_sends_a_free_form_text_reply(monkeypatch):
    from app.services import messaging

    calls = []
    monkeypatch.setattr(messaging, "transport", lambda url, payload, headers: (
        calls.append((url, payload, headers)) or (200, {"messages": [{"id": "wamid.reply.1"}]})))
    monkeypatch.setattr(settings, "whatsapp_access_token", type(settings.whatsapp_access_token)("wa-token"))
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "1234")
    delivery = whatsapp.CloudClient(settings).send_text(OWNER_WA, "Saved.")
    assert delivery.ok and delivery.cost_estimate == 0
    url, payload, headers = calls[-1]
    assert url.endswith("/1234/messages") and headers["Authorization"] == "Bearer wa-token"
    assert payload["type"] == "text" and payload["to"] == OWNER_WA and payload["text"]["body"] == "Saved."


def test_messages_are_ignored_while_whatsapp_is_off(shop_owner, client, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_notifications", False)
    assert post(client, text("Tape 20"))["messages"]["ignored"] == 1
    assert replies() == []


# ---------------------------------------------------------------- the daily stock check


def ist(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A naive UTC time at hour:minute IST, day_offset days from today (IST)."""
    today = datetime.now(IST).date() + timedelta(days=day_offset)
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def test_opening_times_are_read_from_the_opening_hours():
    assert whatsapp.opening_time("9:30 AM - 9:00 PM") == datetime.strptime("09:30", "%H:%M").time()
    assert whatsapp.opening_time("10am-8pm").hour == 10
    assert whatsapp.opening_time("12 pm to 9 pm").hour == 12
    assert whatsapp.opening_time("07:45-21:00").minute == 45
    assert whatsapp.opening_time(None) == whatsapp.DEFAULT_OPENING == whatsapp.opening_time("all day")


def test_the_stock_check_goes_at_opening_time_with_stale_listings(shop_owner, ids):
    db = Database()
    now = ist(1, 10)  # tomorrow 10:00 IST: Shop A opens at the default 9:00
    db.shops.update_one({"_id": ids["shop_a"]}, {"$set": {"opening_hours": None}})
    db.products.update_one({"_id": ids["tape"]}, {"$set": {"stock_updated_at": now - timedelta(days=3)}})
    db.products.update_one({"_id": ids["charger"]}, {"$set": {"stock_updated_at": now - timedelta(hours=1)}})

    assert whatsapp.send_stock_checks(db, ist(1, 8, 59))["sent"] == 0  # before opening
    result = whatsapp.send_stock_checks(db, now)
    assert result["enabled"] and result["sent"] >= 1
    row = db.mongo[whatsapp.STOCK_CHECKS].find_one({"shop_id": ids["shop_a"]})
    assert row["outcome"] == "sent" and row["day"] == (datetime.now(IST).date() + timedelta(days=1)).isoformat()
    note = db.notifications.find_one({"user_id": shop_owner.id, "kind": "stock_check"}, sort=[("_id", -1)])
    tape = db.products.get(ids["tape"])
    assert f"{tape.name}: {tape.quantity} (counted 3 days ago)" in note.body and "Charger" not in note.body
    [queued] = list(db.mongo[whatsapp.OUTBOX].find({"kind": "stock_check"}))
    assert queued["template"] == "nearshop_shop_stock_check" and queued["user_id"] == shop_owner.id
    # Once a day: a later tick sends nothing more.
    assert whatsapp.send_stock_checks(db, now + timedelta(minutes=30))["sent"] == 0
    assert db.mongo[whatsapp.OUTBOX].count_documents({"kind": "stock_check"}) == 1
    sent = whatsapp.send_due(db, client=whatsapp.FAKE)  # the notification is dated by the real clock
    assert sent["sent"] == 1 and whatsapp.FAKE.sent[-1]["template"] == "nearshop_shop_stock_check"
    assert whatsapp.FAKE.sent[-1]["phone"] == OWNER_PHONE


def test_the_stock_check_is_skipped_late_fresh_or_unreachable(shop_owner, ids):
    db = Database()
    db.shops.update_one({"_id": ids["shop_a"]}, {"$set": {"opening_hours": "10:00 AM - 8:00 PM"}})
    try:
        db.products.update_many({"shop_id": ids["shop_a"]}, {"$set": {"stock_updated_at": ist(1, 9)}})
        assert whatsapp.send_stock_checks(db, ist(1, 14))["sent"] == 0  # more than 3 hours after opening
        assert db.mongo[whatsapp.STOCK_CHECKS].count_documents({"shop_id": ids["shop_a"]}) == 0
        whatsapp.send_stock_checks(db, ist(1, 10, 30))  # everything counted this morning
        assert db.mongo[whatsapp.STOCK_CHECKS].find_one({"shop_id": ids["shop_a"]})["outcome"] == "fresh"

        db.products.update_many({"shop_id": ids["shop_a"]}, {"$set": {"stock_updated_at": ist(-2, 9)}})
        set_consent(db, shop_owner.id, "whatsapp", False)
        whatsapp.send_stock_checks(db, ist(2, 10, 30))
        rows = list(db.mongo[whatsapp.STOCK_CHECKS].find({"shop_id": ids["shop_a"]}, sort=[("day", 1)]))
        assert [r["outcome"] for r in rows] == ["fresh", "unreachable"]
        assert db.mongo[whatsapp.OUTBOX].count_documents({}) == 0
    finally:
        db.shops.update_one({"_id": ids["shop_a"]}, {"$set": {"opening_hours": None}})


def test_the_stock_check_job_runs_under_its_lease_and_does_nothing_while_whatsapp_is_off(monkeypatch):
    assert jobs.JOBS[jobs.STOCK_CHECK_JOB] is jobs.stock_check
    assert jobs.RETRY_AFTER_FAILURE[jobs.STOCK_CHECK_JOB] == timedelta(minutes=15)
    monkeypatch.setattr(settings, "whatsapp_notifications", False)
    assert jobs.stock_check() == {"enabled": False}
