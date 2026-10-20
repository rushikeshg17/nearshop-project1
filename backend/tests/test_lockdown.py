"""Launch lockdown (P0.2): sign-ups can be closed, /api/meta exposes the flags, and the operator tools
in database/admin_tasks.py refuse to write without the typed database name and print no email."""
import pytest

from app.core.config import Settings, settings
from app.core.security import hash_password, verify_password
from app.models import Role, User
from database import admin_tasks
from tests.conftest import HEADERS

# The demo password that backend/.env.example used to publish. Retired: the seed refuses it, and the
# report flags any demo account that still accepts it. Kept here only to test that check.
OLD_DEFAULT = "nearshop123"

SHOP_BODY = {
    "owner": {"name": "Lock Owner", "email": "lock-owner@example.com", "password": "longenough1"},
    "shop": {"name": "Lockdown Hardware", "category_slugs": ["plumbing"], "phone": "9876500001",
             "address_line": "3 Station Road", "locality": "Agrahara", "lat": 12.31, "lng": 76.65},
}


@pytest.fixture
def signups(monkeypatch):
    def set_open(value: bool) -> None:
        monkeypatch.setattr(settings, "signups_open", value)
    return set_open


# ---------------------------------------------------------------- settings
def test_signups_default_closed_in_production_and_open_elsewhere(monkeypatch):
    monkeypatch.delenv("NEARSHOP_SIGNUPS_OPEN", raising=False)
    monkeypatch.delenv("NEARSHOP_ENVIRONMENT", raising=False)
    assert Settings(_env_file=None, environment="production").signups_open is False
    assert Settings(_env_file=None, environment="development").signups_open is True


def test_signups_open_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv("NEARSHOP_SIGNUPS_OPEN", "true")
    assert Settings(_env_file=None, environment="production").signups_open is True
    monkeypatch.setenv("NEARSHOP_SIGNUPS_OPEN", "false")
    assert Settings(_env_file=None, environment="development").signups_open is False


# ---------------------------------------------------------------- register endpoints
@pytest.mark.parametrize("path, body", [
    ("/api/auth/register", {"name": "Closed Person", "email": "closed@example.com", "password": "longenough1"}),
    ("/api/auth/register", {}),  # refused before the body is validated
    ("/api/auth/register-shop", SHOP_BODY),
    ("/api/auth/register-shop", {"owner": {}}),
])
def test_register_endpoints_return_403_when_signups_closed(client, db, signups, path, body):
    signups(False)
    res = client.post(path, json=body, headers=HEADERS)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "signups_closed"
    assert not db.users.exists({"email": {"$in": ["closed@example.com", "lock-owner@example.com"]}})


@pytest.fixture
def cleanup_signups(db):
    """Remove the accounts and shop the open sign-up tests create, so later tests see the shared data
    unchanged."""
    yield
    emails = ["open@example.com", SHOP_BODY["owner"]["email"]]
    owner_ids = [u["_id"] for u in db.users.find_raw({"email": {"$in": emails}}, {"_id": 1})]
    db.shops.delete_many({"owner_id": {"$in": owner_ids}})
    db.users.delete_many({"_id": {"$in": owner_ids}})


