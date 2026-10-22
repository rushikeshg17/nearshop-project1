"""Phone one-time codes (P4.1): sending and verifying, phone sign-in, registration and phone changes, the limits
and the daily cap, availability without a live provider, the providers, and migration 7's duplicate-phone stop.

Codes go through the console provider (app/services/messaging.py), which keeps them in memory for these tests;
nothing reaches WhatsApp or an SMS gateway (the provider tests replace the HTTP transport)."""
import os
import uuid
from datetime import datetime, timedelta

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo.errors import DuplicateKeyError

from app import main
from app.core import observability, ratelimit
from app.core.config import Settings, settings
from app.core.database import Database, utcnow
from app.core.database import client as mongo_client
from app.core.security import hash_password
from app.models import Role, User
from app.services import messaging, otp
from app.services.messaging import ConsoleProvider, Delivery, SmsProvider, WhatsAppProvider, otp_sms_text
from database import migrate
from database.migrations import m0007_phone_identity
from tests.conftest import HEADERS, PASSWORD, login
from tests.test_observability import FAKE_DSN, FakeTransport
from tests.test_rate_limits import SECRET, Clock, assert_limited, edge

FULL_CONSENT = {"account_and_orders": True, "age_18_plus": True}
MINUTE, HOUR, DAY = 60, 3600, 86400


def new_phone() -> str:
    return f"7{uuid.uuid4().int % 10 ** 9:09d}"


@pytest.fixture(autouse=True)
def clean(ids, monkeypatch):
    """Empty OTP collections and outbox, sign-ups open, and the users a test made removed afterwards."""
    monkeypatch.setattr(settings, "signups_open", True)
    db = Database()
    for name in (otp.CODES, otp.SENDS, otp.DAILY):
        db.mongo[name].delete_many({})
    ConsoleProvider.outbox.clear()
    before = {d["_id"] for d in db.users.find_raw({}, {"_id": 1})}
    yield
    made = [d["_id"] for d in db.users.find_raw({}, {"_id": 1}) if d["_id"] not in before]
    db.users.delete_many({"_id": {"$in": made}})
    db.mongo["consents"].delete_many({"user_id": {"$in": made}})
    for name in (otp.CODES, otp.SENDS, otp.DAILY):
        db.mongo[name].delete_many({})


@pytest.fixture
def api(ids):
    with TestClient(main.app) as c:
        yield c


def ask(c: TestClient, phone: str, purpose: str = "login", headers: dict | None = None):
    return c.post("/api/auth/otp/request", json={"phone": phone, "purpose": purpose}, headers=headers or HEADERS)


def check(c: TestClient, phone: str, code: str | None, purpose: str = "login", headers: dict | None = None, **extra):
    body = {"phone": phone, "purpose": purpose, "code": code or "000000", **extra}
    return c.post("/api/auth/otp/verify", json=body, headers=headers or HEADERS)


def code_for(c: TestClient, phone: str, purpose: str = "login") -> str:
    res = ask(c, phone, purpose)
    assert res.status_code == 200, res.text
    return ConsoleProvider.last_code(phone)


def register(c: TestClient, phone: str, name: str = "Phone Person") -> dict:
    res = check(c, phone, code_for(c, phone, "register"), "register", name=name, consent=FULL_CONSENT)
    assert res.status_code == 200, res.text
    return res.json()


def email_user(db, phone: str | None = None, verified: bool = False, role: Role = Role.CUSTOMER) -> User:
    return db.users.insert(User(name="Email Person", email=f"otp-{uuid.uuid4().hex[:10]}@example.com", phone=phone,
                                phone_verified_at=utcnow() if verified else None, password_hash=hash_password(PASSWORD),
                                role=role))


def me(token: str):
    with TestClient(main.app) as other:
        other.cookies.set(settings.cookie_name, token)
        return other.get("/api/auth/me").json()


# ---------------------------------------------------------------- send and verify, end to end


