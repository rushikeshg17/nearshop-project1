"""P7.1: production holds only real data; the demo with synthetic data lives in its own deployment.

- The seed's catalogue-bootstrap mode upserts categories and catalogue items only, never deletes, and refuses
  to run without the typed database-name confirmation (it is the one seed mode allowed in production).
- The full seed is refused outside demo, development and test.
- The AI Lab (/api/ai) and the search comparison answer not found to anyone but an admin when the lab is not
  public (staging and production), and are open to everyone in the demo; a private lab shows only the model
  cards trained on real data.
- admin_tasks demo-check counts is_demo documents and nearshop.demo users.
"""
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings, settings
from app.core.database import Database, utcnow
from app.core.database import client as mongo_client
from app.main import app
from app.models import ModelRun
from database import admin_tasks, migrate
from database.seed import seed
from tests.conftest import HEADERS


def _no_database(*args, **kwargs):
    pytest.fail("reached the database before the confirmation was checked")


# ---------------------------------------------------------------- catalogue bootstrap
@pytest.fixture
def boot_db(ids, monkeypatch):
    """A fresh, migrated database on the test server, named in the settings as the configured database."""
    name = f"{settings.mongodb_db}_boot"
    mongo_client.drop_database(name)
    migrate.run(mongo_client[name])
    monkeypatch.setattr(settings, "mongodb_db", name)
    yield mongo_client[name]
    mongo_client.drop_database(name)


@pytest.mark.parametrize("confirm", [None, "", "nearshop_prod", "nearshop"])
def test_bootstrap_refuses_without_confirmation_before_any_database_use(monkeypatch, confirm):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "mongodb_db", "nearshop_prod_fresh")
    monkeypatch.setattr(seed, "Database", _no_database)
    monkeypatch.setattr(seed.schema, "reset", _no_database)
    argv = ["--catalogue-bootstrap"] + (["--confirm-db", confirm] if confirm is not None else [])
    with pytest.raises(SystemExit) as exc:
        seed.main(argv)
    assert "Catalogue bootstrap stopped; no database was touched" in str(exc.value.code)


def test_bootstrap_problem_is_pure():
    assert seed.bootstrap_problem("nearshop_prod", None)
    assert seed.bootstrap_problem("nearshop_prod", "nearshop")
    assert seed.bootstrap_problem("nearshop_prod", "nearshop_prod") is None


def test_bootstrap_refuses_a_database_without_schema(ids, monkeypatch):
    name = f"{settings.mongodb_db}_noschema"
    mongo_client.drop_database(name)
    monkeypatch.setattr(settings, "mongodb_db", name)
    try:
        with pytest.raises(SystemExit) as exc:
            seed.main(["--catalogue-bootstrap", "--confirm-db", name])
        assert "no schema yet" in str(exc.value.code)
        assert mongo_client[name]["categories"].count_documents({}) == 0
    finally:
        mongo_client.drop_database(name)


