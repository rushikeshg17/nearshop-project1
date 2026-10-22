"""Revocable sessions and the admin second factor (P2.4): token_version and per-device sessions, the TOTP
step at admin sign-in with its lock and recovery codes, masked emails with an audited reveal, and the
admin_tasks create-admin command."""
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pymongo.collection import Collection as MongoCollection

from app.core import security
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import (
    TOTP_MAX_FAILURES,
    create_access_token,
    current_step,
    encrypt_totp_secret,
    hash_password,
    hash_recovery_code,
    new_totp_secret,
    totp_at,
)
from app.main import app
from app.models import Role, User
from database import admin_tasks
from tests.conftest import ADMIN_RECOVERY_CODES, HEADERS, PASSWORD, admin_code

ANDROID_CHROME = ("Mozilla/5.0 (Linux; Android 14; SM-A146B) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/141.0.0.0 Mobile Safari/537.36")
IPHONE_SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "Version/18.6 Mobile/15E148 Safari/604.1")


# ---------------------------------------------------------------- helpers
@pytest.fixture
def make_user(db):
    """Users made for one test (a customer by default) with the test password, removed afterwards."""
    made: list[int] = []

    def make(email: str, role: Role = Role.CUSTOMER, **fields) -> User:
        fields.setdefault("name", "Session Tester")
        user = db.users.insert(User(email=email, password_hash=hash_password(PASSWORD), role=role, **fields))
        made.append(user.id)
        return user

    yield make
    db.users.delete_many({"_id": {"$in": made}})
    db.mongo["admin_audit"].delete_many({"target_user_id": {"$in": made}})


def device(email: str, user_agent: str = ANDROID_CHROME, **extra) -> tuple[TestClient, str]:
    """A signed-in browser: a client holding the session cookie, and that cookie's value (to copy)."""
    c = TestClient(app)
    res = c.post("/api/auth/login", json={"email": email, "password": PASSWORD, **extra},
                 headers={**HEADERS, "User-Agent": user_agent})
    assert res.status_code == 200, res.text
    return c, c.cookies[settings.cookie_name]


def me_with(token: str):
    """GET /api/auth/me from a different browser that holds only a copy of `token`."""
    with TestClient(app) as other:
        other.cookies.set(settings.cookie_name, token)
        res = other.get("/api/auth/me")
    assert res.status_code == 200
    return res.json()


def admin_login(email: str, code: str | None = None):
    with TestClient(app) as c:
        body = {"email": email, "password": PASSWORD, **({"code": code} if code is not None else {})}
        return c.post("/api/auth/login", json=body, headers=HEADERS)


def enrolled_admin(make_user, email: str, **fields) -> tuple[User, str]:
    secret = new_totp_secret()
    user = make_user(email, Role.ADMIN, totp_secret_enc=encrypt_totp_secret(secret), **fields)
    return user, secret