def test_phone_signin_is_reported_available_with_the_console_provider(api):
    res = api.get("/api/auth/phone-signin")
    assert res.status_code == 200
    assert res.json() == {"available": True, "channels": ["console"], "code_length": 6, "expires_in": 300,
                          "resend_after": 60}


def test_register_and_sign_in_by_code_end_to_end(api, db):
    phone = new_phone()
    res = ask(api, phone, "register")
    assert res.status_code == 200 and res.json()["ok"] is True and res.json()["channel"] == "console"
    code = ConsoleProvider.last_code(phone)
    assert len(code) == 6 and code.isdigit()
    assert ConsoleProvider.outbox[-1]["text"].endswith(f"\n@localhost #{code}")  # the WebOTP line

    # Stored only as keyed hashes: neither the phone nor the code appears in the code or the send log.
    stored = db.mongo[otp.CODES].find_one({})
    logged = db.mongo[otp.SENDS].find_one({})
    for doc in (stored, logged):
        assert phone not in str(doc) and code not in str(doc)
    assert stored["attempts"] == 0 and stored["expires_at"] - stored["created_at"] == timedelta(minutes=5)
    assert logged["channel"] == "console" and logged["outcome"] == "sent" and logged["cost_estimate"] == 0

    res = check(api, phone, code, "register", name="Asha Phone", consent=FULL_CONSENT)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["phone"] == phone and body["email"] is None and body["role"] == "customer"
    assert api.cookies.get(settings.cookie_name)  # the same session cookie as a password sign-in
    assert api.get("/api/auth/me").json()["id"] == body["id"]

    raw = db.users.raw.find_one({"_id": body["id"]})
    assert "email" not in raw and raw["password_hash"] == "" and isinstance(raw["phone_verified_at"], datetime)
    granted = {r["purpose"]: r["granted"] for r in db.mongo["consents"].find({"user_id": body["id"]})}
    assert granted["account_and_orders"] is True and granted["age_18_plus"] is True

    # Sign in again from another browser with a login code.
    with TestClient(main.app) as other:
        res = check(other, phone, code_for(other, phone), "login")
        assert res.status_code == 200, res.text
        assert other.get("/api/auth/me").json()["id"] == body["id"]
    assert db.users.get(body["id"]).last_login_at is not None


def test_identical_responses_for_known_and_unknown_phones(api, db):
    known = new_phone()
    email_user(db, known, verified=True)
    unknown = new_phone()
    for purpose in ("login", "reset", "register"):
        a, b = ask(api, known, purpose), ask(api, unknown, purpose)
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, a.json())
        # A code is sent either way: nothing about the account shows in what happens either.
        assert ConsoleProvider.last_code(known) and ConsoleProvider.last_code(unknown)
    assert db.mongo[otp.SENDS].count_documents({"outcome": "sent"}) == 6


def test_a_correct_code_for_a_phone_without_an_account_says_so(api, db):
    phone = new_phone()
    res = check(api, phone, code_for(api, phone), "login")
    assert res.status_code == 404 and res.json()["error"]["code"] == "no_account"
    # The seeded customer typed a phone in at registration but never verified it: phone sign-in never reaches it.
    res = check(api, "9876543210", code_for(api, "9876543210"), "login")
    assert res.status_code == 404 and res.json()["error"]["code"] == "no_account"


def test_registration_without_consent_is_refused(api, db):
    phone = new_phone()
    code = code_for(api, phone, "register")
    res = check(api, phone, code, "register", name="No Consent")
    assert res.status_code == 422 and res.json()["error"]["code"] == "consent_required"
    res = check(api, phone, code, "register", name="No Age", consent={"account_and_orders": True})
    assert res.status_code == 422 and res.json()["error"]["code"] == "age_confirmation_required"
    assert not db.users.exists({"phone": phone})
    # The refusal came before the code was checked, so the code still works with the consent given.
    assert check(api, phone, code, "register", name="With Consent", consent=FULL_CONSENT).status_code == 200


