"""Password hashing (Argon2), revocable JWT sessions and the admin second factor (TOTP).

Sessions (P2.4). Every sign-in creates a session: a random id, recorded on the user document in `sessions`
(at most MAX_SESSIONS, the oldest dropped first) and carried in the token as `sid`, with the user's
`token_version` as `ver`. app/core/deps.py accepts a token only while its version equals the user's and its
session is still on the user document, which it reads on every request anyway, so the check costs no extra
database round trip. Logout removes one session; revoke_all_update() (sign-out-everywhere, password change,
phone change, deactivation) bumps the version and clears the list, which ends every session at once.

Second factor. Admins confirm sign-in with a TOTP code (RFC 6238: HMAC-SHA1, 30-second step, 6 digits) or a
single-use recovery code. The TOTP secret is stored encrypted with NEARSHOP_TOTP_KEY (a Fernet key; several
comma-separated keys decrypt, the first encrypts, so the key can be rotated); recovery codes are stored as
SHA-256 digests. Enrolment happens only through database/admin_tasks.py create-admin.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import jwt
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pwdlib import PasswordHash
from pydantic import SecretStr
from pydantic_settings import BaseSettings

from app.core.config import Settings, settings
from app.core.database import utcnow

_hasher = PasswordHash.recommended()
ALGORITHM = "HS256"

MAX_SESSIONS = 10  # active sessions kept per user; the 11th sign-in drops the oldest
ADMIN_SESSION_MINUTES = 12 * 60
LAST_SEEN_EVERY = timedelta(hours=1)  # last_seen_at is written at most this often per session
DEVICE_LABEL_MAX = 60

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # steps either side of now that are accepted, for clock drift
TOTP_ISSUER = "NearShop"
TOTP_MAX_FAILURES = 5  # wrong codes in a row before admin sign-in locks
TOTP_LOCK = timedelta(minutes=15)
RECOVERY_CODES = 10


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _hasher.verify(password, password_hash)


# ---------------------------------------------------------------- session tokens
def session_minutes(role: str) -> int:
    """How long a session lasts: 12 hours for admins, access_token_minutes for everyone else."""
    return min(ADMIN_SESSION_MINUTES, settings.access_token_minutes) if role == "admin" else settings.access_token_minutes


def new_session_id() -> str:
    return secrets.token_urlsafe(16)


def create_access_token(user_id: int, role: str, *, version: int = 0, session_id: str | None = None,
                        minutes: int | None = None) -> str:
    """A signed token for one session. deps refuses it unless `session_id` is one of the user's recorded
    sessions and `version` is the user's current token_version."""
    now = utcnow()
    payload = {
        "sub": str(user_id),
        "role": role,
        "ver": version,
        "iat": now,
        "exp": now + timedelta(minutes=minutes if minutes is not None else session_minutes(role)),
    }
    if session_id is not None:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def revoke_all_update(extra: dict | None = None) -> dict:
    """The users update that ends every session of a user: a new token_version and no recorded sessions.
    `extra` adds fields to the same $set (a new password hash, phone or role, is_active)."""
    return {"$inc": {"token_version": 1},
            "$set": {"sessions": [], "updated_at": utcnow(), **(extra or {})}}


_BROWSERS = (("edg/", "Edge"), ("opr/", "Opera"), ("samsungbrowser", "Samsung Internet"), ("firefox", "Firefox"),
             ("fxios", "Firefox"), ("crios", "Chrome"), ("chrome", "Chrome"), ("safari", "Safari"))
_SYSTEMS = (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("windows", "Windows"),
            ("mac os", "Mac"), ("cros", "ChromeOS"), ("linux", "Linux"))


def device_label(user_agent: str | None) -> str:
    """A short, coarse label for a session ("Chrome on Android"), never the raw user agent."""
    ua = (user_agent or "").lower()
    browser = next((name for key, name in _BROWSERS if key in ua), None)
    system = next((name for key, name in _SYSTEMS if key in ua), None)
    if browser and system:
        label = f"{browser} on {system}"
    else:
        label = browser or system or ("Command-line client" if re.match(r"(curl|python|httpx|wget)", ua)
                                      else "Unknown device")
    return label[:DEVICE_LABEL_MAX]