# ---------------------------------------------------------------- RFC 6238
@pytest.mark.parametrize("at, expected", [(59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"),
                                          (2000000000, "69279037")])
def test_totp_matches_the_rfc_6238_sha1_vectors(at, expected):
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of the RFC's "12345678901234567890"
    assert totp_at(secret, current_step(at), digits=8) == expected


def test_totp_accepts_one_step_of_drift_only():
    secret = new_totp_secret()
    now = 1_800_000_000
    step = current_step(now)
    for offset in (-1, 0, 1):
        assert security.match_totp(secret, totp_at(secret, step + offset), at=now) == step + offset
    assert security.match_totp(secret, totp_at(secret, step + 2), at=now) is None
    assert security.match_totp(secret, "12345", at=now) is None


def test_the_totp_secret_is_stored_encrypted():
    secret = new_totp_secret()
    stored = encrypt_totp_secret(secret)
    assert secret not in stored
    assert security.decrypt_totp_secret(stored) == secret


def test_device_labels_are_short_and_coarse():
    assert security.device_label(ANDROID_CHROME) == "Chrome on Android"
    assert security.device_label(IPHONE_SAFARI) == "Safari on iPhone"
    assert security.device_label(None) == "Unknown device"
    assert len(security.device_label("x" * 500)) <= security.DEVICE_LABEL_MAX


# ---------------------------------------------------------------- revoking every session
def test_an_old_token_is_rejected_after_sign_out_everywhere(make_user):
    user = make_user("everywhere@example.com")
    a, token_a = device(user.email)
    b, token_b = device(user.email, IPHONE_SAFARI)
    assert me_with(token_a)["id"] == user.id

    assert a.post("/api/auth/logout-everywhere", headers=HEADERS).json() == {"ok": True}
    assert me_with(token_a) is None
    assert me_with(token_b) is None
    assert b.get("/api/auth/me").json() is None
    assert Database().users.get(user.id).sessions == []


def test_an_old_token_is_rejected_after_a_password_change(make_user):
    user = make_user("password-change@example.com")
    a, token_a = device(user.email)
    _, token_b = device(user.email, IPHONE_SAFARI)

    wrong = a.post("/api/auth/password", json={"current_password": "not-it", "new_password": "a-new-password-1"},
                   headers=HEADERS)
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "wrong_password"
    res = a.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "a-new-password-1"},
                 headers=HEADERS)
    assert res.status_code == 200, res.text
    assert me_with(token_a) is None and me_with(token_b) is None
    # this device continues on a new session
    assert a.cookies[settings.cookie_name] != token_a
    assert a.get("/api/auth/me").json()["id"] == user.id
    assert len(Database().users.get(user.id).sessions) == 1


def test_profile_changes_keep_sessions_and_the_phone_needs_a_code(make_user):
    user = make_user("phone-change@example.com", phone="9876511111")
    a, token_a = device(user.email)
    _, token_b = device(user.email, IPHONE_SAFARI)

    # Other profile changes leave sessions alone
    assert a.patch("/api/auth/me", json={"home_label": "Home"}, headers=HEADERS).status_code == 200
    assert me_with(token_b)["id"] == user.id

    # Since P4.1 a new phone is set only by a verify code (tests/test_otp.py, where the change ends every session).
    res = a.patch("/api/auth/me", json={"phone": "9876522222"}, headers=HEADERS)
    assert res.status_code == 422
    assert me_with(token_a)["phone"] == "9876511111" and me_with(token_b)["id"] == user.id


def test_deactivation_and_a_role_change_end_sessions(db, make_user):
    user = make_user("role-change@example.com")
    _, token = device(user.email)
    # A role change ends every session: changing the role back does not bring the old token back.
    db.users.update_one({"_id": user.id}, security.role_change_update(Role.OWNER))
    assert me_with(token) is None
    stored = db.users.get(user.id)
    assert stored.role == Role.OWNER and stored.token_version == 1 and stored.sessions == []
    db.users.update_one({"_id": user.id}, security.role_change_update(Role.CUSTOMER))
    assert me_with(token) is None
    assert db.users.get(user.id).token_version == 2
    # Defence in depth: a role edited directly in the database (without the helper) still refuses the token.
    _, token = device(user.email)
    db.users.update_one({"_id": user.id}, {"$set": {"role": Role.OWNER.value}})
    assert me_with(token) is None
    db.users.update_one({"_id": user.id}, {"$set": {"role": Role.CUSTOMER.value}})

    assert admin_tasks.deactivate_user(db, user.id, ask=lambda _: db.mongo.name, out=lambda _: None) == 0
    stored = db.users.get(user.id)
    assert stored.token_version == 3 and stored.sessions == []
    db.users.update_one({"_id": user.id}, {"$set": {"is_active": True}})
    assert me_with(token) is None  # reactivating does not bring old sessions back