def test_registration_needs_open_signups_and_refuses_a_verified_phone(api, db, monkeypatch):
    taken = new_phone()
    email_user(db, taken, verified=True)
    res = check(api, taken, code_for(api, taken, "register"), "register", name="Second", consent=FULL_CONSENT)
    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"

    monkeypatch.setattr(settings, "signups_open", False)
    assert ask(api, new_phone(), "register").json()["error"]["code"] == "signups_closed"


def test_a_verified_code_takes_the_phone_from_an_account_that_only_typed_it_in(api, db):
    phone = new_phone()
    squatter = email_user(db, phone)  # unverified: anyone can type any number in
    new = register(api, phone)
    assert db.users.get(new["id"]).phone == phone and db.users.get(squatter.id).phone is None


# ---------------------------------------------------------------- expiry, attempts, reuse


def test_an_expired_code_is_refused(api, db, monkeypatch):
    phone = new_phone()
    email_user(db, phone, verified=True)
    code = code_for(api, phone)
    later = utcnow() + timedelta(minutes=5, seconds=1)
    monkeypatch.setattr(otp, "now", lambda: later)
    res = check(api, phone, code)
    assert res.status_code == 400 and res.json()["error"]["code"] == "otp_expired"


def test_the_code_locks_after_5_wrong_attempts(api, db):
    phone = new_phone()
    email_user(db, phone, verified=True)
    code = code_for(api, phone)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for left in (4, 3, 2, 1):
        res = check(api, phone, wrong)
        assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_code"
        assert f"{left} attempt" in res.json()["error"]["message"]
    res = check(api, phone, wrong)
    assert res.status_code == 429 and res.json()["error"]["code"] == "otp_locked"
    res = check(api, phone, code)  # the right code no longer helps
    assert res.status_code == 429 and res.json()["error"]["code"] == "otp_locked"
    assert check(api, phone, code_for(api, phone)).status_code == 200  # a new code does


def test_a_code_is_single_use_and_a_new_code_replaces_the_old(api, db):
    phone = new_phone()
    email_user(db, phone, verified=True)
    first = code_for(api, phone)
    second = code_for(api, phone)
    if first != second:
        assert check(api, phone, first).json()["error"]["code"] == "invalid_code"
    assert check(api, phone, second).status_code == 200
    res = check(api, phone, second)
    assert res.status_code == 400 and res.json()["error"]["code"] == "otp_expired"
    # A code is for one purpose only.
    reset_code = code_for(api, phone, "reset")
    assert check(api, phone, reset_code, "login").json()["error"]["code"] == "otp_expired"


# ---------------------------------------------------------------- phone changes, reset, admins


def test_patch_me_refuses_a_phone_and_the_verify_flow_changes_it_and_ends_sessions(db):
    user = email_user(db, new_phone())
    with TestClient(main.app) as a, TestClient(main.app) as b:
        login(a, user.email)
        login(b, user.email)
        token_a, token_b = a.cookies[settings.cookie_name], b.cookies[settings.cookie_name]

        new = new_phone()
        res = a.patch("/api/auth/me", json={"phone": new}, headers=HEADERS)
        assert res.status_code == 422
        assert db.users.get(user.id).phone == user.phone

        res = check(a, new, code_for(a, new, "verify"), "verify")
        assert res.status_code == 200, res.text
        assert res.json()["phone"] == new
        stored = db.users.get(user.id)
        assert stored.phone == new and stored.phone_verified_at is not None
        assert stored.token_version == user.token_version + 1
        # Tokens issued before the change stop working; this device continues on a new session.
        assert me(token_a) is None and me(token_b) is None
        assert a.get("/api/auth/me").json()["id"] == user.id


def test_confirming_the_current_phone_keeps_sessions(db):
    phone = new_phone()
    user = email_user(db, phone)
    with TestClient(main.app) as a:
        login(a, user.email)
        token = a.cookies[settings.cookie_name]
        assert check(a, phone, code_for(a, phone, "verify"), "verify").status_code == 200
        assert db.users.get(user.id).phone_verified_at is not None and me(token)["id"] == user.id


