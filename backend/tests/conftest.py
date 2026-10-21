"""Test fixtures: an isolated, throwaway MongoDB database with a tiny, known dataset.

Tests read only NEARSHOP_TEST_MONGODB_URI, a local MongoDB replica set (make test starts one with
docker compose and sets it). Before anything is imported or connected, the run stops when that URI
is missing, names a host other than localhost, 127.0.0.1 or the CI service host mongo, or equals
NEARSHOP_MONGODB_URI (environment or backend/.env). Tests never reach Atlas.

Each run uses its own database (`nearshop_test_<random>`) on that server, dropped when the run ends.
Settings are read at import time, so the environment is configured before importing the app.
"""
import base64
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from dotenv import dotenv_values

TEST_URI_VAR = "NEARSHOP_TEST_MONGODB_URI"
TEST_HOSTS = frozenset({"localhost", "127.0.0.1", "mongo"})
BACKEND_DIR = Path(__file__).resolve().parents[1]


def _uri_hosts(uri: str) -> list[str]:
    # A simpler parse than database.schema.uri_hosts (importing that would read the app settings before
    # this guard has pointed them at the test database). It does not unwrap IPv6 literals such as [::1],
    # so those come out as hosts outside TEST_HOSTS and the URI is refused: it can only fail safe.
    rest = uri.strip().split("://", 1)[-1].replace("?", "/", 1).split("/", 1)[0].rsplit("@", 1)[-1]
    return [p.strip().lower().split(":", 1)[0] for p in rest.split(",") if p.strip()]


def check_test_database(test_uri: str | None, app_uri: str | None) -> str | None:
    """Say why the suite must not run against `test_uri`, or return None. Pure: nothing is connected."""
    if not test_uri or not test_uri.strip():
        return (f"{TEST_URI_VAR} is not set. Start the local replica set with make db-up and run make test, "
                f"or set {TEST_URI_VAR}=mongodb://127.0.0.1:27017/?directConnection=true")
    if not test_uri.startswith("mongodb://"):
        return f"{TEST_URI_VAR} must be a mongodb:// URI for a local server (mongodb+srv is never local)."
    hosts = _uri_hosts(test_uri)
    if not hosts or any(h not in TEST_HOSTS for h in hosts):
        return (f"{TEST_URI_VAR} names host(s) {', '.join(hosts) or '(none)'}; tests run only against "
                f"{', '.join(sorted(TEST_HOSTS))}.")
    if app_uri and test_uri.strip() == app_uri.strip():
        return f"{TEST_URI_VAR} equals NEARSHOP_MONGODB_URI; give the tests their own URI."
    return None


def _configured_app_uri() -> str | None:
    if "NEARSHOP_MONGODB_URI" in os.environ:
        return os.environ["NEARSHOP_MONGODB_URI"]
    env_file = BACKEND_DIR / ".env"
    return dotenv_values(env_file).get("NEARSHOP_MONGODB_URI") if env_file.is_file() else None


_test_uri = os.environ.get(TEST_URI_VAR)
if _problem := check_test_database(_test_uri, _configured_app_uri()):
    print(f"Tests stopped before connecting to any database: {_problem}", file=sys.stderr)
    raise SystemExit(4)  # exits pytest during startup, before the app is imported