def test_tokens_without_a_session_are_refused(make_user):
    user = make_user("legacy-token@example.com")
    device(user.email)
    assert me_with(create_access_token(user.id, "customer")) is None  # the pre-P2.4 shape: no sid
    stored = Database().users.get(user.id)
    forged = create_access_token(user.id, "customer", version=stored.token_version, session_id="not-a-session")
    assert me_with(forged) is None


# ---------------------------------------------------------------- per-device sessions
def test_logout_revokes_only_that_device(make_user):
    user = make_user("two-devices@example.com")
    a, token_a = device(user.email)
    b, token_b = device(user.email, IPHONE_SAFARI)

    assert a.post("/api/auth/logout", headers=HEADERS).json() == {"ok": True}
    assert me_with(token_a) is None  # the copied cookie stops working on its next request
    assert b.get("/api/auth/me").json()["id"] == user.id
    assert me_with(token_b)["id"] == user.id
    assert [s.device for s in Database().users.get(user.id).sessions] == ["Safari on iPhone"]


def test_the_eleventh_sign_in_drops_the_oldest_session(make_user):
    user = make_user("eleven@example.com")
    tokens = [device(user.email)[1] for _ in range(11)]
    stored = Database().users.get(user.id)
    assert len(stored.sessions) == security.MAX_SESSIONS == 10
    assert me_with(tokens[0]) is None
    assert me_with(tokens[1])["id"] == user.id
    assert me_with(tokens[-1])["id"] == user.id


def test_users_can_list_and_revoke_their_sessions(make_user):
    user = make_user("list-sessions@example.com")
    a, _ = device(user.email)
    _, token_b = device(user.email, IPHONE_SAFARI)
    rows = a.get("/api/auth/sessions").json()
    assert len(rows) == 2
    assert {r["device"] for r in rows} == {"Chrome on Android", "Safari on iPhone"}
    assert [r["current"] for r in rows if r["device"] == "Chrome on Android"] == [True]
    other = next(r for r in rows if not r["current"])
    assert set(other) == {"id", "device", "created_at", "last_seen_at", "current"}

    assert a.delete(f"/api/auth/sessions/{other['id']}", headers=HEADERS).json() == {"ok": True}
    assert me_with(token_b) is None
    assert a.delete(f"/api/auth/sessions/{other['id']}", headers=HEADERS).status_code == 404
    assert a.get("/api/auth/me").json()["id"] == user.id


def test_sessions_cannot_be_revoked_across_accounts(make_user):
    one, two = make_user("owner-one@example.com"), make_user("owner-two@example.com")
    a, _ = device(one.email)
    _, token_two = device(two.email)
    sid = Database().users.get(two.id).sessions[0].id
    assert a.delete(f"/api/auth/sessions/{sid}", headers=HEADERS).status_code == 404
    assert me_with(token_two)["id"] == two.id


# ---------------------------------------------------------------- database round trips
class ReadCounter:
    """Counts reads and writes on every pymongo collection (outermost calls only: find_one calls find)."""

    READS = ("find", "find_one", "aggregate", "count_documents", "distinct", "estimated_document_count")
    WRITES = ("insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many",
              "find_one_and_update", "bulk_write")

    def __init__(self, monkeypatch):
        self.reads: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self._local = threading.local()
        for name in (*self.READS, *self.WRITES):
            monkeypatch.setattr(MongoCollection, name, self._wrap(name, getattr(MongoCollection, name)))

    def _wrap(self, name, original):
        counter = self

        def wrapper(coll, *args, **kwargs):
            if getattr(counter._local, "depth", 0) == 0:
                (counter.reads if name in counter.READS else counter.writes).append((coll.name, name))
            counter._local.depth = getattr(counter._local, "depth", 0) + 1
            try:
                return original(coll, *args, **kwargs)
            finally:
                counter._local.depth -= 1

        return wrapper

    def reset(self):
        self.reads.clear()
        self.writes.clear()