def test_a_verify_code_needs_its_own_signed_in_user(api, db):
    phone = new_phone()
    assert ask(api, phone, "verify").status_code == 401
    assert check(api, phone, None, "verify").status_code == 401
    mine, other = email_user(db), email_user(db)
    with TestClient(main.app) as a, TestClient(main.app) as b:
        login(a, mine.email)
        login(b, other.email)
        code = code_for(a, phone, "verify")
        res = check(b, phone, code, "verify")  # another user cannot spend it
        assert res.status_code == 400 and res.json()["error"]["code"] == "otp_expired"
        held = email_user(db, new_phone(), verified=True)
        res = check(a, held.phone, code_for(a, held.phone, "verify"), "verify")
        assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"


def test_reset_sets_a_new_password_and_ends_every_session(db):
    phone = new_phone()
    user = email_user(db, phone, verified=True, role=Role.OWNER)
    with TestClient(main.app) as a, TestClient(main.app) as b:
        login(a, user.email)
        old = a.cookies[settings.cookie_name]
        res = check(b, phone, code_for(b, phone, "reset"), "reset", new_password="a-new-password-1")
        assert res.status_code == 200, res.text
        assert me(old) is None and b.get("/api/auth/me").json()["id"] == user.id
        res = b.post("/api/auth/login", json={"email": user.email, "password": "a-new-password-1"}, headers=HEADERS)
        assert res.status_code == 200


def test_owners_sign_in_by_code_and_admins_keep_their_password_and_second_factor(api, db):
    owner = email_user(db, new_phone(), verified=True, role=Role.OWNER)
    res = check(api, owner.phone, code_for(api, owner.phone), "login")
    assert res.status_code == 200 and res.json()["role"] == "owner"

    admin = email_user(db, new_phone(), verified=True, role=Role.ADMIN)
    with TestClient(main.app) as c:
        res = check(c, admin.phone, code_for(c, admin.phone), "login")
        assert res.status_code == 403 and res.json()["error"]["code"] == "password_required"
        assert c.get("/api/auth/me").json() is None
        res = check(c, admin.phone, code_for(c, admin.phone, "reset"), "reset", new_password="a-new-password-1")
        assert res.status_code == 403 and res.json()["error"]["code"] == "admin_reset"


def test_email_registration_refuses_a_phone_already_in_use(api, db):
    taken = email_user(db, new_phone())
    body = {"name": "Dup Phone", "email": f"otp-{uuid.uuid4().hex[:8]}@example.com", "password": "longenough1",
            "phone": taken.phone, "consent": FULL_CONSENT}
    res = api.post("/api/auth/register", json=body, headers=HEADERS)
    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"


def test_a_phone_account_has_no_password_to_guess(db):
    with TestClient(main.app) as c:
        body = register(c, new_phone())
        res = c.post("/api/auth/password", json={"current_password": "", "new_password": "longenough1"},
                     headers=HEADERS)
        assert res.status_code == 422
        res = c.post("/api/auth/password", json={"current_password": "x", "new_password": "longenough1"},
                     headers=HEADERS)
        assert res.status_code == 400 and res.json()["error"]["code"] == "wrong_password"
        assert db.users.get(body["id"]).password_hash == ""


def test_reviews_require_a_verified_phone_wherever_phone_sign_in_is_live():
    assert Settings(_env_file=None, environment="production").review_requires_verified_phone is False
    live = Settings(_env_file=None, environment="production", whatsapp_access_token="t", whatsapp_phone_number_id="1",
                    whatsapp_otp_template="nearshop_code")
    assert live.otp_live and live.review_requires_verified_phone is True
    assert Settings(_env_file=None, environment="production", sms_auth_key="k", sms_template_id="t",
                    otp_enabled=False).review_requires_verified_phone is False
    assert Settings(_env_file=None, environment="production", review_requires_verified_phone=True,
                    ).review_requires_verified_phone is True


# ---------------------------------------------------------------- limits and the daily cap


