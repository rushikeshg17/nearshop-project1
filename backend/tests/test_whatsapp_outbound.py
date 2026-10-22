"""Outbound WhatsApp notifications (P4.3): the outbox written in the notification's transaction, the sender's retries,
opt-outs, the status webhook (signature, no CSRF header, duplicates) and the hourly failure-rate event. Every send
goes through the fake client; nothing reaches Meta."""
import hashlib
import hmac
import json
from datetime import UTC, timedelta

import pytest
import sentry_sdk

from app import jobs
from app.core import observability
from app.core.config import settings
from app.core.database import Database, utcnow
from app.services import whatsapp
from app.services.notifications import notify
from app.services.privacy import set_consent
from tests.conftest import HEADERS
from tests.test_observability import FAKE_DSN, FakeTransport

OUTBOX = whatsapp.OUTBOX
APP_SECRET = "test-app-secret-" + "x" * 16
VERIFY_TOKEN = "test-verify-token"
CUSTOMER_PHONE, OWNER_PHONE = "9876543210", "9000012345"


class Clock:
    def __init__(self) -> None:
        self.now = utcnow() + timedelta(seconds=1)

    def __call__(self):
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def people(ids):
    """The seeded customer and Owner A with verified phones and the WhatsApp consent granted; put back afterwards."""
    db = Database()
    users = {u.email: u for u in db.users.find({"email": {"$in": ["cust@example.com", "a@example.com"]}})}
    customer, owner = users["cust@example.com"], users["a@example.com"]
    saved = {u.id: {"phone": u.phone, "phone_verified_at": u.phone_verified_at} for u in (customer, owner)}
    db.users.update_one({"_id": customer.id}, {"$set": {"phone": CUSTOMER_PHONE, "phone_verified_at": utcnow()}})
    db.users.update_one({"_id": owner.id}, {"$set": {"phone": OWNER_PHONE, "phone_verified_at": utcnow()}})
    before = {r["_id"] for r in db.mongo["consents"].find({}, {"_id": 1})}
    for user in (customer, owner):
        set_consent(db, user.id, "whatsapp", True)
    yield {"customer": customer.id, "owner": owner.id}
    db.mongo["consents"].delete_many({"_id": {"$nin": list(before)}, "user_id": {"$in": [customer.id, owner.id]}})
    for user_id, fields in saved.items():
        db.users.update_one({"_id": user_id}, {"$set": fields})