def test_an_authenticated_request_makes_no_extra_reads(make_user, monkeypatch):
    user = make_user("round-trips@example.com")
    signed_in, _ = device(user.email)
    counter = ReadCounter(monkeypatch)
    assert TestClient(app).get("/api/auth/me").json() is None  # no lifespan: only the request's own reads
    anonymous_reads = list(counter.reads)

    counter.reset()
    assert signed_in.get("/api/auth/me").json()["id"] == user.id
    # The one read deps always made (the user document) and nothing more; the session is checked against it.
    assert counter.reads == [*anonymous_reads, ("users", "find_one")]
    assert counter.writes == []  # last_seen_at was written at sign-in, under an hour ago


def test_last_seen_is_written_at_most_once_an_hour(db, make_user, monkeypatch):
    user = make_user("last-seen@example.com")
    signed_in, _ = device(user.email)
    old = utcnow() - timedelta(hours=2)
    db.users.update_one({"_id": user.id}, {"$set": {"sessions.0.last_seen_at": old}})
    counter = ReadCounter(monkeypatch)
    signed_in.get("/api/auth/me")
    assert counter.writes == [("users", "update_one")]
    assert db.users.get(user.id).sessions[0].last_seen_at > old
    counter.reset()
    signed_in.get("/api/auth/me")
    assert counter.writes == []


# ---------------------------------------------------------------- admin second factor
def test_admin_login_without_a_valid_code_fails(make_user):
    admin, secret = enrolled_admin(make_user, "second-factor@example.com")
    no_code = admin_login(admin.email)
    assert no_code.status_code == 403 and no_code.json()["error"]["code"] == "totp_required"
    assert settings.cookie_name not in no_code.cookies
    wrong = admin_login(admin.email, totp_at(secret, current_step() + 5))
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_code"
    bad_password = TestClient(app).post("/api/auth/login", headers=HEADERS, json={
        "email": admin.email, "password": "not-the-password", "code": totp_at(secret, current_step())})
    assert bad_password.status_code == 401 and bad_password.json()["error"]["code"] == "invalid_credentials"

    code = totp_at(secret, current_step())
    ok = admin_login(admin.email, code)
    assert ok.status_code == 200 and ok.json()["role"] == "admin"
    replay = admin_login(admin.email, code)  # a code is accepted once only
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "invalid_code"


def test_an_admin_without_a_second_factor_cannot_sign_in(make_user):
    admin = make_user("unenrolled-admin@example.com", Role.ADMIN)
    res = admin_login(admin.email, "123456")
    assert res.status_code == 403 and res.json()["error"]["code"] == "totp_not_enrolled"


def test_five_wrong_codes_lock_admin_login(db, make_user):
    admin, secret = enrolled_admin(make_user, "lockout@example.com")
    wrong = totp_at(secret, current_step() + 10)
    for _ in range(TOTP_MAX_FAILURES - 1):
        assert admin_login(admin.email, wrong).status_code == 401
    fifth = admin_login(admin.email, wrong)
    assert fifth.status_code == 429 and fifth.json()["error"]["code"] == "totp_locked"
    assert "15 more minutes" in fifth.json()["error"]["message"]
    # Locked: even the right code is refused
    locked = admin_login(admin.email, totp_at(secret, current_step()))
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "totp_locked"
    stored = db.users.get(admin.id)
    assert stored.totp_locked_until - utcnow() > timedelta(minutes=14)

    db.users.update_one({"_id": admin.id}, {"$set": {"totp_locked_until": utcnow() - timedelta(seconds=1)}})
    assert admin_login(admin.email, totp_at(secret, current_step())).status_code == 200


def test_a_good_code_resets_the_failure_count(db, make_user):
    admin, secret = enrolled_admin(make_user, "reset-failures@example.com")
    for _ in range(TOTP_MAX_FAILURES - 1):
        assert admin_login(admin.email, "000000" if totp_at(secret, current_step()) != "000000" else "111111"
                           ).status_code == 401
    assert admin_login(admin.email, totp_at(secret, current_step())).status_code == 200
    assert db.users.get(admin.id).totp_failures == 0