@pytest.fixture
def limited(ids, monkeypatch):
    """The rate limiter enforcing (the test environment's default is off), with a clock in the future."""
    clock = Clock()
    monkeypatch.setattr(ratelimit, "now", clock)
    monkeypatch.setattr(settings, "rate_limit_mode", "enforce")
    monkeypatch.setattr(settings, "edge_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "edge_secret_required", False)
    Database().mongo[ratelimit.COLLECTION].delete_many({})
    with TestClient(main.app) as c:
        yield c, clock
    Database().mongo[ratelimit.COLLECTION].delete_many({})


def test_one_send_per_phone_per_minute(limited):
    api, clock = limited
    phone = new_phone()
    assert ask(api, phone, headers=edge()).status_code == 200
    assert_limited(ask(api, phone, headers=edge("203.0.113.11")), MINUTE)  # another IP, the same phone
    assert ask(api, new_phone(), headers=edge()).status_code == 200  # another phone is not held back
    clock.advance(MINUTE)
    assert ask(api, phone, headers=edge()).status_code == 200


def test_five_sends_per_phone_per_day(limited):
    api, clock = limited
    phone = new_phone()
    for n in range(5):
        assert ask(api, phone, headers=edge(f"203.0.113.{20 + n}")).status_code == 200
        clock.advance(MINUTE)
    assert_limited(ask(api, phone, headers=edge("203.0.113.30")), DAY)


def test_twenty_sends_per_ip_per_day(limited):
    api, _ = limited
    for _ in range(20):
        assert ask(api, new_phone(), headers=edge("198.51.100.7")).status_code == 200
    assert_limited(ask(api, new_phone(), headers=edge("198.51.100.7")), DAY)
    assert ask(api, new_phone(), headers=edge("198.51.100.8")).status_code == 200


def test_ten_verifications_per_phone_per_hour(limited):
    api, clock = limited
    phone = new_phone()
    for _ in range(10):
        assert check(api, phone, "123456", headers=edge()).status_code == 400  # no code was sent: expired
    assert_limited(check(api, phone, "123456", headers=edge("203.0.113.40")), HOUR)
    clock.advance(HOUR)
    assert check(api, phone, "123456", headers=edge()).status_code == 400


def test_the_daily_cap_and_its_80_percent_warning_once_a_day(api, db, monkeypatch):
    warnings = []
    monkeypatch.setattr(otp, "report_warning", lambda message, **extra: warnings.append((message, extra)))
    monkeypatch.setattr(settings, "otp_daily_cap", 5)
    for n in range(1, 6):
        assert ask(api, new_phone()).status_code == 200
        assert len(warnings) == (1 if n >= 4 else 0)  # 80% of 5 is the 4th send, reported once
    assert warnings[0][0] == otp.SPEND_WARNING and warnings[0][1]["sends"] == 4 and warnings[0][1]["cap"] == 5

    res = ask(api, new_phone())
    assert res.status_code == 503 and res.json()["error"]["code"] == "otp_unavailable"
    assert db.mongo[otp.DAILY].find_one({"_id": otp.today()})["count"] == 5  # the refused request is not counted
    assert db.mongo[otp.SENDS].count_documents({"outcome": "capped"}) == 1
    assert len(warnings) == 1

    tomorrow = utcnow() + timedelta(days=1)
    monkeypatch.setattr(otp, "now", lambda: tomorrow)
    for _ in range(4):
        assert ask(api, new_phone()).status_code == 200
    assert len(warnings) == 2  # a new day, a new count and one more warning


@pytest.fixture
def tracker():
    transport = FakeTransport()
    assert observability.init_error_tracking(dsn=FAKE_DSN, transport=transport)
    yield transport
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)


def test_the_spend_warning_reaches_the_error_tracker(tracker):
    otp.report_warning(otp.SPEND_WARNING, day="2026-10-22", sends=1600, cap=2000)
    sentry_sdk.flush()
    events = tracker.events()
    assert len(events) == 1
    event = events[0]
    assert event["level"] == "warning" and event["message"] == otp.SPEND_WARNING
    assert event["tags"]["alert"] == "otp_spend" and event["fingerprint"] == ["otp-spend-warning"]