def test_bootstrap_in_production_upserts_reference_data_and_never_deletes(boot_db, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(seed.schema, "reset", _no_database)  # never a reset
    monkeypatch.setattr(seed, "train_all", _no_database)  # and never the demo city or training
    db = Database(boot_db.name)
    # Already there: one catalogue category under a stale name, and data the bootstrap must leave alone.
    mobile = db.categories.insert(seed.Category(id=db.next_id("categories"), slug="mobile-accessories",
                                                name="Old name", icon="Smartphone"))
    own = db.categories.insert(seed.Category(id=db.next_id("categories"), slug="team-added", name="Team added"))
    own_item = db.catalog_items.insert(seed.CatalogItem(id=db.next_id("catalog_items"), slug="team-item",
                                                        name="Team item", category_id=own.id))
    boot_db["counters"].delete_many({})  # as after a restore without counters: ids must still not collide
    other = {name: boot_db[name].count_documents({}) for name in ("users", "shops", "products", "sales_history",
                                                                  "search_history")}

    seed.main(["--catalogue-bootstrap", "--confirm-db", boot_db.name])

    categories = len(seed.load("categories.json"))
    items = len(seed.load("catalog.json"))
    assert boot_db["categories"].count_documents({}) == categories + 1
    assert boot_db["catalog_items"].count_documents({}) == items + 1
    kept = boot_db["categories"].find_one({"slug": "mobile-accessories"})
    assert kept["_id"] == mobile.id and kept["name"] != "Old name"
    assert boot_db["categories"].find_one({"_id": own.id})["slug"] == "team-added"
    assert boot_db["catalog_items"].find_one({"_id": own_item.id})["slug"] == "team-item"
    assert len(set(boot_db["categories"].distinct("_id"))) == categories + 1
    assert {name: boot_db[name].count_documents({}) for name in other} == other == dict.fromkeys(other, 0)
    assert boot_db["catalog_items"].count_documents({"local_terms.0": {"$exists": True}}) > 0

    # Again: nothing is added, everything matched, nothing removed.
    counts = seed.bootstrap_catalogue(boot_db)
    assert counts["categories_added"] == counts["catalog_items_added"] == 0
    assert counts["categories_updated"] == categories and counts["catalog_items_updated"] == items
    assert boot_db["categories"].count_documents({}) == categories + 1
    assert boot_db["catalog_items"].count_documents({}) == items + 1


def test_bootstrap_with_fewer_items_deletes_nothing(boot_db):
    seed.bootstrap_catalogue(boot_db)
    before = (boot_db["categories"].count_documents({}), boot_db["catalog_items"].count_documents({}))
    categories = seed.load("categories.json")[:1]
    items = [i for i in seed.load("catalog.json") if i["category"] == categories[0]["slug"]][:1]
    counts = seed.bootstrap_catalogue(boot_db, categories, items)
    assert counts["categories_added"] == counts["catalog_items_added"] == 0
    assert (boot_db["categories"].count_documents({}), boot_db["catalog_items"].count_documents({})) == before


# ---------------------------------------------------------------- the full seed
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_seed_refused_outside_demo_development_and_test(monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "demo_password", SecretStr(secrets.token_urlsafe(24)))
    monkeypatch.setattr(seed, "Database", _no_database)
    monkeypatch.setattr(seed.schema, "reset", _no_database)
    with pytest.raises(SystemExit) as exc:
        seed.main(["--confirm-db", settings.mongodb_db])
    message = str(exc.value.code)
    assert "Seed stopped; no database was touched" in message and "development, test, demo" in message


@pytest.mark.parametrize("environment", ["development", "test", "demo"])
def test_seed_allowed_environments(environment):
    problem = seed.schema.check_reset_allowed(
        environment=environment, mongodb_uri="mongodb://127.0.0.1:27017", db_name="nearshop_demo",
        production_db="nearshop_prod", protected_hosts=[], confirm="nearshop_demo")
    assert problem is None


# ---------------------------------------------------------------- the AI Lab
def test_ai_lab_public_defaults():
    def lab(environment: str) -> bool:
        return bool(Settings(_env_file=None, environment=environment).ai_lab_public)

    assert not lab("production") and not lab("staging")
    assert lab("demo") and lab("development") and lab("test")
    assert Settings(_env_file=None, environment="production", ai_lab_public=True).ai_lab_public is True


LAB_GETS = [("/api/ai/access", None), ("/api/ai/models", None), ("/api/ai/search-eval", None),
            ("/api/ai/search-compare", {"q": "charger"})]


@pytest.fixture
def production_lab(monkeypatch):
    monkeypatch.setattr(settings, "ai_lab_public", False)


@pytest.fixture
def demo_lab(monkeypatch):
    monkeypatch.setattr(settings, "ai_lab_public", True)


@pytest.mark.parametrize("path,params", LAB_GETS)
def test_ai_lab_not_found_for_visitors_in_production(production_lab, client, path, params):
    assert client.get(path, params=params, headers=HEADERS).status_code == 404


@pytest.mark.parametrize("path,params", LAB_GETS)
def test_ai_lab_not_found_for_customers_in_production(production_lab, customer, path, params):
    assert customer.get(path, params=params, headers=HEADERS).status_code == 404


def test_ai_lab_not_found_for_owners_in_production(production_lab, owner_a):
    assert owner_a.get("/api/ai/search-compare", params={"q": "charger"}, headers=HEADERS).status_code == 404
    assert owner_a.post("/api/ai/retrain", headers=HEADERS).status_code == 403


@pytest.mark.parametrize("path,params", LAB_GETS)
def test_ai_lab_allowed_for_admins_in_production(production_lab, admin, path, params):
    assert admin.get(path, params=params, headers=HEADERS).status_code == 200


@pytest.mark.parametrize("path,params", LAB_GETS)
def test_ai_lab_open_to_everyone_in_demo(demo_lab, client, path, params):
    assert client.get(path, params=params, headers=HEADERS).status_code == 200


def test_retrain_is_admin_only_in_demo_too(demo_lab, client, customer):
    with TestClient(app) as visitor:
        assert visitor.post("/api/ai/retrain", headers=HEADERS).status_code == 401
    assert customer.post("/api/ai/retrain", headers=HEADERS).status_code == 403


def test_meta_reports_whether_the_lab_is_public(client, monkeypatch):
    monkeypatch.setattr(settings, "ai_lab_public", False)
    assert client.get("/api/meta", headers=HEADERS).json()["ai_lab_public"] is False
    monkeypatch.setattr(settings, "ai_lab_public", True)
    assert client.get("/api/meta", headers=HEADERS).json()["ai_lab_public"] is True


def test_private_lab_shows_only_real_data_model_cards(admin, db, monkeypatch):
    later = utcnow() + timedelta(days=3650)  # newer than any run another test stores
    demo_run = db.model_runs.insert(ModelRun(id=db.next_id("model_runs"), name="demand", algorithm="test",
                                             uses_demo_data=True, created_at=later))
    real_run = db.model_runs.insert(ModelRun(id=db.next_id("model_runs"), name="recommendations", algorithm="test",
                                             uses_demo_data=False, created_at=later))
    try:
        monkeypatch.setattr(settings, "ai_lab_public", False)
        names = {m["name"] for m in admin.get("/api/ai/models", headers=HEADERS).json()["models"]}
        assert "demand" not in names and "recommendations" in names
        assert all(not m["uses_demo_data"] for m in admin.get("/api/ai/models", headers=HEADERS).json()["models"])
        monkeypatch.setattr(settings, "ai_lab_public", True)
        names = {m["name"] for m in admin.get("/api/ai/models", headers=HEADERS).json()["models"]}
        assert {"demand", "recommendations"} <= names
    finally:
        db.model_runs.delete_many({"_id": {"$in": [demo_run.id, real_run.id]}})


# ---------------------------------------------------------------- admin_tasks demo-check
@pytest.fixture
def check_db(ids):
    name = f"{settings.mongodb_db}_check"
    mongo_client.drop_database(name)
    yield mongo_client[name]
    mongo_client.drop_database(name)


def test_demo_check_counts_is_demo_documents_and_demo_users(check_db, monkeypatch):
    check_db["sales_history"].insert_many([{"is_demo": n < 3} for n in range(5)])
    check_db["demand_forecasts"].insert_many([{"is_demo": True} for _ in range(2)])
    check_db["users"].insert_many([
        {"email": "admin@nearshop.demo"}, {"email": "Shop1@Shops.NearShop.Demo"},
        {"email": "real@example.org"}, {"email": "x@notnearshop.demo"}, {"email": "a@nearshop.demo.example.org"}])
    counts = admin_tasks.demo_counts(check_db)
    assert counts["is_demo_documents"] == 5
    assert counts["is_demo.sales_history"] == 3 and counts["is_demo.demand_forecasts"] == 2
    assert counts["demo_users"] == 2

    lines: list[str] = []
    monkeypatch.setattr(settings, "mongodb_db", check_db.name)
    assert admin_tasks.main(["demo-check"], ask=_no_database, out=lines.append) == 1
    text = "\n".join(lines)
    assert "Documents flagged is_demo: 5" in text and "Users on nearshop.demo: 2" in text
    assert "@" not in text.replace("is_demo", "")  # prints counts only, never an address


def test_demo_check_passes_on_a_database_without_synthetic_data(check_db):
    check_db["users"].insert_one({"email": "owner@example.org"})
    check_db["sales_history"].insert_one({"is_demo": False})
    lines: list[str] = []
    assert admin_tasks.demo_check(Database(check_db.name), out=lines.append) == 0
    assert "RESULT: no synthetic data." in lines
