"""The move to phone sign-in (P4.2): what /auth/me reports, email sign-in before and after the sunset, the daily
phone_migration job on a controlled clock, refusing a phone verified on another account, and the sign-ups that verify
a phone with a code (phone registration with an optional email, and shop sign-up).

Codes go through the console provider (app/services/messaging.py); nothing reaches WhatsApp or an SMS gateway. The
suite's sunset is far away (tests/conftest.py); these tests move it."""
import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import jobs, main
from app.api.routes import auth as auth_routes
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.models import Role, User
from app.services import otp
from app.services.messaging import ConsoleProvider
from tests.conftest import HEADERS, PASSWORD, login

FULL_CONSENT = {"account_and_orders": True, "age_18_plus": True}
SUNSET = date(2027, 4, 10)


def new_phone() -> str:
    return f"8{uuid.uuid4().int % 10 ** 9:09d}"


@pytest.fixture(autouse=True)
def clean(ids, monkeypatch):
    """Sign-ups open, empty OTP collections, and the users, shops, consents and notifications a test made removed
    afterwards."""
    monkeypatch.setattr(settings, "signups_open", True)
    db = Database()
    for name in (otp.CODES, otp.SENDS, otp.DAILY):
        db.mongo[name].delete_many({})
    ConsoleProvider.outbox.clear()
    users = {d["_id"] for d in db.users.find_raw({}, {"_id": 1})}
    shops = {d["_id"] for d in db.shops.find_raw({}, {"_id": 1})}
    yield
    made = [d["_id"] for d in db.users.find_raw({}, {"_id": 1}) if d["_id"] not in users]
    db.users.delete_many({"_id": {"$in": made}})
    db.shops.delete_many({"_id": {"$nin": list(shops)}})
    for name in ("consents", "notifications"):
        db.mongo[name].delete_many({"user_id": {"$in": made}})
    for name in (otp.CODES, otp.SENDS, otp.DAILY):
        db.mongo[name].delete_many({})


def sunset_in(monkeypatch, days: int) -> date:
    """Put the sunset this many days from today (IST): negative means it has passed."""
    sunset = auth_routes.ist_today() + timedelta(days=days)
    monkeypatch.setattr(settings, "email_login_sunset", sunset)
    return sunset


def account(db, role: Role = Role.CUSTOMER, verified: bool = False, phone: str | None = None) -> User:
    return db.users.insert(User(name="Migrating Person", email=f"mig-{uuid.uuid4().hex[:10]}@example.com",
                                phone=phone or (new_phone() if verified else None),
                                phone_verified_at=utcnow() if verified else None,
                                password_hash=hash_password(PASSWORD), role=role))


def email_login(c: TestClient, email: str):
    return c.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers=HEADERS)


def ask(c: TestClient, phone: str, purpose: str):
    res = c.post("/api/auth/otp/request", json={"phone": phone, "purpose": purpose}, headers=HEADERS)
    assert res.status_code == 200, res.text
    return ConsoleProvider.last_code(phone)


# ---------------------------------------------------------------- /auth/me


def test_me_reports_the_verification_state_and_the_sunset(db, monkeypatch):
    sunset = sunset_in(monkeypatch, 20)
    unverified, verified = account(db), account(db, verified=True)
    with TestClient(main.app) as c:
        login(c, unverified.email)
        me = c.get("/api/auth/me").json()
        assert me["phone_verified"] is False and me["email_login_sunset"] == sunset.isoformat()
        assert me["phone_migration"] == "warn"  # the banner, until the sunset
        sunset_in(monkeypatch, 0)
        assert c.get("/api/auth/me").json()["phone_migration"] == "required"  # from the sunset: /verify-phone
        monkeypatch.setattr(settings, "otp_enabled", False)  # no phone sign-in: nothing is asked
        assert c.get("/api/auth/me").json()["phone_migration"] is None
    monkeypatch.setattr(settings, "otp_enabled", True)
    with TestClient(main.app) as c:
        login(c, verified.email)
        me = c.get("/api/auth/me").json()
        assert me["phone_verified"] is True and me["phone_migration"] is None
    with TestClient(main.app) as c:
        login(c, "admin@example.com")  # admins keep password and second factor: never asked
        me = c.get("/api/auth/me").json()
        assert me["phone_verified"] is False and me["phone_migration"] is None