# ---------------------------------------------------------------- availability


@pytest.mark.parametrize("environment", ["staging", "production", "demo"])
def test_without_a_live_provider_outside_development_and_test_phone_sign_in_is_unavailable(api, monkeypatch,
                                                                                          environment):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "otp_console", True)  # even if it were set, the console never runs here
    assert api.get("/api/auth/phone-signin").json()["available"] is False
    phone = new_phone()
    for res in (ask(api, phone), check(api, phone, "123456")):
        assert res.status_code == 503 and res.json()["error"]["code"] == "otp_unavailable"
    assert not ConsoleProvider.outbox


def test_phone_sign_in_can_be_switched_off(api, monkeypatch):
    monkeypatch.setattr(settings, "otp_enabled", False)
    assert api.get("/api/auth/phone-signin").json()["available"] is False
    assert ask(api, new_phone()).status_code == 503


def test_the_console_provider_is_refused_outside_development_and_test():
    with pytest.raises(ValueError, match="NEARSHOP_OTP_CONSOLE"):
        Settings(_env_file=None, environment="staging", otp_console=True)
    assert Settings(_env_file=None, environment="production").otp_console is False
    assert messaging.otp_providers(Settings(_env_file=None, environment="production")) == []


# ---------------------------------------------------------------- providers


def live_settings(monkeypatch, *, whatsapp=True, sms=True):
    monkeypatch.setattr(settings, "environment", "staging")
    if whatsapp:
        monkeypatch.setattr(settings, "whatsapp_access_token", SecretStr("wa-token"))
        monkeypatch.setattr(settings, "whatsapp_phone_number_id", "1234567890")
        monkeypatch.setattr(settings, "whatsapp_otp_template", "nearshop_code")
    if sms:
        monkeypatch.setattr(settings, "sms_auth_key", SecretStr("sms-key"))
        monkeypatch.setattr(settings, "sms_template_id", "tmpl-1")


def test_whatsapp_and_sms_payloads(monkeypatch):
    live_settings(monkeypatch)
    calls = []
    monkeypatch.setattr(messaging, "transport", lambda url, payload, headers: (
        calls.append((url, payload, headers)) or (200, {"messages": [{"id": "wamid.1"}]})))

    delivery = WhatsAppProvider().send_otp("9876512345", "123456")
    assert delivery == Delivery(True, "whatsapp", settings.whatsapp_cost_inr, message_id="wamid.1")
    url, payload, headers = calls[-1]
    assert url == "https://graph.facebook.com/v21.0/1234567890/messages"
    assert headers["Authorization"] == "Bearer wa-token" and payload["to"] == "919876512345"
    template = payload["template"]
    assert template["name"] == "nearshop_code"
    body, button = template["components"]
    assert body["parameters"] == [{"type": "text", "text": "123456"}]
    assert button["type"] == "button" and button["sub_type"] == "url" and button["parameters"][0]["text"] == "123456"

    assert SmsProvider().send_otp("9876512345", "654321").ok
    url, payload, headers = calls[-1]
    assert url == settings.sms_api_url and headers["authkey"] == "sms-key"
    assert payload == {"template_id": "tmpl-1", "short_url": "0", "recipients": [{"mobiles": "919876512345",
                                                                                   "otp": "654321"}]}


def test_the_sms_text_ends_with_the_webotp_line():
    text = otp_sms_text("482913", "nearshop.example")
    assert text.splitlines()[-1] == "@nearshop.example #482913" and text.startswith("482913 ")


