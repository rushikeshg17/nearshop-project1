"""Outbound WhatsApp notifications through an outbox (P4.3; plan sections 8.7 and 8.10; docs/whatsapp-templates.md).

The outbox
----------
notify() (app/services/notifications.py) writes the in-app notification and, in the same transaction, calls enqueue,
which writes one notification_outbox row when all of these hold:

  * the notification's kind has a WhatsApp template (TEMPLATES) for the recipient's role: a new reservation or order
    for the shop's owner; a reservation confirmed, ready, expired or rejected, or an order's status change, for the
    customer. Every other kind stays in the app only;
  * WhatsApp notifications are on here (get_client: NEARSHOP_WHATSAPP_NOTIFICATIONS, with the Cloud API configured,
    or the fake client in development and test);
  * the recipient opted in: the WhatsApp consent purpose (P3.1) is granted, and the account is active with a verified
    phone (P4.1). A message never goes to an unverified number.

The row holds the template, its language and its two variables (the notification's title and text, on one line
each), never the phone number: the sender reads the number at send time. A rolled-back transaction leaves neither
the notification nor its row.

The sender
----------
send_due runs from the send_notifications job (app/jobs.py) on every 30-second tick, under that job's lease, so one
process sends. It claims due rows one at a time (queued -> sending), checks the recipient again (a consent withdrawn
or a phone removed since the notification cancels the row), and sends the template through the client. A failed
send is retried RETRY_DELAYS after each failure (30 s, 2 min, 10 min), and the row is marked failed after the third
retry fails. A row still in sending SENDING_STALE after it was claimed (the process stopped mid-send) counts as a
failed attempt. A row older than STALE_AFTER before its first send (the sender was off) is cancelled, not sent: the
news is old. A run stops after BATCH rows or RUN_BUDGET_SECONDS, so a slow Cloud API never holds up the other jobs.
Nothing here runs inside a reservation or order transaction, so a slow or failing send can never block or fail one.

Status callbacks
----------------
Meta calls POST /api/webhooks/whatsapp (app/api/routes/webhooks.py) with each message's sent, delivered, read and
failed statuses. record_statuses writes each one once on the row with that message id: a duplicate callback finds
its time already set and changes nothing. The row keeps seconds_to_sent (from notify to the Cloud API's accept) and
seconds_to_delivered (from notify to Meta's delivered time), the measures of the P4.3 acceptance target.

The failure alert
-----------------
Each sender run computes failure_rate: the rows marked failed in the last hour over the rows that finished (sent or
failed) in it. Above 5% it reports a warning event to the error tracker (report_failure_rate, fingerprint
whatsapp-failure-rate), on which the WhatsApp alert of plan section 8.10 fires (set up at plan step 4-A). The job
reports it at most once every FAILURE_ALERT_EVERY while the rate stays above.

Message text is never logged: it can hold a customer's name or a delivery code. Phone numbers are logged masked.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pymongo import ReturnDocument

from app.core.config import Settings, settings
from app.core.database import Database, utcnow
from app.models import Notification
from app.models.enums import OrderStatus, Role
from app.services import messaging
from app.services.messaging import Delivery, masked

log = logging.getLogger("nearshop.whatsapp")

OUTBOX = "notification_outbox"
CHANNEL = "whatsapp"
CONSENT_PURPOSE = "whatsapp"  # the P3.1 consent purpose for WhatsApp messages
RETRY_DELAYS = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10))
MAX_ATTEMPTS = 1 + len(RETRY_DELAYS)
SENDING_STALE = timedelta(minutes=2)  # a claimed row not finished by then: its process stopped mid-send
STALE_AFTER = timedelta(minutes=30)  # a row not sent by then is cancelled (a request expires after 20 minutes)
BATCH = 50
RUN_BUDGET_SECONDS = 20.0
FAILURE_WINDOW = timedelta(hours=1)
FAILURE_THRESHOLD = 0.05  # the WhatsApp alert (plan section 8.10): failure rate above 5% over 1 hour
FAILURE_ALERT_EVERY = timedelta(minutes=15)
FAILURE_WARNING = "WhatsApp notification failure rate above 5% over the last hour"
PARAM_MAX = 400  # characters per template variable (Meta allows 1,024 for the whole body)
STATUSES = ("queued", "sending", "sent", "delivered", "read", "failed", "cancelled")
# A status callback's field on the row. "sent" is Meta's confirmation that it sent the message to WhatsApp; the row's
# own sent_at is when the Cloud API accepted it.
CALLBACK_FIELDS = {"sent": "wa_sent_at", "delivered": "delivered_at", "read": "read_at", "failed": "failed_at"}


@dataclass(frozen=True)
class Template:
    """An approved utility template (docs/whatsapp-templates.md): its name at Meta, the role of the recipient it is
    for, and the actions of its quick-reply buttons, in order."""

    name: str
    role: Role
    buttons: tuple[str, ...] = ()


_ORDER_STATUS = Template("nearshop_order_status", Role.CUSTOMER)
# The notification kinds that go out on WhatsApp, by the kind notify() is given.
TEMPLATES: dict[str, Template] = {
    "reservation_new": Template("nearshop_shop_new_reservation", Role.OWNER, ("confirm", "decline")),
    "order_new": Template("nearshop_shop_new_order", Role.OWNER),
    "reservation_confirmed": Template("nearshop_reservation_confirmed", Role.CUSTOMER),
    "reservation_ready": Template("nearshop_reservation_ready", Role.CUSTOMER),
    "reservation_expired": Template("nearshop_reservation_closed", Role.CUSTOMER),
    "reservation_rejected": Template("nearshop_reservation_closed", Role.CUSTOMER),
    **{f"order_{s.lower()}": _ORDER_STATUS for s in (
        OrderStatus.SHOP_CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED,
        OrderStatus.DELIVERY_FAILED, OrderStatus.CANCELLED)},
}

# A reservation or order code (app/utils/text.py short_code) in a notification's title.
_CODE = re.compile(r"\b([RD])-([A-Z0-9]{4,12})\b")


# ---------------------------------------------------------------- clients


class WhatsAppClient(Protocol):
    def send_template(self, phone: str, template: str, language: str,
                      components: list[dict[str, Any]]) -> Delivery: ...


class CloudClient:
    """Meta's WhatsApp Cloud API, with the access token and phone number id from settings."""

    def __init__(self, config: Settings = settings):
        self.config = config

    def send_template(self, phone: str, template: str, language: str,
                      components: list[dict[str, Any]]) -> Delivery:
        payload = messaging.whatsapp_template_payload(phone, template, language, components)
        return messaging.send_whatsapp(self.config, payload, self.config.whatsapp_utility_cost_inr)