def test_register_works_when_signups_open(client, db, signups, cleanup_signups):
    signups(True)
    res = client.post("/api/auth/register", json={"name": "Open Person", "email": "open@example.com",
                                                  "password": "longenough1"}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "customer"


def test_register_shop_works_when_signups_open(client, db, signups, cleanup_signups):
    signups(True)
    res = client.post("/api/auth/register-shop", json=SHOP_BODY, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "owner"
    assert res.json()["shop"]["name"] == "Lockdown Hardware"


# ---------------------------------------------------------------- meta
@pytest.mark.parametrize("signups_open, demo_mode", [(True, False), (False, True)])
def test_meta_exposes_signups_open_and_demo_mode(client, monkeypatch, signups_open, demo_mode):
    monkeypatch.setattr(settings, "signups_open", signups_open)
    monkeypatch.setattr(settings, "demo_mode", demo_mode)
    body = client.get("/api/meta").json()
    assert body["signups_open"] is signups_open
    assert body["demo_mode"] is demo_mode


# ---------------------------------------------------------------- admin tasks: pure checks
def test_default_password_check_true_for_old_default_hash():
    assert admin_tasks.uses_retired_password(hash_password(OLD_DEFAULT), OLD_DEFAULT) is True


def test_default_password_check_false_otherwise():
    assert admin_tasks.uses_retired_password(hash_password("a-new-random-demo-password-42"), OLD_DEFAULT) is False
    assert admin_tasks.uses_retired_password("not-a-hash", OLD_DEFAULT) is False


@pytest.mark.parametrize("email, demo", [
    ("admin@nearshop.demo", True),
    ("Owner@NearShop.Demo ", True),
    ("hardware@shops.nearshop.demo", True),
    ("x@notnearshop.demo", False),  # a look-alike domain is not a demo account
    ("nearshop.demo@gmail.com", False),
    ("x@nearshop.demo.example.com", False),
])
def test_is_demo_email_matches_the_domain_and_its_subdomains_only(email, demo):
    assert admin_tasks.is_demo_email(email) is demo


def test_retired_password_is_recognised_by_its_digest():
    assert admin_tasks.is_retired_password(OLD_DEFAULT)
    assert not admin_tasks.is_retired_password("nearshop1234")


# ---------------------------------------------------------------- admin tasks: writes need the typed name
@pytest.fixture
def demo_user(db):
    user = db.users.insert(User(name="Lock Demo", email="lock-demo@nearshop.demo", role=Role.CUSTOMER,
                                password_hash=hash_password(OLD_DEFAULT)))
    yield user
    db.users.delete_many({"_id": user.id})


@pytest.fixture
def lines():
    captured: list[str] = []
    return captured


def test_rotate_demo_does_nothing_when_confirmation_does_not_match(db, demo_user, lines):
    before = db.users.get(demo_user.id).password_hash
    code = admin_tasks.rotate_demo(db, ask=lambda _prompt: "production", out=lines.append)
    assert code != 0
    assert db.users.get(demo_user.id).password_hash == before
    assert not any(line.startswith("  id ") for line in lines)  # no password list was printed


def test_rotate_demo_sets_long_random_passwords_when_confirmed(db, demo_user, lines):
    code = admin_tasks.rotate_demo(db, ask=lambda _prompt: db.mongo.name, out=lines.append)
    assert code == 0
    printed = next(line for line in lines if line.startswith(f"  id {demo_user.id} "))
    assert "@" not in "\n".join(lines)  # ids, roles and names only: no email address
    new_password = printed.split()[-1]
    assert len(new_password) >= 24
    stored = db.users.get(demo_user.id).password_hash
    assert verify_password(new_password, stored)
    assert not verify_password(OLD_DEFAULT, stored)
    customer = db.users.find_one({"email": "cust@example.com"})
    assert not verify_password(new_password, customer.password_hash)  # only demo accounts change


def test_rotate_demo_changes_nothing_when_an_account_changes_meanwhile(db, demo_user, lines):
    before = db.users.get(demo_user.id).password_hash

    def password_after_email_change() -> str:  # runs between the read and the write
        db.users.raw.update_one({"_id": demo_user.id}, {"$set": {"email": "lock-moved@nearshop.demo"}})
        return "a-replacement-password-of-32-chars"

    code = admin_tasks.rotate_demo(db, ask=lambda _prompt: db.mongo.name, out=lines.append,
                                   new_password=password_after_email_change)
    assert code != 0
    assert db.users.get(demo_user.id).password_hash == before
    assert not any("a-replacement-password" in line for line in lines)  # no wrong password handed out


def test_deactivate_user_does_nothing_when_confirmation_does_not_match(db, demo_user, lines):
    code = admin_tasks.main(["deactivate-user", str(demo_user.id)], ask=lambda _prompt: "", out=lines.append)
    assert code != 0
    assert db.users.get(demo_user.id).is_active is True


def test_deactivate_user_when_confirmed(db, demo_user, lines):
    code = admin_tasks.deactivate_user(db, demo_user.id, ask=lambda _prompt: db.mongo.name, out=lines.append)
    assert code == 0
    assert db.users.get(demo_user.id).is_active is False


# ---------------------------------------------------------------- admin tasks: report and list-admins
def test_report_flags_old_default_and_prints_no_email(db, demo_user, lines):
    admin_tasks.report(db, OLD_DEFAULT, out=lines.append)
    text = "\n".join(lines)
    assert "@" not in text
    assert "$argon2" not in text
    assert f"Database: {db.mongo.name}" in text
    assert any(f"id {demo_user.id} " in line and "MATCHES" in line for line in lines)
    assert "1 demo account(s) still accept the old public default" in text


def test_report_states_when_no_demo_account_matches(db, demo_user, lines):
    db.users.set(demo_user, password_hash=hash_password("a-new-random-demo-password-42"))
    admin_tasks.report(db, OLD_DEFAULT, out=lines.append)
    assert "RESULT: no demo account matches the old public default." in lines


def test_report_lists_other_accounts_by_id_and_role(db, lines):
    real = db.users.insert(User(name="Real Person", email="someone@gmail.com", role=Role.OWNER,
                                password_hash=hash_password("whatever-password-1")))
    try:
        admin_tasks.report(db, None, out=lines.append)
    finally:
        db.users.delete_many({"_id": real.id})
    text = "\n".join(lines)
    assert "@" not in text and "gmail" not in text
    assert any(f"id {real.id}  role owner" in line for line in lines)
    assert "default-password check was skipped" in text


def test_report_lists_seed_generated_accounts_by_role_and_later_sign_ups_as_other(db, demo_user, lines):
    late = db.users.insert(User(name="Late Sign-up", email="late@example.com", role=Role.CUSTOMER,
                                password_hash=hash_password("whatever-password-1")))
    try:
        admin_tasks.report(db, None, out=lines.append)
    finally:
        db.users.delete_many({"_id": late.id})
    text = "\n".join(lines)
    assert "@" not in text
    seeded = db.users.find_raw({"email": {"$regex": "@example\\.com$"}, "_id": {"$lt": demo_user.id}},
                               {"role": 1})
    for role in {r["role"] for r in seeded}:
        assert any(line.strip().startswith(f"role {role}: ") for line in lines)
    other = lines.index(next(line for line in lines if "Other, possibly real people" in line))
    assert any(f"id {late.id}  role customer" in line for line in lines[other:])


def test_list_admins_prints_ids_without_email(db, lines):
    admin_tasks.main(["list-admins"], out=lines.append)
    admin = db.users.find_one({"role": "admin"})
    assert any(f"id {admin.id} " in line for line in lines)
    assert "@" not in "\n".join(lines)
