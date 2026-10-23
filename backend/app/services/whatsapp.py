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
the notification nor its row. The text is in the recipient's language (P7.11, app/services/notification_text.py), and
so is the template: en (the code in NEARSHOP_WHATSAPP_TEMPLATE_LANGUAGE) or kn (template_language), except that a kind
whose message awaits the team's Kannada (notification_text.AWAITING_KANNADA) always uses the en template.

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

Two gaps in those measures are closed here:

  * A callback can arrive before the sender has saved the message id the Cloud API returned (Meta can call the
    webhook within a second of accepting a message). Such a callback is held in whatsapp_status_pending (PENDING, for
    PENDING_KEEP) and applied when the sender saves the id (apply_pending). The webhook holds it first and then tries
    the row once more, and the sender saves the id first and then reads what is held, so whichever runs second sees
    the other's write. A held callback whose message is not a notification (a sign-in code) is never applied, and the
    TTL index removes it.
  * Meta may report read without ever reporting delivered. A read message was delivered, so read sets delivered_at
    (and seconds_to_delivered) to its own time when no delivered callback came, with delivered_inferred set. That is
    an upper bound, so the delivery measures err on the slow side. A delivered callback arriving later replaces it.

The failure alert
-----------------
Each sender run computes failure_rate: the rows marked failed in the last hour over the rows that finished (sent or
failed) in it. Above 5% it reports a warning event to the error tracker (report_failure_rate, fingerprint
whatsapp-failure-rate), on which the WhatsApp alert of plan section 8.10 fires (set up at plan step 4-A). The job
reports it at most once every FAILURE_ALERT_EVERY while the rate stays above.

Replies from shops (P4.4)
-------------------------
The webhook hands every message a phone sends to handle_messages. Each is handled once (its id claimed in
whatsapp_inbound first) and only from a shop's verified phone (shop_for_phone): a Confirm or Decline button on the
new-reservation template runs that transition as the owner, noted "via WhatsApp"; lines of item and count are matched
to the shop's listings with the importer's matcher and shown back, and applied with set_stock (reason
whatsapp_update) when the owner replies Yes; an ambiguous line offers up to 3 candidates; any other number gets the
support reply. Replies are free-form texts, sent only inside the sender's 24-hour service window. send_stock_checks,
run by the stock_check job, sends each shop's stock check from its opening time (P9.3 cadence below).
docs/whatsapp-templates.md
describes the conversation. A statement's quick reply (payload statement:<id>:<reply>), or its reply typed inside the
window, and the reason asked for after Not accurate, go to app/services/statements.py first (P9.9). The replies and
the stock check are in the owner's language (P7.11: the catalogue in app/services/notification_text.py); a number
that is not a shop's gets the English support reply.

The stock-check cadence and its cost (P9.3; plan section 8.17)
---------------------------------------------------------------
Meta charges per template sent outside the recipient's 24-hour customer-service window and may class the stock check
as marketing. So the check goes daily, at the shop's opening hour, only to an owner whose window will still be open
WINDOW_MARGIN (30 minutes) later, as a free-form text inside it (route in_window, category service: no charge); every
other shop with a stale listing gets the template once a week, at the opening hour of NEARSHOP_STOCK_CHECK_WEEKLY_DAY
(Monday; the next day for a shop closed on it), route template, category utility. At most one check per shop per
IST day, none on the shop's closed day (closed_on), none when no listing is older than 48 hours. The window is the
owner's own: whatsapp_sessions keeps owner_inbound_at, the last message from the owner's verified phone (a message
from the shop's call-back-verified number opens that number's window, not the owner's). Every outbox row records its
route and the category it was sent under; Meta's status callbacks add the category, billable flag and pricing type
Meta reports (meta_category, billable, pricing_type) and the cost those imply (cost_estimate), so the plan's section
11 cost can be checked against what was actually charged.

The monthly statement (P9.9)
----------------------------
app/services/statements.py sends each active shop's monthly statement through notify, as statement_free (the shadow
bill) or statement_roi. Their outbox rows carry the template's own variables (STATEMENT_VARIABLES) instead of the
notification's title and text, and buttons whose payloads name the statement; inside the owner's open window the row
goes as a free-form text instead (in_window_text), which asks for the reply in words.

Message text is never logged: it can hold a customer's name or a delivery code. Phone numbers are logged masked.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import WEEKDAYS, Settings, settings
from app.core.database import Database, utcnow
from app.core.errors import AppError
from app.models import InventoryReason, Notification, Product, Reservation, Shop, User
from app.models.enums import OrderStatus, Role
from app.services import messaging
from app.services import notification_text as words
from app.services.messaging import Delivery, masked

log = logging.getLogger("nearshop.whatsapp")

OUTBOX = "notification_outbox"
PENDING = "whatsapp_status_pending"  # status callbacks that came before their message id was saved
PENDING_KEEP = timedelta(hours=1)
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
TEXT_MAX = 4096  # characters in a free-form text message (Cloud API limit)
STATUSES = ("queued", "sending", "sent", "delivered", "read", "failed", "cancelled")
# A status callback's field on the row. "sent" is Meta's confirmation that it sent the message to WhatsApp; the row's
# own sent_at is when the Cloud API accepted it.
CALLBACK_FIELDS = {"sent": "wa_sent_at", "delivered": "delivered_at", "read": "read_at", "failed": "failed_at"}
CALLBACK_ORDER = tuple(CALLBACK_FIELDS)  # the order held callbacks are applied in


@dataclass(frozen=True)
class Template:
    """An approved utility template (docs/whatsapp-templates.md): its name at Meta, the role of the recipient it is
    for, the actions of its quick-reply buttons, in order, and the category it is submitted under (Meta decides the
    one it charges, which the status callbacks report: meta_category)."""

    name: str
    role: Role
    buttons: tuple[str, ...] = ()
    category: str = "utility"


# How an outbox row is sent (P9.3): as its template, or as a free-form text inside the recipient's open 24-hour
# customer-service window (only the stock check, send_stock_checks). Each row records its route and category.
ROUTE_TEMPLATE = "template"
ROUTE_IN_WINDOW = "in_window"
CATEGORY_SERVICE = "service"  # Meta's category for free-form messages inside the window: not charged