def test_a_failed_whatsapp_send_falls_back_to_sms_and_every_send_is_logged(api, db, monkeypatch):
    live_settings(monkeypatch)
    channels = []

    def transport(url, payload, headers):
        channels.append("whatsapp" if "graph.facebook.com" in url else "sms")
        return (500, {}) if channels[-1] == "whatsapp" else (200, {"request_id": "r1"})

    monkeypatch.setattr(messaging, "transport", transport)
    assert api.get("/api/auth/phone-signin").json()["channels"] == ["whatsapp", "sms"]
    res = ask(api, new_phone())
    assert res.status_code == 200 and res.json()["channel"] == "sms" and channels == ["whatsapp", "sms"]
    logged = list(db.mongo[otp.SENDS].find({}, sort=[("at", 1), ("_id", 1)]))
    assert [(r["channel"], r["outcome"], r["cost_estimate"], r["error"]) for r in logged] == [
        ("whatsapp", "failed", 0, "http_500"), ("sms", "sent", settings.sms_cost_inr, None)]


def test_when_every_provider_fails_no_code_is_left_behind(api, db, monkeypatch):
    live_settings(monkeypatch, whatsapp=False)

    def down(url, payload, headers):
        raise TimeoutError

    monkeypatch.setattr(messaging, "transport", down)
    res = ask(api, new_phone())
    assert res.status_code == 503 and res.json()["error"]["code"] == "otp_send_failed"
    assert db.mongo[otp.CODES].count_documents({}) == 0
    assert db.mongo[otp.SENDS].find_one({})["error"] == "TimeoutError"


# ---------------------------------------------------------------- migration 7


@pytest.fixture
def scratch():
    name = f"{os.environ['NEARSHOP_MONGODB_DB']}_p{uuid.uuid4().hex[:8]}"
    yield mongo_client[name]
    mongo_client.drop_database(name)


def user_doc(_id, phone=None, email=True):
    now = datetime(2026, 10, 20)
    doc = {"_id": _id, "name": "U", "phone": phone, "password_hash": "x", "role": "customer", "is_active": True,
           "created_at": now, "updated_at": now}
    return {**doc, "email": f"u{_id}@example.com"} if email else doc


def test_the_duplicate_phone_migration_stops_and_changes_nothing(scratch):
    # The state before migration 7: the old unique email index, and two accounts sharing a phone.
    scratch["users"].create_index("email", unique=True, name="uq_email")
    scratch["users"].insert_many([user_doc(1, "9876500101"), user_doc(2, "9876500101"), user_doc(3, "9876500102"),
                                  user_doc(4)])
    before = scratch["users"].index_information()

    with pytest.raises(m0007_phone_identity.DuplicatePhones) as stopped:
        m0007_phone_identity.apply(scratch)
    assert stopped.value.groups == [[1, 2]]
    assert "1, 2" in str(stopped.value) and "9876500101" not in str(stopped.value)  # ids, never the numbers
    assert scratch["users"].index_information() == before

    # Once the shared number is settled the migration runs, and its result holds.
    scratch["users"].update_one({"_id": 2}, {"$set": {"phone": None}})
    m0007_phone_identity.apply(scratch)
    m0007_phone_identity.apply(scratch)  # idempotent
    names = set(scratch["users"].index_information())
    assert {"uq_phone", "uq_email_set"} <= names and "uq_email" not in names
    scratch["users"].insert_many([user_doc(5, "9876500105", email=False), user_doc(6, email=False)])  # no email
    with pytest.raises(DuplicateKeyError):
        scratch["users"].insert_one(user_doc(7, "9876500102"))


def test_the_runner_records_nothing_when_the_duplicate_phone_check_stops(scratch):
    migrate.run(scratch)
    scratch[migrate.MIGRATIONS_COLLECTION].delete_one({"_id": 7})
    scratch["users"].drop_index("uq_phone")
    scratch["users"].insert_many([user_doc(1, "9876500201"), user_doc(2, "9876500201")])
    with pytest.raises(m0007_phone_identity.DuplicatePhones):
        migrate.run(scratch)
    assert scratch[migrate.MIGRATIONS_COLLECTION].find_one({"_id": 7}) is None
    assert "uq_phone" not in scratch["users"].index_information()