class FakeClient:
    """Development and test: keeps the messages in memory (sent) and logs them with the phone masked; nothing leaves
    the machine. failures makes the next sends fail (tests)."""

    def __init__(self) -> None:
        self.sent: deque[dict[str, Any]] = deque(maxlen=500)
        self.failures = 0
        self._n = 0

    def send_template(self, phone: str, template: str, language: str,
                      components: list[dict[str, Any]]) -> Delivery:
        if self.failures > 0:
            self.failures -= 1
            return Delivery(False, CHANNEL, error="http_503")
        self._n += 1
        message_id = f"wamid.fake.{self._n}.{time.time_ns()}"
        self.sent.append({"phone": phone, "template": template, "language": language, "components": components,
                          "message_id": message_id})
        log.info("fake WhatsApp %s to %s (%s)", template, masked(phone), message_id)
        return Delivery(True, CHANNEL, message_id=message_id)

    def reset(self) -> None:
        self.sent.clear()
        self.failures = 0


FAKE = FakeClient()


def get_client(config: Settings = settings) -> WhatsAppClient | None:
    """The client notifications go through, or None while WhatsApp notifications are off here. The Cloud API when
    NEARSHOP_WHATSAPP_NOTIFICATIONS is on and the access token and phone number id are set (never in the test
    environment); otherwise, in development and test, the fake client. Elsewhere, unconfigured, None."""
    if not config.whatsapp_notifications:
        return None
    if config.environment != "test" and config.whatsapp_notify_configured:
        return CloudClient(config)
    if config.environment in ("development", "test"):
        return FAKE
    return None