def test_a_recovery_code_works_exactly_once(db, make_user):
    codes = ["abcde-fghjk", "mnpqr-stuvw"]
    admin, _ = enrolled_admin(make_user, "recovery@example.com",
                              totp_recovery_hashes=[hash_recovery_code(c) for c in codes])
    assert admin_login(admin.email, "ABCDE FGHJK").status_code == 200  # case and separators don't matter
    again = admin_login(admin.email, codes[0])
    assert again.status_code == 401 and again.json()["error"]["code"] == "invalid_code"
    assert db.users.get(admin.id).totp_recovery_hashes == [hash_recovery_code(codes[1])]
    assert admin_login(admin.email, codes[1]).status_code == 200


def test_the_seeded_admin_signs_in_with_its_code_and_recovery_codes_are_hashed(db):
    stored = db.users.find_raw({"email": "admin@example.com"}, {"totp_recovery_hashes": 1, "totp_secret_enc": 1})[0]
    assert not set(ADMIN_RECOVERY_CODES) & set(stored["totp_recovery_hashes"])
    assert stored["totp_secret_enc"]
    db.users.update_one({"email": "admin@example.com"}, {"$set": {"totp_last_step": 0}})
    assert admin_login("admin@example.com", admin_code()).status_code == 200


def test_admin_sessions_last_twelve_hours(make_user):
    admin, secret = enrolled_admin(make_user, "twelve-hours@example.com")
    c = TestClient(app)
    res = c.post("/api/auth/login", headers=HEADERS,
                 json={"email": admin.email, "password": PASSWORD, "code": totp_at(secret, current_step())})
    assert res.status_code == 200
    assert "max-age=43200" in res.headers["set-cookie"].lower()
    payload = security.decode_access_token(c.cookies[settings.cookie_name])
    assert payload["exp"] - payload["iat"] == 12 * 3600
    session = Database().users.get(admin.id).sessions[0]
    assert session.expires_at - session.created_at == timedelta(hours=12)


def test_admin_login_reports_a_missing_key_without_signing_in(make_user, monkeypatch):
    admin, secret = enrolled_admin(make_user, "no-key@example.com")
    monkeypatch.setenv("NEARSHOP_TOTP_KEY", "")
    res = admin_login(admin.email, totp_at(secret, current_step()))
    assert res.status_code == 503 and res.json()["error"]["code"] == "totp_unavailable"