_ORDER_STATUS = Template("nearshop_order_status", Role.CUSTOMER)
# The notification kinds that go out on WhatsApp, by the kind notify() is given.
TEMPLATES: dict[str, Template] = {
    "reservation_new": Template("nearshop_shop_new_reservation", Role.OWNER, ("confirm", "decline")),
    "order_new": Template("nearshop_shop_new_order", Role.OWNER),
    "stock_check": Template("nearshop_shop_stock_check", Role.OWNER),  # the daily stock check (P4.4)
    # The monthly statement (P9.9, app/services/statements.py): a shadow bill to a shop on Free (and to every shop
    # before billing starts), an ROI statement to a paying shop. Their buttons' payloads are statement:<id>:<reply>.
    "statement_free": Template("nearshop_shop_statement_free", Role.OWNER,
                               ("accurate", "not_accurate", "would_pay", "not_now")),
    "statement_roi": Template("nearshop_shop_statement_roi", Role.OWNER, ("accurate", "not_accurate")),
    "arrival_failure": Template("nearshop_shop_arrival_report", Role.OWNER),  # an item missing on arrival (P9.1)
    "reservation_confirmed": Template("nearshop_reservation_confirmed", Role.CUSTOMER),
    "reservation_ready": Template("nearshop_reservation_ready", Role.CUSTOMER),
    "reservation_expired": Template("nearshop_reservation_closed", Role.CUSTOMER),
    "reservation_rejected": Template("nearshop_reservation_closed", Role.CUSTOMER),
    **{f"order_{s.lower()}": _ORDER_STATUS for s in (
        OrderStatus.SHOP_CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED,
        OrderStatus.DELIVERY_FAILED, OrderStatus.CANCELLED)},
}

# The statement templates' body variables, in order (docs/whatsapp-templates.md): an outbox row of these kinds holds
# them as its params instead of the notification's title and text, and inside the window they fill the free-form text
# (STATEMENTS free_text and roi_text in app/services/notification_text.py).
STATEMENT_VARIABLES: dict[str, tuple[str, ...]] = {
    "statement_free": ("month_shop", "counts", "price"),
    "statement_roi": ("month_shop", "counts", "plan", "guarantee"),
}
STATEMENT_TEXT_KEYS = {"statement_free": "free_text", "statement_roi": "roi_text"}

# The Meta language code of the notification templates for each language a user can choose (P7.11). English keeps the
# code in NEARSHOP_WHATSAPP_TEMPLATE_LANGUAGE. Precondition before Kannada is offered to users: every template in
# docs/whatsapp-templates.md must be approved at Meta in kn as well; until then Meta rejects a kn send and the outbox
# row is marked failed (the in-app notification is unaffected).
KANNADA_TEMPLATE_LANGUAGE = "kn"


def template_language(language: str | None) -> str:
    """The template language for a user's language: kn for Kannada, otherwise English (also for a language the
    templates are not approved in yet)."""
    if words.language_of(language) == "kn":
        return KANNADA_TEMPLATE_LANGUAGE
    return settings.whatsapp_template_language.strip() or "en"


# A reservation or order code (app/utils/text.py short_code) in a notification's title.
_CODE = re.compile(r"\b([RD])-([A-Z0-9]{4,12})\b")


# ---------------------------------------------------------------- clients


class WhatsAppClient(Protocol):
    def send_template(self, phone: str, template: str, language: str,
                      components: list[dict[str, Any]]) -> Delivery: ...

    def send_text(self, to: str, text: str) -> Delivery: ...


class CloudClient:
    """Meta's WhatsApp Cloud API, with the access token and phone number id from settings."""

    def __init__(self, config: Settings = settings):
        self.config = config

    def send_template(self, phone: str, template: str, language: str,
                      components: list[dict[str, Any]]) -> Delivery:
        payload = messaging.whatsapp_template_payload(phone, template, language, components)
        return messaging.send_whatsapp(self.config, payload, self.config.whatsapp_utility_cost_inr)

    def send_text(self, to: str, text: str) -> Delivery:
        """A free-form text reply (P4.4), allowed only inside the recipient's 24-hour service window, where Meta does
        not charge for it. to: the WhatsApp id the message came from (country code first, digits only)."""
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
                   "text": {"preview_url": False, "body": text[:TEXT_MAX]}}
        return messaging.send_whatsapp(self.config, payload, 0.0)


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

    def send_text(self, to: str, text: str) -> Delivery:
        if self.failures > 0:
            self.failures -= 1
            return Delivery(False, CHANNEL, error="http_503")
        self._n += 1
        message_id = f"wamid.fake.{self._n}.{time.time_ns()}"
        self.sent.append({"phone": to, "template": None, "text": text[:TEXT_MAX], "message_id": message_id})
        log.info("fake WhatsApp reply to %s (%s)", masked(to), message_id)
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


def _reachable(db: Database, user_id: int, template: Template) -> tuple[dict[str, Any] | None, str | None]:
    """(the user's document, None) when the user can get this template now, else (None, the reason)."""
    user = db.users.raw.find_one({"_id": user_id}, {"role": 1, "phone": 1, "phone_verified_at": 1, "is_active": 1,
                                                    "language": 1}, session=db.session)
    if user is None or not user.get("is_active", True):
        return None, "inactive"
    if user.get("role") != template.role.value:
        return None, "role"
    if not user.get("phone") or user.get("phone_verified_at") is None:
        return None, "no_verified_phone"
    if not consented(db, user_id):
        return None, "opted_out"
    return user, None


def _recipient(db: Database, user_id: int, template: Template) -> tuple[str | None, str | None]:
    """(phone, None) when the user can get this template now, else (None, the reason)."""
    user, reason = _reachable(db, user_id, template)
    return (user["phone"], None) if user is not None else (None, reason)


def _row_language(kind: str, language: str | None) -> str:
    """The template language of a kind's outbox row. A kind whose message still awaits the team's Kannada
    (notification_text.AWAITING_KANNADA) was rendered in English for everyone, and its template has no kn version
    at Meta yet, so it goes with the English template until both exist."""
    if kind in words.AWAITING_KANNADA:
        return template_language(None)
    return template_language(language)