def test_phone_signin_reports_the_sunset(api_client, monkeypatch):
    sunset = sunset_in(monkeypatch, 3)
    body = api_client.get("/api/auth/phone-signin").json()
    assert body["available"] is True and body["email_login_sunset"] == sunset.isoformat()
    assert body["email_login_ended"] is False
    sunset_in(monkeypatch, -1)
    assert api_client.get("/api/auth/phone-signin").json()["email_login_ended"] is True


@pytest.fixture
def api_client(ids):
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------- email sign-in and the sunset


def test_email_login_is_allowed_before_the_sunset_and_refused_after_it_without_a_verified_phone(db, monkeypatch):
    customer, owner = account(db), account(db, Role.OWNER)
    verified_customer, verified_owner = account(db, verified=True), account(db, Role.OWNER, verified=True)
    with TestClient(main.app) as c:
        sunset_in(monkeypatch, 1)  # the day before
        for user in (customer, owner, verified_customer, verified_owner):
            assert email_login(c, user.email).status_code == 200

        sunset_in(monkeypatch, 0)  # the sunset day itself
        for user in (customer, owner):
            res = email_login(c, user.email)
            assert res.status_code == 403 and res.json()["error"]["code"] == "phone_signin_required"
            assert "phone" in res.json()["error"]["message"]
        # A verified phone keeps email sign-in, and a wrong password still says only that.
        for user in (verified_customer, verified_owner):
            assert email_login(c, user.email).status_code == 200
        res = c.post("/api/auth/login", json={"email": customer.email, "password": "wrong-password"}, headers=HEADERS)
        assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_credentials"


def test_admins_keep_email_sign_in_after_the_sunset(client, monkeypatch):
    sunset_in(monkeypatch, -5)
    login(client, "admin@example.com")  # asserts 200: password and authenticator code
    assert client.get("/api/auth/me").json()["role"] == "admin"


def test_without_phone_sign_in_the_sunset_does_not_lock_anyone_out(db, monkeypatch):
    customer = account(db)
    sunset_in(monkeypatch, -5)
    monkeypatch.setattr(settings, "otp_enabled", False)
    with TestClient(main.app) as c:
        assert email_login(c, customer.email).status_code == 200


def test_after_the_sunset_an_unknown_phone_is_pointed_to_support(api_client, monkeypatch):
    sunset_in(monkeypatch, -1)
    phone = new_phone()
    res = api_client.post("/api/auth/otp/verify", json={"phone": phone, "purpose": "login", "code": ask(
        api_client, phone, "login")}, headers=HEADERS)
    assert res.status_code == 404 and "grievance" in res.json()["error"]["message"]


def test_an_email_account_verifies_its_phone_and_then_signs_in_with_it(db, monkeypatch):
    """The staging rehearsal, here: the sunset passed, the signed-in account verifies, then phone sign-in works."""
    sunset_in(monkeypatch, -1)
    user = account(db)
    phone = new_phone()
    with TestClient(main.app) as c:
        # Signed in from before the sunset: the session still works, and the site sends it to /verify-phone.
        sunset_in(monkeypatch, 1)
        login(c, user.email)
        sunset_in(monkeypatch, -1)
        assert c.get("/api/auth/me").json()["phone_migration"] == "required"
        res = c.post("/api/auth/otp/verify", json={"phone": phone, "purpose": "verify", "code": ask(c, phone, "verify")},
                     headers=HEADERS)
        assert res.status_code == 200, res.text
        assert res.json()["phone_verified"] is True and res.json()["phone_migration"] is None
    with TestClient(main.app) as c:
        login_res = c.post("/api/auth/otp/verify", json={"phone": phone, "purpose": "login",
                                                          "code": ask(c, phone, "login")}, headers=HEADERS)
        assert login_res.status_code == 200 and login_res.json()["id"] == user.id
        assert email_login(c, user.email).status_code == 200  # a verified phone keeps email sign-in too


# ---------------------------------------------------------------- a phone already on another account