@pytest.fixture(autouse=True)
def outbox(ids, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    monkeypatch.setattr(settings, "whatsapp_app_secret", type(settings.whatsapp_app_secret)(APP_SECRET))
    monkeypatch.setattr(settings, "whatsapp_webhook_verify_token",
                        type(settings.whatsapp_webhook_verify_token)(VERIFY_TOKEN))
    db = Database()
    db.mongo[OUTBOX].delete_many({})
    whatsapp.FAKE.reset()
    yield db.mongo[OUTBOX]
    db.mongo[OUTBOX].delete_many({})
    db.mongo[jobs.LEASES].delete_many({"_id": jobs.SEND_NOTIFICATIONS_JOB})
    whatsapp.FAKE.reset()


def rows(outbox, **filter):
    return list(outbox.find(filter, sort=[("_id", 1)]))


# ---------------------------------------------------------------- the outbox and its transaction


def test_the_outbox_row_is_written_in_the_notifications_transaction(people, outbox):
    db = Database()
    seen_outside: list[int] = []

    def work():
        notify(db, people["customer"], "reservation_confirmed", "Shop A confirmed your reservation",
               "1 x Tape is held for you for 30 minutes.\nCode R-ABC123.", "/account/reservations/7")
        seen_outside.append(Database().mongo[OUTBOX].count_documents({}))  # not committed yet: invisible outside

    db.transaction(work)
    assert seen_outside == [0]
    [row] = rows(outbox)
    note = db.notifications.get(row["notification_id"])
    assert note is not None and note.kind == "reservation_confirmed"
    assert row["status"] == "queued" and row["attempts"] == 0 and row["template"] == "nearshop_reservation_confirmed"
    assert row["created_at"] == note.created_at == row["next_attempt_at"]
    assert row["params"] == ["Shop A confirmed your reservation", "1 x Tape is held for you for 30 minutes. Code R-ABC123."]
    assert "phone" not in row and row["subject"] == "reservation:7"  # from the link: the title has no code


def test_a_rolled_back_transaction_leaves_no_outbox_row(people, outbox):
    db = Database()

    def work():
        notify(db, people["customer"], "reservation_ready", "Ready for pickup rollback", "Show code R-ROLL01.")
        raise RuntimeError("the reservation failed after notify")

    with pytest.raises(RuntimeError):
        db.transaction(work)
    assert rows(outbox) == []
    assert db.notifications.count({"title": "Ready for pickup rollback"}) == 0


def test_a_new_reservation_reaches_the_shop_with_confirm_and_decline_buttons(people, outbox, customer, owner_a, ids):
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    try:
        [row] = rows(outbox, user_id=people["owner"])
        assert row["kind"] == "reservation_new" and row["template"] == "nearshop_shop_new_reservation"
        assert row["buttons"] == [f"reservation:{r['code']}:confirm", f"reservation:{r['code']}:decline"]
        owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
        [confirmed] = rows(outbox, user_id=people["customer"])
        assert confirmed["template"] == "nearshop_reservation_confirmed"

        result = whatsapp.send_due(Database(), client=whatsapp.FAKE)
        assert result["sent"] == 2
        to_shop = next(m for m in whatsapp.FAKE.sent if m["template"] == "nearshop_shop_new_reservation")
        assert to_shop["phone"] == OWNER_PHONE
        buttons = [c for c in to_shop["components"] if c["type"] == "button"]
        assert [b["sub_type"] for b in buttons] == ["quick_reply", "quick_reply"]
        assert buttons[0]["parameters"][0]["payload"] == f"reservation:{r['code']}:confirm"
        sent = rows(outbox, status="sent")
        assert len(sent) == 2 and all(s["message_id"] and s["sent_at"] and s["params"] is None for s in sent)
        assert all(s["seconds_to_sent"] >= 0 for s in sent)
    finally:
        customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)
    # The owner's "cancelled by customer" notification has no template: in the app only.
    assert rows(outbox, kind="reservation_cancelled") == []


def test_kinds_without_a_template_and_the_wrong_recipient_role_stay_in_the_app(people, outbox):
    db = Database()
    db.transaction(lambda: notify(db, people["customer"], "reservation_completed", "Pickup complete"))
    db.transaction(lambda: notify(db, people["owner"], "reservation_expired", "Reservation R-OWNER1 expired"))
    db.transaction(lambda: notify(db, people["owner"], "order_cancelled", "Order D-OWNER1 cancelled by customer"))
    assert rows(outbox) == []
    db.transaction(lambda: notify(db, people["customer"], "order_cancelled", "Order cancelled", "From Shop A."))
    assert [r["template"] for r in rows(outbox)] == ["nearshop_order_status"]