def new_session(user_agent: str | None, role: str) -> dict:
    """The record of one session, as stored in users.sessions."""
    now = utcnow()
    return {"id": new_session_id(), "created_at": now, "last_seen_at": now,
            "expires_at": now + timedelta(minutes=session_minutes(role)), "device": device_label(user_agent)}


def add_session_pipeline(session: dict, extra: dict | None = None) -> list[dict]:
    """An update pipeline that drops expired sessions, appends `session` and keeps the newest MAX_SESSIONS,
    in one atomic write (with `extra` fields set alongside)."""
    now = session["created_at"]
    kept = {"$filter": {"input": {"$ifNull": ["$sessions", []]}, "cond": {"$gt": ["$$this.expires_at", now]}}}
    literal = {k: {"$literal": v} for k, v in (extra or {}).items()}
    return [{"$set": {"sessions": {"$slice": [{"$concatArrays": [kept, {"$literal": [session]}]}, -MAX_SESSIONS]},
                      "token_version": {"$ifNull": ["$token_version", 0]}, **literal}}]


# ---------------------------------------------------------------- TOTP (RFC 6238)
class TotpNotConfigured(RuntimeError):
    """NEARSHOP_TOTP_KEY is missing or is not a valid Fernet key."""


class _TotpKeySettings(BaseSettings):
    """NEARSHOP_TOTP_KEY, from the environment or backend/.env, read the way app.core.config reads settings."""

    model_config = Settings.model_config
    totp_key: SecretStr = SecretStr("")


def generate_totp_key() -> str:
    """A new value for NEARSHOP_TOTP_KEY."""
    return Fernet.generate_key().decode()


def _cipher() -> MultiFernet:
    keys = [k.strip() for k in _TotpKeySettings().totp_key.get_secret_value().split(",") if k.strip()]
    if not keys:
        raise TotpNotConfigured("NEARSHOP_TOTP_KEY is not set, so admin second factors cannot be used. Generate "
                                "one with: uv run python -c \"from app.core.security import generate_totp_key as g; "
                                "print(g())\"")
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except ValueError as exc:
        raise TotpNotConfigured("NEARSHOP_TOTP_KEY must be a Fernet key (32 url-safe base64-encoded bytes), "
                                "or several separated by commas") from exc


def totp_configured() -> bool:
    try:
        _cipher()
    except TotpNotConfigured:
        return False
    return True


def new_totp_secret() -> str:
    """A random 160-bit secret, base32 without padding, as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def encrypt_totp_secret(secret: str) -> str:
    return _cipher().encrypt(secret.encode()).decode()


def decrypt_totp_secret(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise TotpNotConfigured("The stored TOTP secret does not decrypt with NEARSHOP_TOTP_KEY") from exc


def totp_at(secret: str, step: int, digits: int = TOTP_DIGITS, digest: str = "sha1") -> str:
    """The code for one time step (RFC 4226 HOTP over the step counter)."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def current_step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // TOTP_STEP)


def match_totp(secret: str, code: str, *, at: float | None = None) -> int | None:
    """The time step `code` belongs to, within TOTP_WINDOW steps of now, or None."""
    code = code.strip().replace(" ", "")
    if not re.fullmatch(rf"\d{{{TOTP_DIGITS}}}", code):
        return None
    now = current_step(at)
    for step in range(now - TOTP_WINDOW, now + TOTP_WINDOW + 1):
        if hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str) -> str:
    """The otpauth:// URI an authenticator app reads (usually as a QR code)."""
    label = quote(f"{TOTP_ISSUER}:{account}")
    query = urlencode({"secret": secret, "issuer": TOTP_ISSUER, "algorithm": "SHA1", "digits": TOTP_DIGITS,
                       "period": TOTP_STEP})
    return f"otpauth://totp/{label}?{query}"


def looks_like_totp(code: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", code.strip().replace(" ", "")))


def new_recovery_codes(n: int = RECOVERY_CODES) -> list[str]:
    """Single-use recovery codes such as k7dq2-mx9ft (50 random bits each)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(2)) for _ in range(n)]


def hash_recovery_code(code: str) -> str:
    normalised = re.sub(r"[\s-]", "", code.strip().lower())
    return hashlib.sha256(f"nearshop-recovery:{normalised}".encode()).hexdigest()


def locked_until(value: datetime | None, now: datetime | None = None) -> datetime | None:
    """`value` when that lock is still in force, else None."""
    return value if value is not None and value > (now or utcnow()) else None