# ---------------------------------------------------------------- enqueue (inside notify's transaction)


def _one_line(text: str | None) -> str:
    """A template variable: Meta refuses new lines, tabs and runs of spaces in one, and an empty one."""
    flat = " ".join((text or "").split())
    return (flat[:PARAM_MAX - 1] + "…" if len(flat) > PARAM_MAX else flat) or "-"


def subject_of(note: Notification) -> str | None:
    """What the notification is about, for the buttons and for P4.4's replies: reservation:<code> or order:<code>
    from the code in its title, or reservation:<id> or order:<id> from its link."""
    if match := _CODE.search(note.title or ""):
        return f"{'reservation' if match.group(1) == 'R' else 'order'}:{match.group(0)}"
    if note.link and (match := re.fullmatch(r"/account/(reservations|orders)/(\d+)", note.link)):
        return f"{match.group(1)[:-1]}:{match.group(2)}"
    return None


def consented(db: Database, user_id: int) -> bool:
    """The WhatsApp consent in force (the newest consents row for the purpose, P3.1), read in db's transaction."""
    row = db.mongo["consents"].find_one({"user_id": user_id, "purpose": CONSENT_PURPOSE}, {"granted": 1},
                                        sort=[("at", -1), ("_id", -1)], session=db.session)
    return bool(row and row.get("granted"))


def _recipient(db: Database, user_id: int, template: Template) -> tuple[str | None, str | None]:
    """(phone, None) when the user can get this template now, else (None, the reason)."""
    user = db.users.raw.find_one({"_id": user_id}, {"role": 1, "phone": 1, "phone_verified_at": 1, "is_active": 1},
                                 session=db.session)
    if user is None or not user.get("is_active", True):
        return None, "inactive"
    if user.get("role") != template.role.value:
        return None, "role"
    if not user.get("phone") or user.get("phone_verified_at") is None:
        return None, "no_verified_phone"
    if not consented(db, user_id):
        return None, "opted_out"
    return user["phone"], None


def enqueue(db: Database, note: Notification) -> int | None:
    """Write the notification's outbox row in db's transaction, when its kind has a template and the recipient opted
    in (module docstring). Returns the row's id, or None when nothing was written."""
    template = TEMPLATES.get(note.kind)
    if template is None or get_client() is None:
        return None
    phone, _ = _recipient(db, note.user_id, template)
    if phone is None:
        return None
    subject = subject_of(note)
    created = note.created_at
    row = {"_id": db.next_id(OUTBOX), "notification_id": note.id, "user_id": note.user_id, "channel": CHANNEL,
           "kind": note.kind, "template": template.name,
           "language": settings.whatsapp_template_language.strip() or "en",
           "params": [_one_line(note.title), _one_line(note.body)], "subject": subject,
           "buttons": [f"{subject or f'notification:{note.id}'}:{action}" for action in template.buttons],
           "status": "queued", "attempts": 0, "created_at": created, "next_attempt_at": created}
    db.mongo[OUTBOX].insert_one(row, session=db.session)
    return row["_id"]


# ---------------------------------------------------------------- the sender