def test_customer_login_is_unchanged(client):
    res = client.post("/api/auth/login", json={"email": "cust@example.com", "password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 200
    assert set(res.json()) == {"id", "name", "email", "phone", "role", "home_lat", "home_lng", "home_label", "shop",
                               "phone_verified", "email_login_sunset", "phone_migration",  # these three: P4.2
                               "language"}  # P7.11
    assert res.json()["email"] == "cust@example.com"
    assert f"max-age={settings.access_token_minutes * 60}" in res.headers["set-cookie"].lower()
    assert "httponly" in res.headers["set-cookie"].lower()
    wrong = client.post("/api/auth/login", json={"email": "cust@example.com", "password": "nope"}, headers=HEADERS)
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_credentials"


# ---------------------------------------------------------------- masked emails and the audited reveal
def test_the_users_list_masks_emails_and_a_reveal_is_audited(admin, db, make_user):
    target = make_user("reveal-me@example.com", name="Revealed Person")
    rows = admin.get("/api/admin/users", params={"q": "revealed pers"}).json()
    assert [(r["id"], r["email"]) for r in rows] == [(target.id, "r***@example.com")]
    assert [r["id"] for r in admin.get("/api/admin/users", params={"q": str(target.id)}).json()] == [target.id]
    # The search never matches email addresses, so it cannot confirm a masked address without the reveal
    for q in ("reveal-me", "reveal-me@example.com", "@example.com", "example.com"):
        assert target.id not in {r["id"] for r in admin.get("/api/admin/users", params={"q": q}).json()}, q
    assert admin.get("/api/admin/users", params={"q": "9" * 40}).status_code == 200
    assert all("@" in r["email"] and "***" in r["email"] for r in admin.get("/api/admin/users").json())
    shops = admin.get("/api/admin/shops").json()
    assert shops and all(s["owner_email"].startswith(s["owner_email"][0] + "***@") for s in shops)

    short = admin.post(f"/api/admin/users/{target.id}/reveal-email", json={"reason": "why"}, headers=HEADERS)
    assert short.status_code == 422
    assert db.mongo["admin_audit"].count_documents({"target_user_id": target.id}) == 0

    res = admin.post(f"/api/admin/users/{target.id}/reveal-email",
                     json={"reason": "Customer called about a missing pickup"}, headers=HEADERS)
    assert res.status_code == 200
    assert res.json() == {"id": target.id, "email": "reveal-me@example.com"}
    rows = list(db.mongo["admin_audit"].find({"target_user_id": target.id}))
    admin_id = db.users.find_one({"email": "admin@example.com"}).id
    assert len(rows) == 1
    assert rows[0]["admin_id"] == admin_id and rows[0]["action"] == "reveal_email"
    assert rows[0]["reason"] == "Customer called about a missing pickup"
    assert abs(rows[0]["created_at"] - utcnow()) < timedelta(minutes=1)

    assert admin.post("/api/admin/users/999999/reveal-email", json={"reason": "a reason long enough"},
                      headers=HEADERS).status_code == 404


def test_only_admins_can_reveal(customer, make_user):
    target = make_user("not-yours@example.com")
    res = customer.post(f"/api/admin/users/{target.id}/reveal-email", json={"reason": "a reason long enough"},
                        headers=HEADERS)
    assert res.status_code == 403


def test_the_audit_trail_is_kept_for_a_year(db):
    indexes = {i["name"]: i for i in db.mongo["admin_audit"].list_indexes()}
    assert indexes["created_ttl_1y"]["expireAfterSeconds"] == 365 * 24 * 60 * 60


def test_email_masking():
    from app.api.routes.admin import mask_email

    assert mask_email("priya@nearshop.demo") == "p***@nearshop.demo"
    assert mask_email("x@y.in") == "x***@y.in"
    assert mask_email("broken") == "***"


# ---------------------------------------------------------------- admin_tasks
@pytest.fixture
def lines():
    out: list[str] = []
    yield out


@pytest.fixture
def created_admins(db):
    before = {u["_id"] for u in db.users.find_raw({"role": "admin"}, {"_id": 1})}
    yield
    db.users.delete_many({"role": "admin", "_id": {"$nin": list(before)}})


def test_create_admin_enrols_a_second_factor_and_prints_it_once(db, lines, created_admins):
    confirm = lambda _: db.mongo.name  # noqa: E731
    assert admin_tasks.create_admin(db, "New Admin", "New.Admin@Example.com", ask=confirm, out=lines.append) == 0
    text = "\n".join(lines)
    assert "new.admin@example.com" not in text.lower()  # no command prints an email address
    stored = db.users.find_one({"email": "new.admin@example.com"})
    assert stored.role == Role.ADMIN and stored.totp_enrolled and stored.is_active
    uri = next(line.strip() for line in lines if line.strip().startswith("otpauth://totp/"))
    secret = uri.split("secret=")[1].split("&")[0]
    assert secret not in (stored.totp_secret_enc or "")
    assert security.decrypt_totp_secret(stored.totp_secret_enc) == secret
    codes = [line.strip() for line in lines if line.startswith("    ") and not line.strip().startswith("otpauth")]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert stored.totp_recovery_hashes == [hash_recovery_code(c) for c in codes]
    password = next(line.split("Password: ")[1] for line in lines if "Password: " in line)
    assert len(password) >= 24

    # The new admin signs in with the printed password and an authenticator code
    with TestClient(app) as c:
        res = c.post("/api/auth/login", headers=HEADERS, json={
            "email": "new.admin@example.com", "password": password, "code": totp_at(secret, current_step())})
    assert res.status_code == 200 and res.json()["role"] == "admin"

    report: list[str] = []
    admin_tasks.report(db, None, report.append)
    line = next(r for r in report if r.strip().startswith(f"id {stored.id} ") and "TOTP" in r)
    assert "TOTP enrolled" in line
    assert any("second factor enrolled on" in r for r in report)
    listing: list[str] = []
    admin_tasks.list_admins(db, listing.append)
    assert any(f"id {stored.id} " in r and "TOTP enrolled" in r for r in listing)


def test_create_admin_refuses_a_fourth_active_admin(db, lines, created_admins):
    confirm = lambda _: db.mongo.name  # noqa: E731
    active = db.users.count({"role": "admin", "is_active": True})
    for n in range(3 - active):
        assert admin_tasks.create_admin(db, f"Admin {n}", f"admin-{n}@example.com", ask=confirm, out=lines.append) == 0
    assert db.users.count({"role": "admin", "is_active": True}) == 3
    lines.clear()
    assert admin_tasks.create_admin(db, "Fourth", "fourth@example.com", ask=confirm, out=lines.append) == 1
    assert "fourth active admin" in "\n".join(lines)
    assert not db.users.exists({"email": "fourth@example.com"})


def test_create_admin_needs_the_key_confirmation_and_a_new_email(db, lines, created_admins, monkeypatch):
    assert admin_tasks.create_admin(db, "Dup", "admin@example.com", ask=lambda _: db.mongo.name,
                                    out=lines.append) == 1
    assert admin_tasks.create_admin(db, "No Confirm", "noconfirm@example.com", ask=lambda _: "nope",
                                    out=lines.append) == 1
    assert admin_tasks.create_admin(db, "Bad", "not-an-email", ask=lambda _: db.mongo.name, out=lines.append) == 1
    monkeypatch.setenv("NEARSHOP_TOTP_KEY", "")
    assert admin_tasks.create_admin(db, "No Key", "nokey@example.com", ask=lambda _: db.mongo.name,
                                    out=lines.append) == 1
    assert not db.users.exists({"email": {"$in": ["noconfirm@example.com", "nokey@example.com"]}})


def test_the_report_flags_admins_without_a_second_factor(db, make_user):
    unenrolled = make_user("plain-admin@example.com", Role.ADMIN)
    report: list[str] = []
    admin_tasks.report(db, None, report.append)
    line = next(r for r in report if r.strip().startswith(f"id {unenrolled.id} ") and "TOTP" in r)
    assert "TOTP NOT enrolled" in line
    assert "plain-admin@example.com" not in "\n".join(report)


def test_rotate_demo_ends_the_demo_accounts_sessions(db, make_user):
    demo = make_user("rotate-sessions@nearshop.demo")
    _, token = device(demo.email)
    assert admin_tasks.rotate_demo(db, ask=lambda _: db.mongo.name, out=lambda _: None) == 0
    assert me_with(token) is None


# ---------------------------------------------------------------- deploy settings
def test_the_totp_key_is_asked_for_by_both_api_services_and_documented():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    blueprint = yaml.safe_load((root / "render.yaml").read_text(encoding="utf-8"))
    web = [s for s in blueprint["services"] if s["type"] == "web"]
    assert {s["name"] for s in web} == {"nearshop-api-staging", "nearshop-api"}
    for service in web:
        assert {"key": "NEARSHOP_TOTP_KEY", "sync": False} in service["envVars"], service["name"]
    example = (root / "backend" / ".env.example").read_text(encoding="utf-8")
    assert "\nNEARSHOP_TOTP_KEY=\n" in example  # blank: each environment generates its own