def enqueue(db: Database, note: Notification) -> int | None:
    """Write the notification's outbox row in db's transaction, when its kind has a template and the recipient opted
    in (module docstring). Returns the row's id, or None when nothing was written."""
    template = TEMPLATES.get(note.kind)
    if template is None or get_client() is None:
        return None
    user, _ = _reachable(db, note.user_id, template)
    if user is None:
        return None
    subject = subject_of(note)
    created = note.created_at
    row = {"_id": db.next_id(OUTBOX), "notification_id": note.id, "user_id": note.user_id, "channel": CHANNEL,
           "kind": note.kind, "template": template.name,
           # The template in the language notify() rendered the text in: the recipient's, read in this transaction.
           "language": _row_language(note.kind, user.get("language")),
           "params": [_one_line(note.title), _one_line(note.body)], "subject": subject,
           "route": ROUTE_TEMPLATE, "category": template.category,
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
        in_window = row.get("route") == ROUTE_IN_WINDOW
        if in_window and not window_open(db, row["user_id"], now):
            # Free-form messages are allowed only inside the window; it closed before the row was sent (P9.3).
            _finish(db, row["_id"], "cancelled", now, "window_closed")
            counts["cancelled"] += 1
            continue
        try:
            if in_window:
                delivery = client.send_text(messaging.e164(phone), in_window_text(row))
            else:
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
            if delivery.message_id:
                apply_pending(db, delivery.message_id, done)
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


def _error_code(status: dict[str, Any]) -> str | None:
    errors = status.get("errors") if isinstance(status.get("errors"), list) else []
    code = next((e.get("code") for e in errors if isinstance(e, dict) and e.get("code") is not None), None)
    return str(code)[:40] if code is not None else None


def _unset(field: str) -> dict[str, Any]:
    """Aggregation: the row's field is missing or null."""
    return {"$eq": [{"$ifNull": [f"${field}", None]}, None]}


def _status_if(current: list[str], new: str) -> dict[str, Any]:
    return {"$cond": [{"$in": ["$status", current]}, new, "$status"]}


_SECONDS_TO_DELIVERED = {"$set": {"seconds_to_delivered": {
    "$max": [0, {"$divide": [{"$subtract": ["$delivered_at", "$created_at"]}, 1000]}]}}}


def _apply(db: Database, message_id: str, kind: str, at: datetime, code: str | None) -> str:
    """Write one status on the row with message_id, once: recorded, duplicate, late (a failure after delivery: kept
    as delivered) or unknown (no row has the id yet)."""
    out = db.mongo[OUTBOX]
    field = CALLBACK_FIELDS[kind]
    match: dict[str, Any] = {"message_id": message_id, field: None}
    stages: list[dict[str, Any]] = [{"$set": {field: at}}]
    error = None
    if kind == "delivered":
        # Also replaces a delivered time taken from a read callback (module docstring).
        match = {"message_id": message_id, "$or": [{"delivered_at": None}, {"delivered_inferred": True}]}
        stages = [{"$set": {"delivered_at": at, "delivered_inferred": False,
                            "status": _status_if(["sending", "sent"], "delivered")}}, _SECONDS_TO_DELIVERED]
    elif kind == "read":
        stages = [{"$set": {"read_at": at, "status": _status_if(["sending", "sent", "delivered"], "read"),
                            "delivered_inferred": {"$cond": [_unset("delivered_at"), True, "$delivered_inferred"]},
                            "delivered_at": {"$ifNull": ["$delivered_at", at]}}}, _SECONDS_TO_DELIVERED]
    elif kind == "failed":
        error = f"whatsapp_{code}"[:80] if code is not None else "whatsapp_failed"
        stages = [{"$set": {field: at, "error": error, "status": "failed"}}]
        match["status"] = {"$in": ["sending", "sent"]}  # a failure reported after delivery changes nothing
    if out.update_one(match, stages).modified_count:
        if error:
            log.warning("WhatsApp message %s failed after it was sent: %s", message_id, error)
        return "recorded"
    row = out.find_one({"message_id": message_id}, {"status": 1, field: 1})
    if row is None:
        return "unknown"
    return "duplicate" if row.get(field) is not None else "late"


def _pricing(status: dict[str, Any]) -> dict[str, Any] | None:
    """What a status callback says Meta charges for the message (P9.3): its pricing object's category (utility,
    marketing, authentication or service), billable flag and type (regular, free_customer_service, ...)."""
    pricing = status.get("pricing")
    if not isinstance(pricing, dict):
        return None
    found: dict[str, Any] = {}
    if isinstance(pricing.get("category"), str) and pricing["category"].strip():
        found["meta_category"] = pricing["category"].strip().lower()[:40]
    if isinstance(pricing.get("billable"), bool):
        found["billable"] = pricing["billable"]
    if isinstance(pricing.get("type"), str) and pricing["type"].strip():
        found["pricing_type"] = pricing["type"].strip().lower()[:40]
    return found or None


def priced_cost(pricing: dict[str, Any]) -> float | None:
    """The cost in rupees a callback's pricing implies (plan section 8.7 rates), or None when it does not say. Free
    when not billable, in the service category, or of a free pricing type (free_customer_service, free_entry_point):
    Meta is deprecating billable, after which a free utility message is told apart only by its pricing type."""
    if (pricing.get("billable") is False or pricing.get("meta_category") == CATEGORY_SERVICE
            or str(pricing.get("pricing_type") or "").startswith("free_")):
        return 0.0
    rates = {"utility": settings.whatsapp_utility_cost_inr, "marketing": settings.whatsapp_marketing_cost_inr,
             "authentication": settings.whatsapp_cost_inr}
    return rates.get(pricing.get("meta_category") or "")


def _record_pricing(db: Database, message_id: str, pricing: dict[str, Any] | None) -> None:
    """Write the category Meta reports, and the cost it implies, on the row with message_id (P9.3)."""
    if not pricing:
        return
    fields = dict(pricing)
    cost = priced_cost(pricing)
    if cost is not None:
        fields["cost_estimate"] = round(cost, 4)
    db.mongo[OUTBOX].update_one({"message_id": message_id}, {"$set": fields})


def _hold(db: Database, message_id: str, kind: str, at: datetime, code: str | None, now: datetime,
          pricing: dict[str, Any] | None = None) -> None:
    try:
        db.mongo[PENDING].update_one({"_id": f"{message_id}:{kind}"}, {"$setOnInsert": {
            "message_id": message_id, "status": kind, "at": at, "error_code": code, "received_at": now,
            "expires_at": now + PENDING_KEEP, **({"pricing": pricing} if pricing else {})}}, upsert=True)
    except DuplicateKeyError:  # the same callback held by a concurrent call
        pass


def apply_pending(db: Database, message_id: str, now: datetime | None = None) -> int:
    """Apply the callbacks held for message_id (module docstring), in the order Meta sends them, and remove them.
    The sender calls it right after it saves the id. Returns how many were held."""
    held = sorted(db.mongo[PENDING].find({"message_id": message_id}),
                  key=lambda h: CALLBACK_ORDER.index(h["status"]) if h.get("status") in CALLBACK_ORDER else 99)
    for h in held:
        if h.get("status") in CALLBACK_FIELDS:
            _apply(db, message_id, h["status"], h.get("at") or now or utcnow(), h.get("error_code"))
            _record_pricing(db, message_id, h.get("pricing"))
        db.mongo[PENDING].delete_one({"_id": h["_id"]})
    return len(held)


def record_status(db: Database, status: dict[str, Any], now: datetime) -> str:
    """Record one status callback on its outbox row, once. Returns recorded, duplicate, late (a failure after
    delivery: kept as delivered), held (no row has the message id yet: kept for the sender, module docstring) or
    unknown (not a status we track)."""
    message_id, kind = status.get("id"), status.get("status")
    if not isinstance(message_id, str) or not message_id or len(message_id) > 256 or kind not in CALLBACK_FIELDS:
        return "unknown"
    at = _callback_time(status.get("timestamp"), now)
    code = _error_code(status) if kind == "failed" else None
    pricing = _pricing(status)
    result = _apply(db, message_id, kind, at, code)
    if result != "unknown":
        _record_pricing(db, message_id, pricing)
        return result
    # Hold it, then try once more: the sender may have saved the id in between (module docstring).
    _hold(db, message_id, kind, at, code, now, pricing)
    result = _apply(db, message_id, kind, at, code)
    if result == "unknown":
        return "held"
    db.mongo[PENDING].delete_one({"_id": f"{message_id}:{kind}"})
    _record_pricing(db, message_id, pricing)
    return result


def record_statuses(db: Database, payload: Any, now: datetime | None = None) -> dict[str, int]:
    """Every status callback in a webhook call (Meta batches them), each recorded once. Messages from phones
    (P4.4) go to handle_messages. Returns how many were recorded, duplicate, late, held and unknown."""
    now = now or utcnow()
    counts = {"recorded": 0, "duplicate": 0, "late": 0, "held": 0, "unknown": 0}
    for status in _statuses(payload):
        counts[record_status(db, status, now)] += 1
    return counts



# ---------------------------------------------------------------- replies from shops (P4.4)

INBOUND = "whatsapp_inbound"  # every webhook message id, once
SESSIONS = "whatsapp_sessions"  # per owner: the last message time (the service window) and a stock update in progress
STOCK_CHECKS = "whatsapp_stock_checks"  # one row per shop and IST day the stock check was considered
INBOUND_KEEP = timedelta(days=8)  # Meta retries an unanswered webhook call for up to 7 days
SERVICE_WINDOW = timedelta(hours=24)  # Meta's customer-service window: free-form messages only inside it
PREVIEW_TTL = timedelta(minutes=30)  # a stock preview not answered by then is dropped
MAX_STOCK_LINES = 20
MAX_QUANTITY = 100_000
MAX_SKIPPED = 40  # lines left out, kept for the summary
CHOICES = 3  # candidates offered for an ambiguous line
VIA_WHATSAPP = "via WhatsApp"  # the status-event note of a reservation answered by button
BUTTON_ACTIONS = {"confirm": "confirm", "decline": "reject"}  # quick-reply action -> reservation transition
OPEN_RESERVATION = ("REQUESTED", "CONFIRMED", "READY_FOR_PICKUP")  # the statuses a reservation's timer applies to
YES = frozenset({"yes", "y", "ok", "okay", "haan", "ha", "han", "houdu", "sari", "confirm", "save"})
NO = frozenset({"no", "n", "cancel", "nahi", "illa", "beda"})
_WA_ID = re.compile(r"[1-9]\d{6,14}")
_LOCAL_PHONE = re.compile(r"[6-9]\d{9}")
# One stock line: an item name and a count, either way round ("Tape 20", "Tape: 20", "Tape x 20", "20 Tape").
_UNIT = r"(?:\s*(?:pcs|pc|nos|no|units|unit|qty))?\.?"
_LINE_QTY_LAST = re.compile(rf"^(?P<name>.+?)(?:\s*[:=,\-–]\s*|\s+[x×*]\s*|\s+)(?P<qty>\d{{1,6}}){_UNIT}$", re.I)
_LINE_QTY_FIRST = re.compile(rf"^(?P<qty>\d{{1,6}}){_UNIT}(?:\s*[x×*]\s*|\s*[:=,\-–]\s*|\s+)(?P<name>.+)$", re.I)

HELP_TEXT = words.text("bot_help", "en")  # how to send a stock update; help_text gives it in the owner's language


def help_text(language: str) -> str:
    return words.text("bot_help", language)


def local_phone(wa_id: Any) -> str | None:
    """A WhatsApp id (country code first) as the 10 digits a phone is stored as, or None for any other number."""
    digits = re.sub(r"\D", "", str(wa_id or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if _LOCAL_PHONE.fullmatch(digits) else None


def support_reply() -> str:
    """The reply to a number that is not a shop's verified phone, with the support number when one is set (the
    grievance officer's phone, P3.8: the public contact)."""
    phone = settings.grievance_officer_phone.strip()
    contact = f"call or message NearShop support on {phone}" if phone else "use Help on the NearShop website"
    return (f"Hello, and thank you for your message. This number is not linked to a shop on NearShop, so we could "
            f"not act on it. For help, please {contact}.")


def _messages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    found = []
    for entry in payload.get("entry") or []:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") if isinstance(change, dict) else None
            if isinstance(value, dict):
                found += [m for m in value.get("messages") or [] if isinstance(m, dict)]
    return found


def shop_for_phone(db: Database, phone: str) -> tuple[User, Shop] | None:
    """The owner and shop a message from this 10-digit phone speaks for: the owner's own verified phone (P4.1), or the
    shop's number verified by an admin's call-back (P3.3). The owner's account must be active. V1: one shop per
    owner, the first (as the owner pages take it)."""
    owner = db.users.find_one({"phone": phone, "phone_verified_at": {"$ne": None}, "role": Role.OWNER.value,
                               "is_active": True})
    if owner is not None:
        shop = db.shops.find_one({"owner_id": owner.id}, sort=[("_id", 1)])
        return (owner, shop) if shop is not None else None
    shop = db.shops.find_one({"phone": phone, "phone_verified_at": {"$ne": None}}, sort=[("_id", 1)])
    if shop is None:
        return None
    owner = db.users.get(shop.owner_id)
    if owner is None or not owner.is_active or owner.role != Role.OWNER:
        return None
    return owner, shop


def handle_messages(db: Database, payload: Any, client: WhatsAppClient | None = None,
                    now: datetime | None = None) -> dict[str, int]:
    """Every message in a webhook call (Meta batches them), each handled once (handle_message). Nothing is done while
    WhatsApp is off here (get_client). Returns how many were handled, duplicate and ignored."""
    counts = {"handled": 0, "duplicate": 0, "ignored": 0}
    messages = _messages(payload)
    if not messages:
        return counts
    client = client if client is not None else get_client()
    if client is None:
        counts["ignored"] = len(messages)
        return counts
    now = now or utcnow()
    for message in messages:
        counts[handle_message(db, message, client, now)] += 1
    return counts


def handle_message(db: Database, message: dict[str, Any], client: WhatsAppClient, now: datetime) -> str:
    """Handle one message from a phone (module docstring, "Replies from shops"), once: its id is
    claimed in whatsapp_inbound first, so a replayed or concurrent copy is a duplicate. A failure while acting
    removes the claim and raises, so the webhook answers 500 and Meta's retry handles the message. The reply is sent
    after the action committed, as a free-form text inside the service window; never outside it."""
    message_id, sender = message.get("id"), str(message.get("from") or "")
    if not isinstance(message_id, str) or not message_id or len(message_id) > 256 or not _WA_ID.fullmatch(sender):
        return "ignored"
    at = _callback_time(message.get("timestamp"), now)
    try:
        db.mongo[INBOUND].insert_one({"_id": message_id, "type": str(message.get("type") or "")[:20],
                                      "received_at": now, "expires_at": now + INBOUND_KEEP})
    except DuplicateKeyError:
        return "duplicate"
    try:
        outcome, reply, last_inbound = _respond(db, message, at, now)
    except Exception:
        db.mongo[INBOUND].delete_one({"_id": message_id})
        raise
    sent = _reply(client, sender, reply, last_inbound, now) if reply else None
    db.mongo[INBOUND].update_one({"_id": message_id}, {"$set": {"outcome": outcome[:40], "reply": sent}})
    return "handled"


def _reply(client: WhatsAppClient, to: str, text: str, last_inbound: datetime, now: datetime) -> str:
    """Send a free-form reply when the sender's service window is open (it is for a message just received; a
    webhook call Meta retried a day later finds it closed). Returns sent, failed or window_closed."""
    if now - last_inbound >= SERVICE_WINDOW:
        log.info("WhatsApp reply to %s not sent: the service window has closed", masked(to))
        return "window_closed"
    try:
        delivery = client.send_text(to, text)
    except Exception as exc:  # a client must not raise; a failed reply must not undo the action
        delivery = Delivery(False, CHANNEL, error=type(exc).__name__)
    if not delivery.ok:
        log.warning("WhatsApp reply to %s failed: %s", masked(to), (delivery.error or "failed")[:80])
    return "sent" if delivery.ok else "failed"


def _respond(db: Database, message: dict[str, Any], at: datetime, now: datetime) -> tuple[str, str, datetime]:
    """Act on one message: (outcome, the reply's text, the sender's last message time)."""
    phone = local_phone(message.get("from"))
    found = shop_for_phone(db, phone) if phone else None
    if found is None:
        return "unknown_phone", support_reply(), at
    owner, shop = found
    language = owner.language
    # owner_inbound_at: the owner's own phone's service window, the one the stock check can use (P9.3).
    latest = {"last_inbound_at": at, **({"owner_inbound_at": at} if phone == owner.phone else {})}
    session = db.mongo[SESSIONS].find_one_and_update(
        {"_id": owner.id}, {"$max": latest, "$set": {"updated_at": now}},
        upsert=True, return_document=ReturnDocument.AFTER)
    last_inbound = session.get("last_inbound_at") or at
    kind = message.get("type")
    from app.services import statements  # statements -> notifications -> this module

    if kind in ("button", "interactive"):
        if kind == "button":
            button = message.get("button") if isinstance(message.get("button"), dict) else {}
            payload = str(button.get("payload") or "")
        else:
            interactive = message.get("interactive") if isinstance(message.get("interactive"), dict) else {}
            chosen = interactive.get("button_reply") or interactive.get("list_reply") or {}
            payload = str(chosen.get("id") or "") if isinstance(chosen, dict) else ""
        if payload.startswith(statements.PAYLOAD_PREFIX):  # a statement's quick reply (P9.9)
            outcome, reply = statements.answer_button(db, owner, shop, payload, now)
        else:
            outcome, reply = answer_button(db, owner, shop, payload)
    elif kind == "text":
        text = message.get("text") if isinstance(message.get("text"), dict) else {}
        body = str(text.get("body") or "")
        # A statement reply typed inside the window, or the reason asked for after Not accurate (P9.9); else stock.
        answered = statements.answer_text(db, owner, shop, body, now)
        outcome, reply = answered if answered is not None else answer_text(db, owner, shop, body, now)
    else:
        outcome, reply = "unsupported", help_text(language)
    return outcome, reply, last_inbound


# ---------------------------------------------------------------- Confirm and Decline buttons


def _reservation_for(db: Database, owner: User, payload: str) -> tuple[Reservation | None, str | None]:
    """The reservation a button's payload names (reservation:<code or id>:<action>, or notification:<id>:<action>
    for a notification without a code), and the action; (None, None) for a payload that is not one."""
    parts = payload.split(":")
    if len(parts) != 3 or parts[2] not in BUTTON_ACTIONS:
        return None, None
    entity, ref, action = parts
    if entity == "notification" and ref.isdigit():
        note = db.notifications.get(int(ref))
        subject = subject_of(note) if note is not None and note.user_id == owner.id else None
        entity, _, ref = (subject or ":").partition(":")
    if entity != "reservation" or not ref:
        return None, action
    res = db.reservations.get(int(ref)) if ref.isdigit() else db.reservations.find_one({"code": ref.upper()})
    return res, action


def answer_button(db: Database, owner: User, shop: Shop, payload: str) -> tuple[str, str]:
    """A Confirm or Decline button on the new-reservation template: the transition, as the owner, with the note
    "via WhatsApp" on its status event, all in one transaction. Returns (outcome, reply), the reply in the owner's
    language."""
    from app.services.reservations import transition  # reservations -> notifications -> this module

    language = owner.language
    res, action = _reservation_for(db, owner, payload)
    if action is None:
        return "bad_button", words.text("bot_bad_button", language)
    if res is None or db.shops.get(res.shop_id).owner_id != owner.id:
        return "not_found", words.text("bot_reservation_not_found", language)
    to = BUTTON_ACTIONS[action]
    if res.status in OPEN_RESERVATION and res.expires_at <= utcnow():
        # Its timer ran out before the sweep reached it (a button can be tapped hours later): expire it first, as
        # the sweep would, so the owner's answer finds it expired.
        try:
            transition(db, res.id, "expire", None)
        except AppError:
            pass

    def run() -> Reservation:
        done = transition(db, res.id, to, owner)
        db.mongo["status_events"].find_one_and_update(
            {"entity": "reservation", "entity_id": res.id, "to_status": done.status.value, "actor_id": owner.id},
            {"$set": {"note": VIA_WHATSAPP}}, sort=[("_id", -1)], session=db.session)
        return done

    try:
        done = db.transaction(run)
    except AppError as exc:
        hint = words.text("bot_hint_insufficient_stock", language) if exc.code == "insufficient_stock" else ""
        return f"{to}_refused", words.text("bot_not_confirmed" if to == "confirm" else "bot_not_declined", language,
                                     {"code": res.code, "error": exc.message.rstrip("."), "hint": hint})
    product = db.products.get(done.product_id)
    params = {"code": done.code, "quantity": done.quantity, "minutes": done.hold_minutes,
              "product": product.name if product else words.Term("the_item")}
    if to == "confirm":
        return "confirmed", words.text("bot_confirmed", language, params)
    return "declined", words.text("bot_declined", language, params)


# ---------------------------------------------------------------- stock updates


def parse_stock_lines(text: str) -> tuple[list[tuple[str, int]], list[str]]:
    """The (item name, count) lines of a stock message, and the lines that are not one. A name needs a letter."""
    lines, unread = [], []
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip(" .;")
        if not line:
            continue
        match = _LINE_QTY_LAST.match(line) or _LINE_QTY_FIRST.match(line)
        name = match.group("name").strip(" -:,") if match else ""
        if not match or not re.search(r"[^\W\d_]", name) or int(match.group("qty")) > MAX_QUANTITY:
            unread.append(line[:80])
            continue
        lines.append((name[:120], int(match.group("qty"))))
    return lines, unread


def match_line(name: str, listings: list[Product]) -> tuple[str, list[Product]]:
    """Match one line's name to the shop's listings with the importer's matcher and thresholds
    (app/services/importer.py): ("match", [listing]) for an exact name or a single confident match (AUTO_MATCH),
    ("ambiguous", up to CHOICES candidates) when it is less sure (at least SUGGEST_MATCH before the importer's
    two-word cap), and ("unknown", []) otherwise."""
    from app.services import importer  # importer -> inventory; kept off this module's import path

    key = importer.normalize_name(name)
    exact = [p for p in listings if importer.normalize_name(p.name) == key]
    if len(exact) == 1:
        return "match", exact
    by_id = {p.id: p for p in listings}
    keys = {p.id: importer.normalize_name(f"{p.brand or ''} {p.name}") for p in listings}
    ranked = importer.best_matches(name, keys, limit=CHOICES + 1)
    confident = [pid for pid, raw in ranked if importer.capped_score(name, raw) >= importer.AUTO_MATCH]
    if len(confident) == 1:
        return "match", [by_id[confident[0]]]
    candidates = [by_id[pid] for pid, raw in ranked if raw >= importer.SUGGEST_MATCH][:CHOICES]
    return ("ambiguous", candidates) if candidates else ("unknown", [])


def _item(product: Product, quantity: int) -> dict[str, Any]:
    return {"product_id": product.id, "name": product.name[:120], "quantity": quantity, "before": product.quantity}


def _add_item(pending: dict[str, Any], item: dict[str, Any]) -> None:
    pending["items"] = [i for i in pending["items"] if i["product_id"] != item["product_id"]] + [item]


def _session_pending(db: Database, owner: User, shop: Shop, now: datetime) -> dict[str, Any] | None:
    session = db.mongo[SESSIONS].find_one({"_id": owner.id}, {"pending": 1}) or {}
    pending = session.get("pending")
    if not isinstance(pending, dict) or pending.get("shop_id") != shop.id or pending.get("expires_at", now) <= now:
        return None
    return pending


def _save_pending(db: Database, owner: User, pending: dict[str, Any] | None, now: datetime) -> None:
    if pending is not None:
        pending["expires_at"] = now + PREVIEW_TTL
        pending["skipped"] = pending["skipped"][:MAX_SKIPPED]
    db.mongo[SESSIONS].update_one({"_id": owner.id}, {"$set": {"pending": pending, "updated_at": now}},
                                  session=db.session)


def preview_text(pending: dict[str, Any], language: str = "en") -> str:
    """The bot's reply for a stock update in progress: the next ambiguous line's choices, or the preview to confirm,
    in the owner's language."""
    parts = []
    if pending["items"]:
        parts.append(words.text("preview_matched_so_far" if pending["queue"] else "preview_update_to", language) + "\n"
                     + "\n".join(words.text("preview_item", language, i) for i in pending["items"]))
    if pending["skipped"]:
        parts.append(words.text("preview_skipped", language, {"lines": "; ".join(pending["skipped"])}))
    if pending["queue"]:
        line = pending["queue"][0]
        options = "\n".join(f"{n}. {c['name']}" for n, c in enumerate(line["candidates"], start=1))
        parts.append(words.text("preview_which", language,
                          {"line": line["line"], "quantity": line["quantity"], "options": options}))
    elif pending["items"]:
        parts.append(words.text("preview_confirm", language))
    else:
        parts.append(words.text("preview_nothing", language))
    return "\n\n".join(parts)


def answer_text(db: Database, owner: User, shop: Shop, message: str, now: datetime) -> tuple[str, str]:
    """A text from a shop: Yes or No to the stock preview, a number choosing among an ambiguous line's candidates, or
    new stock lines (which replace any preview in progress). Returns (outcome, reply), the reply in the owner's
    language."""
    language = owner.language
    word = " ".join(message.lower().split()).strip(" .!")
    pending = _session_pending(db, owner, shop, now)
    if pending and pending["queue"] and word.isdecimal():  # not isdigit: int() refuses "²"
        return choose(db, owner, pending, int(word), now)
    if word in YES:
        if pending and pending["queue"]:
            return "choices", words.text("bot_choose_first", language) + "\n\n" + preview_text(pending, language)
        if pending and pending["items"]:
            return apply_stock(db, owner, shop, pending["token"], now)
        return "nothing_to_confirm", words.text("bot_nothing_to_confirm", language) + "\n\n" + help_text(language)
    if word in NO:
        if pending:
            _save_pending(db, owner, None, now)
            return "cancelled", words.text("bot_cancelled", language)
        return "help", help_text(language)
    lines, unread = parse_stock_lines(message)
    if not lines:
        return "help", help_text(language)
    if len(lines) > MAX_STOCK_LINES:
        return "too_many_lines", words.text("bot_too_many_lines", language, {"max": MAX_STOCK_LINES})
    listings = db.products.find({"shop_id": shop.id, "is_active": True})  # never a removed listing
    pending = {"token": uuid.uuid4().hex, "shop_id": shop.id, "items": [], "queue": [],
               "skipped": [words.text("preview_no_count", language, {"line": u}) for u in unread][:10]}
    for name, quantity in lines:
        result, found = match_line(name, listings)
        if result == "match":
            _add_item(pending, _item(found[0], quantity))
        elif result == "ambiguous":
            pending["queue"].append({"line": name, "quantity": quantity,
                                     "candidates": [{"product_id": p.id, "name": p.name[:120]} for p in found]})
        else:
            pending["skipped"].append(name)
    _save_pending(db, owner, pending if pending["items"] or pending["queue"] else None, now)
    return ("choices" if pending["queue"] else "preview"), preview_text(pending, language)


def choose(db: Database, owner: User, pending: dict[str, Any], number: int, now: datetime) -> tuple[str, str]:
    """The answer to an ambiguous line: 1 to 3 picks a candidate, 0 leaves the line out."""
    language = owner.language
    line = pending["queue"][0]
    if not 0 <= number <= len(line["candidates"]):
        return "choices", (words.text("bot_choose_number", language, {"count": len(line["candidates"])}) + "\n\n"
                           + preview_text(pending, language))
    pending["queue"].pop(0)
    if number == 0:
        pending["skipped"].append(line["line"])
    else:
        product = db.products.get(line["candidates"][number - 1]["product_id"])
        if product is None or not product.is_active:  # removed since the choices were offered
            pending["skipped"].append(line["line"])
        else:
            _add_item(pending, _item(product, line["quantity"]))
    _save_pending(db, owner, pending, now)
    return ("choices" if pending["queue"] else "preview"), preview_text(pending, language)


def apply_stock(db: Database, owner: User, shop: Shop, token: str, now: datetime) -> tuple[str, str]:
    """Yes: set each previewed count with set_stock, reason whatsapp_update and the owner as the actor, and clear
    the preview, in one transaction. The preview is taken by its token inside the transaction, so two Yes messages
    apply it once."""
    from app.services.inventory import set_stock

    def run() -> list[tuple[str, int]] | None:
        session = db.mongo[SESSIONS].find_one_and_update(
            {"_id": owner.id, "pending.token": token}, {"$set": {"pending": None, "updated_at": now}},
            session=db.session)
        if session is None:
            return None
        saved = []
        for item in session["pending"]["items"]:
            product = db.products.get(item["product_id"])
            if product is None or product.shop_id != shop.id or not product.is_active:
                continue
            set_stock(db, product, item["quantity"], owner.id, InventoryReason.WHATSAPP_UPDATE)
            saved.append((product.name, item["quantity"]))
        return saved

    saved = db.transaction(run)
    language = owner.language
    if saved is None:
        return "nothing_to_confirm", words.text("bot_nothing_to_confirm", language) + "\n\n" + help_text(language)
    if not saved:
        return "saved", words.text("bot_items_gone", language)
    return "saved", words.text("bot_saved", language) + "\n" + "\n".join(
        words.text("bot_saved_item", language, {"name": name, "quantity": qty}) for name, qty in saved)


# ---------------------------------------------------------------- the daily stock check

STOCK_CHECK_KIND = "stock_check"
STALE_STOCK = timedelta(hours=48)  # listings counted longer ago than this are listed
STOCK_CHECK_ITEMS = 5
STOCK_CHECK_GRACE = timedelta(hours=3)  # sent from the opening time until this long after it
STOCK_CHECK_KEEP = timedelta(days=3)
WINDOW_MARGIN = timedelta(minutes=30)  # the window must stay open this long after the daily check (P9.3)
DEFAULT_OPENING = dtime(9, 0)  # when a shop's opening hours cannot be read
IST = ZoneInfo("Asia/Kolkata")
_TIME_12H = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s*m\b\.?", re.I)
_TIME_24H = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")


def opening_time(hours: str | None) -> dtime:
    """The opening time in a shop's free-text opening hours ("9:30 AM - 9:00 PM", "10am-8pm", "09:00-21:00"): the
    first time written in it, or DEFAULT_OPENING."""
    text = hours or ""
    if match := _TIME_12H.search(text):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if 1 <= hour <= 12 and minute < 60:
            return dtime(hour % 12 + (12 if match.group(3).lower() == "p" else 0), minute)
    if match := _TIME_24H.search(text):
        return dtime(int(match.group(1)), int(match.group(2)))
    return DEFAULT_OPENING


def _days_ago(then: datetime, now: datetime) -> int:
    return max(2, int((now - then).total_seconds() // 86400))


STOCK_CHECK_NAME = 60  # characters of a listing's name in the stock check, shortened further to fit PARAM_MAX
STOCK_CHECK_NAME_MIN = 12


def stock_check_params(shop: dict[str, Any], stale: list[dict[str, Any]], now: datetime,
                       language: str = "en") -> dict[str, Any]:
    """The parameters of the stock_check message (app/services/notification_text.py) in the owner's language: the
    shop's name and the listings with their counts, oldest count first. Long names are shortened until the rendered
    text fits one template variable (PARAM_MAX), so no listing is cut off the end."""
    shop_name = shop.get("name") or words.Term("your_shop")

    def items(cap: int) -> str:
        def name(p: dict[str, Any]) -> str:
            flat = " ".join(str(p.get("name") or "").split())
            return flat if len(flat) <= cap else flat[:cap - 1].rstrip() + "…"

        return "; ".join(words.text("stock_check_item", language, {"name": name(p), "quantity": p["quantity"],
                                                             "days": _days_ago(p["stock_updated_at"], now)})
                         for p in stale)

    def body(cap: int) -> str:
        return words.render(STOCK_CHECK_KIND, language, {"shop": shop_name, "items": items(cap)})[1] or ""

    cap = STOCK_CHECK_NAME
    while len(body(cap)) > PARAM_MAX and cap > STOCK_CHECK_NAME_MIN:
        cap -= 1
    return {"shop": shop_name, "items": items(cap)}


def window_closes_at(db: Database, owner_id: int) -> datetime | None:
    """When the owner's own phone's 24-hour service window closes: SERVICE_WINDOW after their last message from it
    (owner_inbound_at, written by _respond), or None when there was none. A message from before the owner's phone was
    last verified came from another number, or one not proven theirs, and opens nothing."""
    session = db.mongo[SESSIONS].find_one({"_id": owner_id}, {"owner_inbound_at": 1})
    last = session.get("owner_inbound_at") if session else None
    if last is None:
        return None
    user = db.users.raw.find_one({"_id": owner_id}, {"phone_verified_at": 1})
    verified = user.get("phone_verified_at") if user else None
    if verified is None or verified > last:
        return None
    return last + SERVICE_WINDOW


def window_open(db: Database, owner_id: int, now: datetime, margin: timedelta = timedelta(0)) -> bool:
    """The owner's service window is open now and stays open for at least margin."""
    closes = window_closes_at(db, owner_id)
    return closes is not None and closes > now and closes - now >= margin


def in_window_text(row: dict[str, Any]) -> str:
    """The free-form text of an outbox row sent inside the window (P9.3): its title and text, as the template shows
    them, then how to reply, in the recipient's language. A statement (P9.9) is its template's words with its
    variables, ending with the words to reply with (its buttons cannot be sent with a free-form text)."""
    language = words.language_of(row.get("language"))
    if (names := STATEMENT_VARIABLES.get(row.get("kind") or "")) is not None:
        values = [*(row.get("params") or []), *["-"] * len(names)][:len(names)]
        params = dict(zip(names, values, strict=True))
        params["amount_due"] = words.statement_text("amount_due", language)
        return words.statement_text(STATEMENT_TEXT_KEYS[row["kind"]], language, params)[:TEXT_MAX]
    title, body = ([*(row.get("params") or []), "-", "-"])[:2]
    return f"NearShop: {title}.\n{body}\n\n{help_text(language)}"[:TEXT_MAX]


def closed_weekday(closed_on: str | None) -> int | None:
    """The weekday (Monday 0) a shop's closed_on names ("Sunday", "sundays", "Sun"), or None."""
    text = (closed_on or "").strip().lower().rstrip(".")
    for number, name in enumerate(WEEKDAYS):
        if text and text in (name.lower(), name.lower() + "s", name[:3].lower()):
            return number
    return None


def template_weekday(shop: dict[str, Any], config: Settings = settings) -> int:
    """The weekday a shop without an open window gets the stock-check template (P9.3): the configured weekly day
    (NEARSHOP_STOCK_CHECK_WEEKLY_DAY), or the day after when the shop is closed on it."""
    day = WEEKDAYS.index(str(config.stock_check_weekly_day).strip().capitalize())
    return (day + 1) % 7 if closed_weekday(shop.get("closed_on")) == day else day


def send_stock_checks(db: Database, now: datetime | None = None, limit: int = BATCH) -> dict[str, Any]:
    """The stock check (P4.4, cadence P9.3; module docstring): from each active shop's opening time (IST) until
    STOCK_CHECK_GRACE after it, a shop's day is claimed in whatsapp_stock_checks, once, and decided:

      * closed: the shop's closed day (closed_on); nothing is sent;
      * fresh: no active listing counted more than 48 hours ago;
      * unreachable: the owner cannot get WhatsApp messages (verified phone and consent);
      * in_window: the owner's own service window stays open for WINDOW_MARGIN or more: the check goes as a free-form
        text inside it, every day;
      * template: otherwise, on the shop's weekly day (template_weekday), the stock-check template;
      * waiting: otherwise nothing today.

    A check goes as a notification of kind stock_check (in the app and, through the outbox, on WhatsApp) listing up
    to STOCK_CHECK_ITEMS of those listings, oldest first, and its outbox row records the route and category. Only
    while WhatsApp is on here. A failure after the claim releases it and raises, so a later tick tries the shop again.
    Returns the counts of shops sent (in_window plus template), and of each decision."""
    if get_client() is None:
        return {"enabled": False}
    from app.services.notifications import notify  # notifications -> this module

    now = now or utcnow()
    local = now.replace(tzinfo=UTC).astimezone(IST)
    day = local.date().isoformat()
    counts = {"sent": 0, ROUTE_IN_WINDOW: 0, ROUTE_TEMPLATE: 0, "fresh": 0, "unreachable": 0, "closed": 0,
              "waiting": 0}
    considered = 0
    done = {d["shop_id"] for d in db.mongo[STOCK_CHECKS].find({"day": day}, {"shop_id": 1})}
    template = TEMPLATES[STOCK_CHECK_KIND]
    for shop in db.shops.find_raw({"is_active": True},
                                  {"_id": 1, "owner_id": 1, "name": 1, "opening_hours": 1, "closed_on": 1},
                                  sort=[("_id", 1)]):
        if shop["_id"] in done:
            continue
        opens = datetime.combine(local.date(), opening_time(shop.get("opening_hours")), IST)
        if not opens <= local < opens + STOCK_CHECK_GRACE:
            continue
        if considered >= limit:
            break
        claim = f"{shop['_id']}:{day}"
        try:
            db.mongo[STOCK_CHECKS].insert_one({"_id": claim, "shop_id": shop["_id"], "day": day,
                                               "at": now, "expires_at": now + STOCK_CHECK_KEEP})
        except DuplicateKeyError:
            continue
        considered += 1
        route = None
        try:
            if closed_weekday(shop.get("closed_on")) == local.weekday():
                decision = "closed"
            else:
                stale = db.products.find_raw(
                    {"shop_id": shop["_id"], "is_active": True, "stock_updated_at": {"$lt": now - STALE_STOCK}},
                    {"name": 1, "quantity": 1, "stock_updated_at": 1}, sort=[("stock_updated_at", 1), ("_id", 1)],
                    limit=STOCK_CHECK_ITEMS)
                if not stale:
                    decision = "fresh"
                elif _recipient(db, shop["owner_id"], template)[0] is None:
                    decision = "unreachable"
                elif window_open(db, shop["owner_id"], now, WINDOW_MARGIN):
                    decision = route = ROUTE_IN_WINDOW
                elif local.weekday() == template_weekday(shop):
                    decision = route = ROUTE_TEMPLATE
                else:
                    decision = "waiting"
                if route is not None:
                    params = stock_check_params(shop, stale, now, words.recipient_language(db, shop["owner_id"]))
                    db.transaction(lambda s=shop, p=params, r=route: _notify_stock_check(db, notify, s, p, r))
        except Exception:
            # Release the day's claim, so the next tick (still inside the grace period) tries this shop again
            # instead of skipping it for the rest of the day; the job run fails and is retried.
            db.mongo[STOCK_CHECKS].delete_one({"_id": claim})
            raise
        outcome = "sent" if route else decision if decision in ("fresh", "unreachable") else None
        db.mongo[STOCK_CHECKS].update_one({"_id": claim}, {"$set": {"outcome": outcome, "route": route,
                                                                    "decision": decision}})
        counts[decision] += 1
        if route:
            counts["sent"] += 1
    return {"enabled": True, "day": day, **counts}


def _notify_stock_check(db: Database, notify: Callable[..., Notification], shop: dict[str, Any],
                        params: dict[str, Any], route: str) -> None:
    """Inside the transaction: the stock-check notification, and its outbox row's route and category (enqueue writes
    the template route; inside the window it is a free-form text, Meta's service category). Only the row written for
    this notification is changed, never an older queued stock check of the same owner."""
    note = notify(db, shop["owner_id"], STOCK_CHECK_KIND, link="/shop/inventory", message=STOCK_CHECK_KIND,
                  params=params)
    if route == ROUTE_IN_WINDOW:
        db.mongo[OUTBOX].update_one(
            {"notification_id": note.id, "kind": STOCK_CHECK_KIND, "status": "queued"},
            {"$set": {"route": ROUTE_IN_WINDOW, "category": CATEGORY_SERVICE}}, session=db.session)