def test_nothing_is_written_while_whatsapp_notifications_are_off(people, outbox, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_notifications", False)
    db = Database()
    db.transaction(lambda: notify(db, people["customer"], "reservation_ready", "Ready for pickup", "Code R-OFF001."))
    assert rows(outbox) == [] and whatsapp.get_client() is None


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_without_the_cloud_api_configured_deployed_environments_send_nothing(monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "whatsapp_access_token", type(settings.whatsapp_access_token)(""))
    assert whatsapp.get_client() is None
    monkeypatch.setattr(settings, "whatsapp_access_token", type(settings.whatsapp_access_token)("token"))
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123")
    assert isinstance(whatsapp.get_client(), whatsapp.CloudClient)


def test_the_test_environment_never_uses_the_cloud_api(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_access_token", type(settings.whatsapp_access_token)("token"))
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123")
    assert whatsapp.get_client() is whatsapp.FAKE


def test_the_cloud_client_sends_the_template_with_the_token_from_settings(monkeypatch):
    from app.services import messaging

    calls = []
    monkeypatch.setattr(messaging, "transport", lambda url, payload, headers: (
        calls.append((url, payload, headers)) or (200, {"messages": [{"id": "wamid.cloud.1"}]})))
    monkeypatch.setattr(settings, "whatsapp_access_token", type(settings.whatsapp_access_token)("wa-token"))
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "1234")
    row = {"params": ["New reservation R-ABC123", "Asha wants 1 x Tape."], "buttons": ["reservation:R-ABC123:confirm"]}
    delivery = whatsapp.CloudClient(settings).send_template(OWNER_PHONE, "nearshop_shop_new_reservation", "en",
                                                            whatsapp.components(row))
    assert delivery.ok and delivery.message_id == "wamid.cloud.1"
    assert delivery.cost_estimate == settings.whatsapp_utility_cost_inr
    url, payload, headers = calls[-1]
    assert url.endswith("/1234/messages") and headers["Authorization"] == "Bearer wa-token"
    assert payload["to"] == "91" + OWNER_PHONE and payload["type"] == "template"
    template = payload["template"]
    assert template["name"] == "nearshop_shop_new_reservation" and template["language"] == {"code": "en"}
    body, button = template["components"]
    assert [p["text"] for p in body["parameters"]] == row["params"]
    assert button == {"type": "button", "sub_type": "quick_reply", "index": "0",
                      "parameters": [{"type": "payload", "payload": "reservation:R-ABC123:confirm"}]}
    monkeypatch.setattr(messaging, "transport", lambda url, payload, headers: (200, {"error": {"code": 131030}}))
    refused = whatsapp.CloudClient(settings).send_template(OWNER_PHONE, "t", "en", whatsapp.components(row))
    assert not refused.ok and refused.error == "rejected"


# ---------------------------------------------------------------- opt-outs


def test_an_opt_out_is_respected(people, outbox, customer):
    assert customer.delete("/api/auth/consents/whatsapp", headers=HEADERS).status_code == 200
    db = Database()
    db.transaction(lambda: notify(db, people["customer"], "reservation_ready", "Ready for pickup", "Code R-OPT001."))
    assert rows(outbox) == []


def test_an_opt_out_after_the_notification_cancels_the_send(people, outbox):
    db = Database()
    db.transaction(lambda: notify(db, people["customer"], "reservation_ready", "Ready for pickup", "Code R-OPT002."))
    set_consent(db, people["customer"], "whatsapp", False)
    assert whatsapp.send_due(db, client=whatsapp.FAKE)["cancelled"] == 1
    [row] = rows(outbox)
    assert row["status"] == "cancelled" and row["error"] == "opted_out" and row["params"] is None
    assert list(whatsapp.FAKE.sent) == []


def test_an_unverified_phone_gets_no_message(people, outbox):
    db = Database()
    db.users.update_one({"_id": people["customer"]}, {"$set": {"phone_verified_at": None}})
    db.transaction(lambda: notify(db, people["customer"], "reservation_ready", "Ready for pickup", "Code R-UNV001."))
    assert rows(outbox) == []


# ---------------------------------------------------------------- the sender's retries


def queue_one(people) -> None:
    db = Database()
    db.transaction(lambda: notify(db, people["customer"], "order_preparing", "Packing your order",
                                  "Shop A is preparing your order.", "/account/orders/9"))


def test_the_sender_retries_after_30s_2min_and_10min_then_marks_the_row_failed(people, outbox):
    queue_one(people)
    clock, db = Clock(), Database()
    whatsapp.FAKE.failures = 4
    expected_waits = [timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10)]
    for attempt, wait in enumerate(expected_waits, start=1):
        assert whatsapp.send_due(db, client=whatsapp.FAKE, clock=clock)["retrying"] == 1
        [row] = rows(outbox)
        assert row["status"] == "queued" and row["attempts"] == attempt and row["error"] == "http_503"
        assert row["next_attempt_at"] == clock.now + wait
        clock.advance(seconds=wait.total_seconds() - 1)
        assert whatsapp.send_due(db, client=whatsapp.FAKE, clock=clock)["retrying"] == 0  # not due yet
        clock.advance(seconds=1)
    assert whatsapp.send_due(db, client=whatsapp.FAKE, clock=clock)["failed"] == 1
    [row] = rows(outbox)
    assert row["status"] == "failed" and row["attempts"] == 4 and row["failed_at"] is not None
    assert row["params"] is None
    clock.advance(hours=1)
    assert whatsapp.send_due(db, client=whatsapp.FAKE, clock=clock) == {
        "enabled": True, "sent": 0, "retrying": 0, "failed": 0, "cancelled": 0}  # never sent again


