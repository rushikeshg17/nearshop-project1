"""Phone one-time codes (P4.1; plan sections 8.3, 8.4 and 8.10).

A code is 6 digits, valid for 5 minutes, for at most 5 attempts and once. It is stored only as a keyed hash (HMAC
with NEARSHOP_SECRET_KEY) in otp_codes, one document per phone and purpose, whose _id is itself a keyed hash, so a
new request replaces the previous code and the phone number is never stored; the TTL index removes the document 10
minutes after it was created. Every send is logged in otp_sends (the phone's keyed hash, channel, cost estimate in
rupees and outcome), kept 30 days.

Sending tries the providers of app/services/messaging.py in order (WhatsApp, then SMS; the console in development
and test). Phone sign-in is available only while there is one (available()); otherwise every code request answers
503 otp_unavailable, and GET /api/auth/phone-signin says so.

The answer to a code request never depends on whether an account uses the phone: a code is sent either way, and
the purpose is checked against accounts only once a correct code proves the phone (app/api/routes/auth.py).

Limits. The per-phone and per-IP limits are rows of the rate-limit table (app/core/ratelimit.py). Two live here:
  * 5 attempts per code. The 5th wrong code locks it (429 otp_locked); only a new code helps.
  * the global daily cap, NEARSHOP_OTP_DAILY_CAP sends per IST day, counted in otp_daily with one atomic $inc per
    request. Reaching 80% of it reports a warning event to the error tracker, once a day (the OTP-spend alert of
    plan section 8.10 fires on it). Beyond the cap requests answer 503 otp_unavailable until the next day.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import AppError
from app.services import messaging

log = logging.getLogger("nearshop.otp")

CODE_DIGITS = 6
CODE_TTL = timedelta(minutes=messaging.CODE_MINUTES)
MAX_ATTEMPTS = 5
RESEND_AFTER_SECONDS = 60  # the rate-limit table's 1 send per phone per 60 s, which the screens count down
WARN_FRACTION = 0.8  # the OTP-spend alert: 80% of the daily cap
DAILY_KEEP = timedelta(days=3)  # an otp_daily document outlives its day by a little, then the TTL removes it
IST = ZoneInfo("Asia/Kolkata")
SPEND_WARNING = "OTP sends reached 80% of the daily cap"  # the error tracker groups the alert on this text

CODES, SENDS, DAILY = "otp_codes", "otp_sends", "otp_daily"

now: Callable[[], datetime] = utcnow  # tests replace it to move past an expiry or into another day


# ---------------------------------------------------------------- errors


def unavailable() -> AppError:
    return AppError("Signing in with a phone code is not available right now. Please sign in with your email "
                    "and password, or try again later.", code="otp_unavailable", status_code=503)


def _send_failed() -> AppError:
    return AppError("We could not send the code just now. Please try again in a minute.", code="otp_send_failed",
                    status_code=503)


def _expired() -> AppError:
    return AppError("This code has expired or has already been used. Ask for a new code.", code="otp_expired")


def _locked() -> AppError:
    return AppError("Too many wrong codes. Ask for a new code.", code="otp_locked", status_code=429)


def _wrong(left: int) -> AppError:
    return AppError(f"That code is not right. {left} attempt{'s' if left != 1 else ''} left.", code="invalid_code",
                    status_code=401)


# ---------------------------------------------------------------- keys and hashes


def _key(*parts: str) -> str:
    return hmac.new(settings.secret_key.encode(), "\x1f".join(("otp", *parts)).encode(), hashlib.sha256).hexdigest()


def phone_hash(phone: str) -> str:
    return _key("phone", phone)


def code_id(phone: str, purpose: str) -> str:
    return _key("code", phone, purpose)


def code_hash(doc_id: str, code: str) -> str:
    return _key("value", doc_id, code)


def new_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def today() -> str:
    """The IST calendar day of now(), YYYY-MM-DD: the daily cap's day."""
    return now().replace(tzinfo=ZoneInfo("UTC")).astimezone(IST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- availability


def available() -> bool:
    """Whether phone sign-in works here: a provider to send through (messaging.otp_providers)."""
    return bool(messaging.otp_providers())


def status() -> dict[str, Any]:
    """GET /api/auth/phone-signin: whether the phone screens should be shown (P4.2), and the code's rules."""
    providers = messaging.otp_providers()
    return {"available": bool(providers), "channels": [p.channel for p in providers], "code_length": CODE_DIGITS,
            "expires_in": int(CODE_TTL.total_seconds()), "resend_after": RESEND_AFTER_SECONDS}


# ---------------------------------------------------------------- the daily cap


def report_warning(message: str, **extra: Any) -> None:
    """A warning event for the error tracker (Sentry, when it is on) and the log."""
    log.warning("%s (%s)", message, ", ".join(f"{k}={v}" for k, v in extra.items()))
    try:
        import sentry_sdk

        if sentry_sdk.get_client().is_active():
            sentry_sdk.capture_message(message, level="warning", fingerprint=["otp-spend-warning"],
                                       tags={"alert": "otp_spend"}, extras=extra)
    except Exception as exc:  # the tracker must never stop a code from being sent
        log.warning("OTP spend warning not reported to the error tracker: %s", type(exc).__name__)


def _reserve_send(db: Database) -> None:
    """Count one send against today's cap, or raise otp_unavailable when the cap is spent. Reports the 80%
    warning once a day: only the request whose atomic update sets warned_at reports it."""
    cap = settings.otp_daily_cap
    day, at = today(), now()
    daily = db.mongo[DAILY]
    update = {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": at + DAILY_KEEP}}
    try:
        doc = daily.find_one_and_update({"_id": day}, update, upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:  # two first sends of the day raced on the upsert; the second one updates
        doc = daily.find_one_and_update({"_id": day}, update, upsert=True, return_document=ReturnDocument.AFTER)
    count = doc["count"]
    if count > cap:
        daily.update_one({"_id": day, "count": {"$gt": 0}}, {"$inc": {"count": -1}})
        log.error("OTP daily cap of %d reached for %s: code request refused", cap, day)
        raise unavailable()
    if count >= math.ceil(cap * WARN_FRACTION) and doc.get("warned_at") is None:
        if daily.update_one({"_id": day, "warned_at": None}, {"$set": {"warned_at": at}}).modified_count == 1:
            report_warning(SPEND_WARNING, day=day, sends=count, cap=cap)


def _log_send(db: Database, phone: str, purpose: str, outcome: str, channel: str | None = None,
              cost: float = 0.0, error: str | None = None) -> None:
    db.mongo[SENDS].insert_one({"phone_hash": phone_hash(phone), "purpose": purpose, "channel": channel,
                                "outcome": outcome, "cost_estimate": round(cost, 4), "error": error, "at": now()})


# ---------------------------------------------------------------- send and verify


def request_code(db: Database, phone: str, purpose: str, *, user_id: int | None = None) -> dict[str, Any]:
    """Send a new code for (phone, purpose), replacing any earlier one. The answer is the same for every phone.
    user_id: the signed-in user a verify code is for (only that user can then use it)."""
    providers = messaging.otp_providers()
    if not providers:
        raise unavailable()
    try:
        _reserve_send(db)
    except AppError:
        _log_send(db, phone, purpose, "capped")
        raise
    code, doc_id, at = new_code(), code_id(phone, purpose), now()
    db.mongo[CODES].replace_one({"_id": doc_id}, {
        "phone_hash": phone_hash(phone), "purpose": purpose, "code_hash": code_hash(doc_id, code), "user_id": user_id,
        "attempts": 0, "created_at": at, "expires_at": at + CODE_TTL, "used_at": None}, upsert=True)
    for provider in providers:
        delivery = provider.send_otp(phone, code)
        _log_send(db, phone, purpose, "sent" if delivery.ok else "failed", delivery.channel, delivery.cost_estimate,
                  delivery.error)
        if delivery.ok:
            return {"ok": True, "channel": delivery.channel, "code_length": CODE_DIGITS,
                    "expires_in": int(CODE_TTL.total_seconds()), "resend_after": RESEND_AFTER_SECONDS}
    db.mongo[CODES].delete_one({"_id": doc_id, "code_hash": code_hash(doc_id, code)})  # nobody received it
    raise _send_failed()


def verify_code(db: Database, phone: str, purpose: str, code: str, *, user_id: int | None = None) -> None:
    """Spend the code for (phone, purpose), or raise: 401 invalid_code (attempts left), 429 otp_locked after
    MAX_ATTEMPTS wrong codes, 400 otp_expired when there is no live code (expired, used, never sent, or sent for
    another signed-in user). Each attempt is counted before the code is compared, with one atomic update, so
    concurrent guesses cannot exceed the attempts; a correct code is marked used by one conditional update, so
    two requests cannot both spend it."""
    doc_id, at = code_id(phone, purpose), now()
    codes = db.mongo[CODES]
    live = {"_id": doc_id, "used_at": None, "expires_at": {"$gt": at}}
    doc = codes.find_one_and_update({**live, "attempts": {"$lt": MAX_ATTEMPTS}}, {"$inc": {"attempts": 1}},
                                    return_document=ReturnDocument.AFTER)
    if doc is None:
        raise _locked() if codes.count_documents(live, limit=1) else _expired()
    if doc.get("user_id") != user_id:
        raise _expired()
    if not hmac.compare_digest(doc["code_hash"], code_hash(doc_id, code)):
        left = MAX_ATTEMPTS - doc["attempts"]
        raise _locked() if left <= 0 else _wrong(left)
    if codes.update_one({**live, "code_hash": doc["code_hash"]}, {"$set": {"used_at": at}}).modified_count != 1:
        raise _expired()