_tmp = Path(tempfile.mkdtemp(prefix="nearshop-test-"))
os.environ["NEARSHOP_ENVIRONMENT"] = "test"
os.environ["NEARSHOP_MONGODB_URI"] = _test_uri
os.environ["NEARSHOP_MONGODB_DB"] = f"nearshop_test_{uuid.uuid4().hex[:10]}"
os.environ["NEARSHOP_SECRET_KEY"] = secrets.token_urlsafe(32)  # never the real key from backend/.env
os.environ["NEARSHOP_ENABLE_SCHEDULER"] = "false"
os.environ["NEARSHOP_WARM_ON_STARTUP"] = "false"  # the fixture below creates the schema
os.environ["NEARSHOP_COMPLETION_CODES_REQUIRED"] = "true"  # P3.5's completion codes (on by default since P3.7)
os.environ["NEARSHOP_EMBEDDING_PROVIDER"] = "tfidf"  # fast and offline for tests
os.environ["NEARSHOP_MEDIA_DIR"] = str(_tmp / "media")
os.environ["NEARSHOP_KYC_MEDIA_DIR"] = str(_tmp / "private_media")  # KYC documents (P3.3), never the repo's
os.environ["NEARSHOP_KYC_S3_BUCKET"] = ""
# Photos stay on the local backend with /media URLs, whatever backend/.env says: no test reaches a bucket.
os.environ["NEARSHOP_MEDIA_BACKEND"] = "local"
os.environ["NEARSHOP_MEDIA_PUBLIC_BASE_URL"] = ""
os.environ["NEARSHOP_MODEL_DIR"] = str(_tmp / "models")
# Error tracking stays off whatever backend/.env or the shell says: no test error reaches a real project.
os.environ["NEARSHOP_SENTRY_DSN"] = ""
# The admin second factor's encryption key (P2.4): a fresh one per run, never the one from backend/.env.
os.environ["NEARSHOP_TOTP_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

from fastapi.testclient import TestClient  # noqa: E402

from app.ai.semantic_index import index  # noqa: E402
from app.core.database import Database, utcnow  # noqa: E402
from app.core.database import client as mongo_client  # noqa: E402
from app.core.security import (  # noqa: E402
    current_step,
    encrypt_totp_secret,
    hash_password,
    hash_recovery_code,
    new_totp_secret,
    totp_at,
)
from app.main import app  # noqa: E402
from app.models import CatalogItem, Category, Product, Role, Shop, User  # noqa: E402
from app.models.core import VerificationStatus, months_after  # noqa: E402
from database import schema  # noqa: E402

PASSWORD = "test-password-1"
CENTER = (12.3052, 76.6552)
HEADERS = {"X-NearShop-Client": "web"}
# The seeded admin's second factor (P2.4): its TOTP secret and recovery codes.
ADMIN_TOTP_SECRET = new_totp_secret()
ADMIN_RECOVERY_CODES = [f"test{n:02d}-recov" for n in range(10)]


def admin_code(step_offset: int = 0) -> str:
    """The seeded admin's authenticator code for now (or a step either side)."""
    return totp_at(ADMIN_TOTP_SECRET, current_step() + step_offset)


def _seed() -> dict:
    db = Database()
    schema.reset(db.mongo, confirm=db.mongo.name)
    pw = hash_password(PASSWORD)
    mobile = db.categories.insert(Category(slug="mobile-accessories", name="Mobile Accessories", icon="Smartphone",
                                           color_hue=280))
    plumbing = db.categories.insert(Category(slug="plumbing", name="Plumbing", icon="Droplets", color_hue=210,
                                             sort_order=1))
    charger = db.catalog_items.insert(CatalogItem(
        slug="samsung-25w-charger", name="Samsung 25W USB-C Fast Travel Charger", brand="Samsung",
        category_id=mobile.id, subcategory="chargers", typical_price=1299, mrp=1699,
        tags=["phone charger", "adapter", "fast charger", "type c charger"], icon="PlugZap"))
    tape = db.catalog_items.insert(CatalogItem(
        slug="teflon-tape", name="Astral PTFE Thread Seal Tape", brand="Astral", category_id=plumbing.id,
        subcategory="sealants", typical_price=40, mrp=50, tags=["teflon tape", "leak", "tap leak"], icon="Droplets"))

    def user(name, email, role, phone=None):
        return User(name=name, email=email, password_hash=pw, role=role, phone=phone)

    _, _, owner_a, owner_b, _ = db.users.insert_many([
        user("Test Customer", "cust@example.com", Role.CUSTOMER, "9876543210"),
        user("Other Customer", "other@example.com", Role.CUSTOMER),
        user("Owner A", "a@example.com", Role.OWNER),
        user("Owner B", "b@example.com", Role.OWNER),
        user("Admin", "admin@example.com", Role.ADMIN).model_copy(update={
            "totp_secret_enc": encrypt_totp_secret(ADMIN_TOTP_SECRET),
            "totp_recovery_hashes": [hash_recovery_code(c) for c in ADMIN_RECOVERY_CODES]}),
    ])

    # Both shops are verified (P3.3): customers see only shops whose KYC an admin approved, and the tests that
    # search, reserve and order assume they can. tests/test_verification.py covers the unverified cases.
    decided = utcnow()
    verified = {"verification_status": VerificationStatus.VERIFIED, "is_verified": True, "kyc_type": "udyam",
                "phone_verified_at": decided, "submitted_at": decided, "decided_at": decided,
                "verified_until": months_after(decided)}
    shop_a, shop_b = db.shops.insert_many([
        Shop(owner_id=owner_a.id, name="Shop A", slug="shop-a", address_line="1 Main Road", locality="Agrahara",
             city="Mysuru", lat=CENTER[0] + 0.004, lng=CENTER[1], offers_delivery=True, delivery_radius_km=3,
             delivery_fee=30, free_delivery_above=2000, category_ids=[mobile.id, plumbing.id],
             kyc_number="UDYAM-KR-23-0000001", **verified),
        Shop(owner_id=owner_b.id, name="Shop B", slug="shop-b", address_line="2 Market Road", locality="Far Away",
             city="Mysuru", lat=CENTER[0] + 0.2, lng=CENTER[1], category_ids=[plumbing.id],
             kyc_number="UDYAM-KR-23-0000002", **verified),
    ])

    def listing(shop, item, price, qty):
        return Product(shop_id=shop.id, catalog_item_id=item.id, category_id=item.category_id, name=item.name,
                       brand=item.brand, keywords=" ".join(item.tags), icon=item.icon, price=price, mrp=item.mrp,
                       quantity=qty, low_stock_threshold=2)

    p_charger, p_tape, p_tape_far = db.products.insert_many([
        listing(shop_a, charger, 1249, 3), listing(shop_a, tape, 40, 50), listing(shop_b, tape, 38, 10)])
    return {
        "charger": p_charger.id, "tape": p_tape.id, "tape_far": p_tape_far.id,
        "shop_a": shop_a.id, "shop_b": shop_b.id,
    }


@pytest.fixture(scope="session")
def ids():
    data = _seed()
    index.mark_dirty()
    yield data
    mongo_client.drop_database(os.environ["NEARSHOP_MONGODB_DB"])


@pytest.fixture
def db(ids):
    return Database()


@pytest.fixture
def client(ids):
    with TestClient(app) as c:
        yield c


def login(client: TestClient, email: str) -> TestClient:
    body = {"email": email, "password": PASSWORD}
    if email == "admin@example.com":
        # A TOTP code is accepted once only, and tests sign the admin in many times within one 30-second step.
        Database().users.update_one({"email": email}, {"$set": {"totp_last_step": 0, "totp_failures": 0,
                                                                "totp_locked_until": None}})
        body["code"] = admin_code()
    res = client.post("/api/auth/login", json=body, headers=HEADERS)
    assert res.status_code == 200, res.text
    return client


@pytest.fixture
def customer(client):
    return login(client, "cust@example.com")


@pytest.fixture
def owner_a(ids):
    with TestClient(app) as c:
        yield login(c, "a@example.com")


@pytest.fixture
def owner_b(ids):
    with TestClient(app) as c:
        yield login(c, "b@example.com")


@pytest.fixture
def admin(ids):
    with TestClient(app) as c:
        yield login(c, "admin@example.com")