def test_a_retry_that_succeeds_records_the_time_to_sent(people, outbox):
    queue_one(people)
    clock, db = Clock(), Database()
    whatsapp.FAKE.failures = 1
    whatsapp.send_due(db, client=whatsapp.FAKE, clock=clock)
    clock.advance(seconds=30)
    assert whatsapp.send_due(db, client=whatsapp.FAKE, clock=clock)["sent"] == 1
    [row] = rows(outbox)
    assert row["status"] == "sent" and row["attempts"] == 2 and row["error"] is None
    assert row["seconds_to_sent"] == pytest.approx((row["sent_at"] - row["created_at"]).total_seconds(), abs=0.01)
    assert 30 <= row["seconds_to_sent"] < 40


def test_a_client_that_raises_never_stops_the_run(people, outbox):
    queue_one(people)
    queue_one(people)

    class Broken:
        def send_template(self, *args):
            raise TimeoutError

    assert whatsapp.send_due(Database(), client=Broken())["retrying"] == 2


def test_a_send_interrupted_mid_way_counts_as_a_failed_attempt(people, outbox):
    queue_one(people)
    clock, db = Clock(), Database()
    outbox.update_one({}, {"$set": {"status": "sending", "claimed_at": clock.now}})
    clock.advance(minutes=3)
    assert whatsapp.send_due(db, client=whatsapp.FAKE, clock=clock)["retrying"] == 1
    assert rows(outbox)[0]["attempts"] == 1


def test_a_row_too_old_for_its_first_send_is_cancelled(people, outbox):
    queue_one(people)
    clock = Clock()
    clock.advance(minutes=31)
    assert whatsapp.send_due(Database(), client=whatsapp.FAKE, clock=clock)["cancelled"] == 1
    assert rows(outbox)[0]["error"] == "stale" and list(whatsapp.FAKE.sent) == []


def test_the_scheduler_runs_the_sender_under_its_lease(people, outbox):
    queue_one(people)
    db = Database()
    assert jobs.JOBS[jobs.SEND_NOTIFICATIONS_JOB] is jobs.send_notifications
    assert jobs.run_leased(db.mongo, jobs.SEND_NOTIFICATIONS_JOB, "holder-a", jobs.send_notifications)
    assert not jobs.run_leased(db.mongo, jobs.SEND_NOTIFICATIONS_JOB, "holder-b", jobs.send_notifications)
    result = db.mongo[jobs.LEASES].find_one({"_id": jobs.SEND_NOTIFICATIONS_JOB})["last_result"]
    assert result["ok"] is True and result["sent"] == 1 and result["alerted"] is False


# ---------------------------------------------------------------- the webhook


def signed(body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def callback(message_id: str, status: str, timestamp: int | None = None, **extra) -> bytes:
    entry = {"id": message_id, "status": status, "recipient_id": "91" + CUSTOMER_PHONE, **extra}
    if timestamp is not None:
        entry["timestamp"] = str(timestamp)
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [
        {"field": "messages", "value": {"messaging_product": "whatsapp", "statuses": [entry]}}]}]}).encode()