def components(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The template's body variables and its quick-reply buttons' payloads."""
    parts: list[dict[str, Any]] = [
        {"type": "body", "parameters": [{"type": "text", "text": p} for p in row.get("params") or ["-", "-"]]}]
    parts += [{"type": "button", "sub_type": "quick_reply", "index": str(i),
               "parameters": [{"type": "payload", "payload": payload}]}
              for i, payload in enumerate(row.get("buttons") or [])]
    return parts


def _seconds(later: datetime, earlier: datetime) -> float:
    return round(max(0.0, (later - earlier).total_seconds()), 3)


def _attempt_failed(db: Database, row: dict[str, Any], error: str, now: datetime) -> str:
    """Record a failed attempt: retried after the next of RETRY_DELAYS, or failed after the last retry."""
    attempts = row.get("attempts", 0) + 1
    if attempts < MAX_ATTEMPTS:
        db.mongo[OUTBOX].update_one({"_id": row["_id"]}, {"$set": {
            "status": "queued", "attempts": attempts, "error": error[:80],
            "next_attempt_at": now + RETRY_DELAYS[attempts - 1]}, "$unset": {"claimed_at": ""}})
        return "retrying"
    db.mongo[OUTBOX].update_one({"_id": row["_id"]}, {"$set": {
        "status": "failed", "attempts": attempts, "error": error[:80], "failed_at": now, "params": None},
        "$unset": {"claimed_at": ""}})
    log.warning("WhatsApp notification %s failed after %d attempts: %s", row["_id"], attempts, error[:80])
    return "failed"


def _finish(db: Database, row_id: int, status: str, now: datetime, reason: str) -> None:
    db.mongo[OUTBOX].update_one({"_id": row_id}, {"$set": {"status": status, "error": reason, "finished_at": now,
                                                           "params": None}, "$unset": {"claimed_at": ""}})


def send_due(db: Database, client: WhatsAppClient | None = None, clock: Callable[[], datetime] = utcnow,
             budget_seconds: float = RUN_BUDGET_SECONDS) -> dict[str, Any]:
    """One sender run (module docstring). Returns the counts of rows sent, retrying, failed and cancelled."""
    counts = {"sent": 0, "retrying": 0, "failed": 0, "cancelled": 0}
    out = db.mongo[OUTBOX]
    now = clock()
    for row in out.find({"status": "sending", "claimed_at": {"$lte": now - SENDING_STALE}}).limit(BATCH):
        counts[_attempt_failed(db, row, "interrupted", now)] += 1
    client = client if client is not None else get_client()
    if client is None:
        return {"enabled": False, **counts}
    started = time.monotonic()
    for _ in range(BATCH):
        if time.monotonic() - started > budget_seconds:
            break
        now = clock()
        row = out.find_one_and_update({"status": "queued", "next_attempt_at": {"$lte": now}},
                                      {"$set": {"status": "sending", "claimed_at": now}},
                                      sort=[("next_attempt_at", 1), ("_id", 1)], return_document=ReturnDocument.AFTER)
        if row is None:
            break
        if row.get("attempts", 0) == 0 and now - row["created_at"] > STALE_AFTER:
            _finish(db, row["_id"], "cancelled", now, "stale")
            counts["cancelled"] += 1
            continue
        template = TEMPLATES.get(row["kind"])
        phone, reason = _recipient(db, row["user_id"], template) if template else (None, "no_template")
        if phone is None:
            _finish(db, row["_id"], "cancelled", now, reason or "unreachable")
            counts["cancelled"] += 1
            continue
        try:
            delivery = client.send_template(phone, row["template"], row["language"], components(row))
        except Exception as exc:  # a client must not raise, but one bad send must not stop the run
            delivery = Delivery(False, CHANNEL, error=type(exc).__name__)
        done = clock()
        if delivery.ok:
            out.update_one({"_id": row["_id"]}, {"$set": {
                "status": "sent", "attempts": row.get("attempts", 0) + 1, "message_id": delivery.message_id,
                "sent_at": done, "seconds_to_sent": _seconds(done, row["created_at"]), "error": None, "params": None,
                "cost_estimate": round(delivery.cost_estimate, 4)},
                "$unset": {"claimed_at": ""}})
            counts["sent"] += 1
        else:
            counts[_attempt_failed(db, row, delivery.error or "failed", done)] += 1
    return {"enabled": True, **counts}


# ---------------------------------------------------------------- the failure rate and its alert


def failure_rate(db: Database, now: datetime) -> dict[str, Any]:
    """Over the last FAILURE_WINDOW: rows that finished (accepted by the Cloud API, or failed), the failed ones among
    them, and the rate (0 with none)."""
    since = now - FAILURE_WINDOW
    out = db.mongo[OUTBOX]
    finished = out.count_documents({"$or": [{"sent_at": {"$gte": since}}, {"failed_at": {"$gte": since}}]})
    failed = out.count_documents({"failed_at": {"$gte": since}})
    return {"finished": finished, "failed": failed, "failure_rate": round(failed / finished, 4) if finished else 0.0}


def above_threshold(rate: dict[str, Any]) -> bool:
    return rate["finished"] > 0 and rate["failed"] / rate["finished"] > FAILURE_THRESHOLD


def report_failure_rate(rate: dict[str, Any]) -> None:
    """The warning event the WhatsApp alert fires on (plan section 8.10): the error tracker, when it is on, and the
    log."""
    log.warning("%s: %d of %d failed", FAILURE_WARNING, rate["failed"], rate["finished"])
    try:
        import sentry_sdk

        if sentry_sdk.get_client().is_active():
            sentry_sdk.capture_message(FAILURE_WARNING, level="warning", fingerprint=["whatsapp-failure-rate"],
                                       tags={"alert": "whatsapp_failures"}, extras=dict(rate))
    except Exception as exc:  # the tracker must never stop the sender
        log.warning("WhatsApp failure warning not reported to the error tracker: %s", type(exc).__name__)


# ---------------------------------------------------------------- status callbacks (the webhook)


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: "sha256=" and the HMAC-SHA256 of the raw body, keyed with the app secret, in hex. Compared
    in constant time; no secret, no header or another scheme is invalid."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip().lower())


def _callback_time(value: Any, now: datetime) -> datetime:
    """A callback's timestamp (Unix seconds, as a string) as naive UTC; now when missing, malformed or in the future."""
    try:
        at = datetime.fromtimestamp(int(value), UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return now
    return min(at, now)


def _statuses(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    found = []
    for entry in payload.get("entry") or []:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") if isinstance(change, dict) else None
            if isinstance(value, dict):
                found += [s for s in value.get("statuses") or [] if isinstance(s, dict)]
    return found


def record_status(db: Database, status: dict[str, Any], now: datetime) -> str:
    """Record one status callback on its outbox row, once. Returns recorded, duplicate, unknown (no row has the
    message id, or the status is not one we track) or late (a failure after delivery: kept as delivered)."""
    message_id, kind = status.get("id"), status.get("status")
    if not isinstance(message_id, str) or not message_id or kind not in CALLBACK_FIELDS:
        return "unknown"
    out = db.mongo[OUTBOX]
    field = CALLBACK_FIELDS[kind]
    at = _callback_time(status.get("timestamp"), now)
    new: dict[str, Any] = {field: at}
    match: dict[str, Any] = {"message_id": message_id, field: None}
    if kind == "delivered":
        new["seconds_to_delivered"] = {"$max": [0, {"$divide": [{"$subtract": [at, "$created_at"]}, 1000]}]}
        new["status"] = {"$cond": [{"$in": ["$status", ["sending", "sent"]]}, "delivered", "$status"]}
    elif kind == "read":
        new["status"] = {"$cond": [{"$in": ["$status", ["sending", "sent", "delivered"]]}, "read", "$status"]}
    elif kind == "failed":
        errors = status.get("errors") if isinstance(status.get("errors"), list) else []
        code = next((e.get("code") for e in errors if isinstance(e, dict) and e.get("code") is not None), None)
        new["error"] = f"whatsapp_{code}"[:80] if code is not None else "whatsapp_failed"
        new["status"] = "failed"
        match["status"] = {"$in": ["sending", "sent"]}  # a failure reported after delivery changes nothing
    if out.update_one(match, [{"$set": new}]).modified_count:
        if kind == "failed":
            log.warning("WhatsApp message %s failed after it was sent: %s", message_id, new["error"])
        return "recorded"
    row = out.find_one({"message_id": message_id}, {"status": 1, field: 1})
    if row is None:
        return "unknown"
    return "duplicate" if row.get(field) is not None else "late"


def record_statuses(db: Database, payload: Any, now: datetime | None = None) -> dict[str, int]:
    """Every status callback in a webhook call (Meta batches them), each recorded once. Messages from users (P4.4)
    are not handled here. Returns how many were recorded, duplicate, late and unknown."""
    now = now or utcnow()
    counts = {"recorded": 0, "duplicate": 0, "late": 0, "unknown": 0}
    for status in _statuses(payload):
        counts[record_status(db, status, now)] += 1
    return counts