def test_verifying_a_phone_verified_on_another_account_is_refused_with_a_support_message(db):
    holder = account(db, verified=True)
    user = account(db)
    with TestClient(main.app) as c:
        login(c, user.email)
        res = c.post("/api/auth/otp/verify", json={"phone": holder.phone, "purpose": "verify",
                                                    "code": ask(c, holder.phone, "verify")}, headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"
        message = res.json()["error"]["message"]
        assert "contact us" in message and "grievance" in message and "merge" in message
    # Nothing moved: never merged, never taken over.
    assert db.users.get(user.id).phone is None and db.users.get(user.id).phone_verified_at is None
    assert db.users.get(holder.id).phone == holder.phone and db.users.get(holder.id).phone_verified_at is not None


# ---------------------------------------------------------------- sign-ups that verify a phone


def test_phone_registration_takes_an_optional_email(api_client, db):
    phone = new_phone()
    email = f"phone-{uuid.uuid4().hex[:8]}@Example.com"
    res = api_client.post("/api/auth/otp/verify", json={
        "phone": phone, "purpose": "register", "code": ask(api_client, phone, "register"), "name": "Phone Person",
        "email": email, "consent": FULL_CONSENT}, headers=HEADERS)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["email"] == email.lower() and body["phone"] == phone and body["phone_verified"] is True

    # An email already in use is refused before the code is spent; the same code then works without it.
    other = new_phone()
    code = ask(api_client, other, "register")
    with TestClient(main.app) as c:
        res = c.post("/api/auth/otp/verify", json={"phone": other, "purpose": "register", "code": code,
                                                   "name": "Other", "email": email, "consent": FULL_CONSENT},
                     headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "email_taken"
        res = c.post("/api/auth/otp/verify", json={"phone": other, "purpose": "register", "code": code,
                                                   "name": "Other", "consent": FULL_CONSENT}, headers=HEADERS)
        assert res.status_code == 200 and res.json()["email"] is None

    # An email belongs only to a registration.
    res = api_client.post("/api/auth/otp/verify", json={"phone": phone, "purpose": "login", "code": "123456",
                                                         "email": email}, headers=HEADERS)
    assert res.status_code == 422


def shop_body(phone: str, code: str | None = None) -> dict:
    owner = {"name": "Code Owner", "email": f"owner-{uuid.uuid4().hex[:8]}@example.com", "password": "longenough1",
             "consent": FULL_CONSENT}
    if code is not None:
        owner["code"] = code
    return {"owner": owner, "shop": {"name": "Code Hardware", "category_slugs": ["plumbing"], "phone": phone,
                                     "address_line": "4 Station Road", "locality": "Agrahara", "lat": 12.31,
                                     "lng": 76.65}}


def test_shop_signup_with_a_code_verifies_the_owner_and_shop_phone(api_client, db):
    phone = new_phone()
    res = api_client.post("/api/auth/register-shop", json=shop_body(phone, "000000"), headers=HEADERS)
    assert res.status_code == 400 and res.json()["error"]["code"] == "otp_expired"  # no code was sent
    assert not db.shops.exists({"phone": phone})

    with TestClient(main.app) as c:
        res = c.post("/api/auth/register-shop", json=shop_body(phone, ask(c, phone, "register")), headers=HEADERS)
        assert res.status_code == 200, res.text
        assert res.json()["phone"] == phone and res.json()["phone_verified"] is True
        owner = db.users.get(res.json()["id"])
        shop = db.shops.find_one({"owner_id": owner.id})
        assert owner.phone_verified_at is not None and shop.phone_verified_at is not None  # P3.3 needs the shop's

    # The number is now verified on that account: another sign-up with it is refused before its code is spent.
    with TestClient(main.app) as c:
        code = ask(c, phone, "register")
        res = c.post("/api/auth/register-shop", json=shop_body(phone, code), headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"


def test_shop_signup_without_a_code_leaves_the_phone_unverified(api_client, db):
    phone = new_phone()
    res = api_client.post("/api/auth/register-shop", json=shop_body(phone), headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["phone_verified"] is False
    assert db.shops.find_one({"owner_id": res.json()["id"]}).phone_verified_at is None


# ---------------------------------------------------------------- the daily job


class Clock:
    def __init__(self, at: datetime):
        self.now = at

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def migration(db, monkeypatch):
    """The job on a controlled clock, with the sunset on SUNSET. The seeded accounts, which have no verified phone,
    are marked verified meanwhile so that only this test's accounts count; they are put back afterwards."""
    monkeypatch.setattr(settings, "email_login_sunset", SUNSET)
    seeded = db.users.find_raw({"phone_verified_at": None}, {"_id": 1})
    seeded_ids = [d["_id"] for d in seeded]
    db.users.update_many({"_id": {"$in": seeded_ids}}, {"$set": {"phone_verified_at": utcnow()}})
    db.mongo[jobs.LEASES].delete_one({"_id": "phone_migration"})
    yield
    db.users.update_many({"_id": {"$in": seeded_ids}}, {"$set": {"phone_verified_at": None}})
    db.mongo[jobs.LEASES].delete_one({"_id": "phone_migration"})


def run_day(db, clock: Clock, day: date) -> dict:
    """One tick of the job on `day`, at 09:00 IST, through the lease; returns its last_result."""
    clock.now = datetime(day.year, day.month, day.day, 3, 30)
    assert jobs.run_leased(db.mongo, "phone_migration", "process-test", lambda: jobs.phone_migration(clock), clock)
    return db.mongo[jobs.LEASES].find_one({"_id": "phone_migration"})["last_result"]


def warnings_for(db, user: User) -> list[dict]:
    return list(db.mongo["notifications"].find({"user_id": user.id, "kind": jobs.PHONE_MIGRATION_KIND},
                                               sort=[("created_at", 1)]))


def test_the_daily_job_warns_30_7_and_1_days_before_and_switches_off_30_days_after(db, migration):
    customer, owner = account(db), account(db, Role.OWNER)
    verified, admin = account(db, verified=True), account(db, Role.ADMIN)
    clock = Clock(utcnow())
    warned_on: dict[int, int] = {}
    for days_before in range(40, 0, -1):
        result = run_day(db, clock, SUNSET - timedelta(days=days_before))
        assert result["ok"] is True and result["deactivated"] == 0 and result["remaining"] == 2
        if result["warned"]:
            warned_on[days_before] = result["warned"]
    assert warned_on == {30: 2, 7: 2, 1: 2}  # the customer and the owner; never the verified user or the admin
    assert len(warnings_for(db, customer)) == 3 and len(warnings_for(db, owner)) == 3
    assert warnings_for(db, verified) == [] and warnings_for(db, admin) == []
    last = warnings_for(db, customer)[-1]
    assert last["link"] == "/verify-phone" and "tomorrow" in last["title"] and "10 April 2027" in last["body"]

    # A second tick the same day repeats the result and warns nobody again.
    again = run_day(db, clock, SUNSET - timedelta(days=1))
    assert again["warned"] == 2 and len(warnings_for(db, customer)) == 3

    # The owner verifies after the sunset; the customer never does.
    for days_after in range(0, 30):
        result = run_day(db, clock, SUNSET + timedelta(days=days_after))
        assert result["warned"] == 0 and result["deactivated"] == 0
        if days_after == 10:
            db.users.update_one({"_id": owner.id}, {"$set": {"phone": new_phone(), "phone_verified_at": utcnow()}})
    assert result["remaining"] == 1
    result = run_day(db, clock, SUNSET + timedelta(days=30))
    assert (result["warned"], result["deactivated"], result["remaining"]) == (0, 1, 0)
    assert result["ran_for"] == (SUNSET + timedelta(days=30)).isoformat() and result["sunset"] == SUNSET.isoformat()
    switched_off = db.users.get(customer.id)
    assert switched_off.is_active is False and switched_off.sessions == []
    assert switched_off.token_version == customer.token_version + 1  # every session ended
    assert db.users.get(owner.id).is_active is True and db.users.get(admin.id).is_active is True
    assert run_day(db, clock, SUNSET + timedelta(days=31))["deactivated"] == 0


def test_a_missed_warning_day_is_caught_up_and_a_new_account_is_warned_too(db, migration):
    customer = account(db)
    clock = Clock(utcnow())
    assert run_day(db, clock, SUNSET - timedelta(days=31))["warned"] == 0
    # Day 30 missed (the service was down): day 29 sends the 30-day warning, once.
    assert run_day(db, clock, SUNSET - timedelta(days=29))["warned"] == 1
    assert run_day(db, clock, SUNSET - timedelta(days=28))["warned"] == 0
    late = account(db)  # registered by email after the first warning
    assert run_day(db, clock, SUNSET - timedelta(days=27))["warned"] == 1
    assert len(warnings_for(db, customer)) == 1 and len(warnings_for(db, late)) == 1


def test_without_phone_sign_in_the_job_only_counts(db, migration, monkeypatch):
    customer = account(db)
    monkeypatch.setattr(settings, "otp_enabled", False)
    clock = Clock(utcnow())
    result = run_day(db, clock, SUNSET - timedelta(days=30))
    assert (result["warned"], result["deactivated"], result["remaining"]) == (0, 0, 1)
    assert result["phone_signin_available"] is False
    result = run_day(db, clock, SUNSET + timedelta(days=30))
    assert result["deactivated"] == 0 and db.users.get(customer.id).is_active is True