def sent_row(people, outbox) -> dict:
    queue_one(people)
    whatsapp.send_due(Database(), client=whatsapp.FAKE)
    [row] = rows(outbox)
    assert row["status"] == "sent"
    return row


def test_the_verification_challenge(client):
    ok = client.get("/api/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                                                      "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    wrong = client.get("/api/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                         "hub.challenge": "1"})
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "verification_failed"


def test_the_verification_challenge_is_refused_without_a_configured_token(client, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_webhook_verify_token",
                        type(settings.whatsapp_webhook_verify_token)(""))
    res = client.get("/api/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "",
                                                       "hub.challenge": "1"})
    assert res.status_code == 403


def test_a_bad_signature_is_rejected(people, outbox, client):
    row = sent_row(people, outbox)
    body = callback(row["message_id"], "delivered")
    for headers in ({}, signed(body, secret="another-secret"), {"X-Hub-Signature-256": "sha1=abc"},
                    signed(body + b" ")):
        res = client.post("/api/webhooks/whatsapp", content=body, headers=headers)
        assert res.status_code == 403 and res.json()["error"]["code"] == "bad_signature"
    assert rows(outbox)[0].get("delivered_at") is None


def test_without_an_app_secret_every_call_is_rejected(people, outbox, client, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", type(settings.whatsapp_app_secret)(""))
    body = callback("wamid.x", "delivered")
    assert client.post("/api/webhooks/whatsapp", content=body, headers=signed(body, secret="")).status_code == 403


def test_the_webhook_works_without_the_csrf_header(people, outbox, client):
    row = sent_row(people, outbox)
    created = row["created_at"] - timedelta(minutes=1)  # the notification a minute ago, delivered 42 s after it
    outbox.update_one({"_id": row["_id"]}, {"$set": {"created_at": created}})
    delivered_at = created + timedelta(seconds=42)
    body = callback(row["message_id"], "delivered", int(delivered_at.replace(tzinfo=UTC).timestamp()))
    headers = signed(body)
    assert "X-NearShop-Client" not in headers
    res = client.post("/api/webhooks/whatsapp", content=body, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["recorded"] == 1
    [row] = rows(outbox)
    assert row["status"] == "delivered" and row["delivered_at"] is not None
    assert row["seconds_to_delivered"] == pytest.approx(42, abs=1)
    # An ordinary API POST without the header is still refused: the webhook is outside the guard, nothing else.
    assert client.post("/api/auth/logout").status_code == 403


def test_duplicate_callbacks_are_ignored(people, outbox, client):
    row = sent_row(people, outbox)
    first = callback(row["message_id"], "delivered")
    assert client.post("/api/webhooks/whatsapp", content=first, headers=signed(first)).json()["recorded"] == 1
    recorded = rows(outbox)[0]
    later = callback(row["message_id"], "delivered", int(utcnow().replace(tzinfo=UTC).timestamp()) - 5)
    again = client.post("/api/webhooks/whatsapp", content=later, headers=signed(later)).json()
    assert again["recorded"] == 0 and again["duplicate"] == 1
    after = rows(outbox)[0]
    assert after["delivered_at"] == recorded["delivered_at"]
    assert after["seconds_to_delivered"] == recorded["seconds_to_delivered"]

    read = callback(row["message_id"], "read")
    assert client.post("/api/webhooks/whatsapp", content=read, headers=signed(read)).json()["recorded"] == 1
    assert client.post("/api/webhooks/whatsapp", content=read, headers=signed(read)).json()["duplicate"] == 1
    # A failure reported after delivery changes nothing.
    failed = callback(row["message_id"], "failed", errors=[{"code": 131026, "title": "Undeliverable"}])
    assert client.post("/api/webhooks/whatsapp", content=failed, headers=signed(failed)).json()["late"] == 1
    final = rows(outbox)[0]
    assert final["status"] == "read" and final.get("failed_at") is None


def test_sent_and_failed_callbacks_are_recorded(people, outbox, client):
    row = sent_row(people, outbox)
    body = callback(row["message_id"], "sent")
    assert client.post("/api/webhooks/whatsapp", content=body, headers=signed(body)).json()["recorded"] == 1
    body = callback(row["message_id"], "failed", errors=[{"code": 131026, "title": "Message undeliverable"}])
    assert client.post("/api/webhooks/whatsapp", content=body, headers=signed(body)).json()["recorded"] == 1
    [row] = rows(outbox)
    assert row["status"] == "failed" and row["error"] == "whatsapp_131026"
    assert row["wa_sent_at"] is not None and row["failed_at"] is not None
    unknown = callback("wamid.nobody", "delivered")
    assert client.post("/api/webhooks/whatsapp", content=unknown, headers=signed(unknown)).json()["unknown"] == 1


def test_a_signed_body_that_is_not_json_is_acknowledged(client):
    body = b"not json"
    assert client.post("/api/webhooks/whatsapp", content=body, headers=signed(body)).status_code == 200


# ---------------------------------------------------------------- the failure-rate event


def add_finished(outbox, sent: int, failed: int, at) -> None:
    db = Database()
    docs = []
    for n in range(sent + failed):
        doc = {"_id": db.next_id(OUTBOX), "notification_id": 0, "user_id": 0, "channel": "whatsapp",
               "kind": "order_preparing", "template": "nearshop_order_status", "language": "en", "params": None,
               "buttons": [], "status": "sent", "attempts": 1, "created_at": at, "next_attempt_at": at,
               "sent_at": at}
        if n >= sent:
            doc.update(status="failed", attempts=4, failed_at=at, error="http_503")
            del doc["sent_at"]
        docs.append(doc)
    outbox.insert_many(docs)


@pytest.fixture
def tracker():
    transport = FakeTransport()
    assert observability.init_error_tracking(dsn=FAKE_DSN, transport=transport)
    yield transport
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)


def run_job(clock=None):
    db = Database()
    assert jobs.run_leased(db.mongo, jobs.SEND_NOTIFICATIONS_JOB, "holder-a",
                           lambda: jobs.send_notifications(clock or utcnow))
    return db.mongo[jobs.LEASES].find_one({"_id": jobs.SEND_NOTIFICATIONS_JOB})["last_result"]


def test_the_failure_rate_event_is_not_reported_at_or_below_5_percent(outbox, tracker):
    now = utcnow()
    add_finished(outbox, sent=19, failed=1, at=now - timedelta(minutes=10))  # 1 of 20: exactly 5%
    add_finished(outbox, sent=0, failed=5, at=now - timedelta(minutes=61))  # older than the hour: not counted
    result = run_job()
    assert result["finished"] == 20 and result["failed"] == 1 and result["failure_rate"] == 0.05
    assert result["alerted"] is False
    sentry_sdk.flush()
    assert tracker.events() == []


def test_the_failure_rate_event_is_reported_above_5_percent(outbox, tracker):
    now = utcnow()
    add_finished(outbox, sent=19, failed=2, at=now - timedelta(minutes=10))  # 2 of 21: 9.5%
    clock = Clock()
    result = run_job(clock)
    assert result["alerted"] is True and result["failure_rate"] == pytest.approx(2 / 21, abs=1e-4)
    sentry_sdk.flush()
    [event] = tracker.events()
    assert event["level"] == "warning" and event["message"] == whatsapp.FAILURE_WARNING
    assert event["tags"]["alert"] == "whatsapp_failures" and event["fingerprint"] == ["whatsapp-failure-rate"]
    # Still above on the next runs: reported again only after FAILURE_ALERT_EVERY.
    clock.advance(seconds=30)
    assert run_job(clock)["alerted"] is False
    clock.advance(minutes=15)
    assert run_job(clock)["alerted"] is True
    sentry_sdk.flush()
    assert len(tracker.events()) == 2
