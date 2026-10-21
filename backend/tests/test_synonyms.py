"""Synonym management for admins (P5.4): the import migration, search reading entries from the database, the
30-second version check, validation, the preview and admin-only access."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.database import client as mongo_client
from app.main import app
from app.services import synonyms
from app.utils.text import expand_synonyms
from database import migrate
from database.migrations import m0005_synonyms
from tests.conftest import CENTER, HEADERS, login

TERM = "zorbik"  # a made-up word: nothing in the catalogue matches it


@pytest.fixture
def scratch():
    name = f"{os.environ['NEARSHOP_MONGODB_DB']}_s{uuid.uuid4().hex[:8]}"
    yield mongo_client[name]
    mongo_client.drop_database(name)


@pytest.fixture
def clean(db):
    """Remove the entries a test adds (any term outside the 42 imported ones) and restore the imported ones."""
    imported = {t: e for t, e, _ in m0005_synonyms.ENTRIES}
    yield
    db.mongo["synonyms"].delete_many({"term": {"$nin": list(imported)}})
    for term, expansions in imported.items():
        db.mongo["synonyms"].update_one({"term": term}, {"$set": {"expansions": expansions, "active": True}})
    db.mongo["counters"].update_one({"_id": synonyms.VERSION_ID}, {"$inc": {"seq": 1}}, upsert=True)
    synonyms.cache.invalidate()


def search(client, q):
    return client.get("/api/search", params={"q": q, "lat": CENTER[0], "lng": CENTER[1]}).json()


# ---------------------------------------------------------------- the migration


def test_migration_imports_exactly_the_42_entries(scratch):
    migrate.run(scratch)  # a new database: every migration, then ensure_schema
    coll = scratch["synonyms"]
    assert coll.count_documents({}) == 42 == len(m0005_synonyms.ENTRIES)
    assert coll.count_documents({"active": True, "created_by": None}) == 42
    chappal = coll.find_one({"term": "chappal"})
    assert chappal["expansions"] == ["slipper", "sandal", "flip flop"] and isinstance(chappal["_id"], int)
    assert "uq_term" in coll.index_information()
    assert synonyms.read_version(scratch) >= 1

    m0005_synonyms.apply(scratch)  # a rerun inserts nothing and keeps an admin's change
    assert coll.count_documents({}) == 42
    coll.update_one({"term": "tape"}, {"$set": {"active": False}})
    m0005_synonyms.apply(scratch)
    assert coll.count_documents({}) == 42 and coll.find_one({"term": "tape"})["active"] is False


def test_the_test_database_has_the_imported_entries(db):
    table = synonyms.load_table(db.mongo)
    assert len(table) == 42 and table["adaptor"] == ["charger", "adapter"]
    assert expand_synonyms("chappal for kids", db.mongo) == ["slipper", "sandal", "flip flop"]


# ---------------------------------------------------------------- search reads the database


def test_an_expansion_read_from_the_database_works_in_search(admin, client, db, clean):
    assert search(client, TERM)["total"] == 0
    res = admin.post("/api/admin/synonyms", json={"term": " Zorbik ", "expansions": ["Teflon Tape"],
                                                   "languages": ["en"]}, headers=HEADERS)
    assert res.status_code == 201, res.text
    entry = res.json()
    assert entry["term"] == TERM and entry["expansions"] == ["teflon tape"] and entry["active"] is True
    found = search(client, TERM)
    assert "teflon tape" in found["expanded_terms"] and found["total"] >= 1

    off = admin.patch(f"/api/admin/synonyms/{entry['id']}", json={"active": False}, headers=HEADERS)
    assert off.status_code == 200 and off.json()["active"] is False
    assert search(client, TERM)["total"] == 0  # deactivated: no longer expanded

    edited = admin.patch(f"/api/admin/synonyms/{entry['id']}", json={"active": True, "expansions": ["charger"]},
                         headers=HEADERS).json()
    assert edited["expansions"] == ["charger"] and search(client, TERM)["expanded_terms"] == ["charger"]
    listed = admin.get("/api/admin/synonyms", params={"q": "zorb"}).json()
    assert [e["term"] for e in listed["entries"]] == [TERM] and listed["limit"] == 500


def test_a_change_is_visible_within_30_seconds(db, clean):
    now = [1000.0]
    cache = synonyms.SynonymCache(clock=lambda: now[0])
    assert TERM not in cache.table(db.mongo)
    # Another process saves an entry: the document and a new version, nothing in this process's cache.
    db.mongo["synonyms"].insert_one({"_id": db.next_id("synonyms"), "term": TERM, "expansions": ["broom"],
                                     "languages": [], "active": True, "created_by": None,
                                     "created_at": synonyms.utcnow(), "updated_at": synonyms.utcnow()})
    db.mongo["counters"].update_one({"_id": synonyms.VERSION_ID}, {"$inc": {"seq": 1}}, upsert=True)
    now[0] += 29.0
    assert TERM not in cache.table(db.mongo)  # still the cached entries
    now[0] += 1.0
    assert cache.table(db.mongo)[TERM] == ["broom"]  # 30 s after the last check: re-read


def test_unchanged_version_is_not_reread(db, monkeypatch):
    now = [0.0]
    cache = synonyms.SynonymCache(clock=lambda: now[0])
    cache.table(db.mongo)
    loads = []
    monkeypatch.setattr(synonyms, "load_table", lambda mongo: loads.append(1) or {})
    now[0] += 31
    assert len(cache.table(db.mongo)) == 42 and loads == []  # only the version was read


def test_a_first_read_that_fails_is_retried_soon(db, monkeypatch):
    now = [0.0]
    cache = synonyms.SynonymCache(clock=lambda: now[0])
    real = synonyms.read_version

    def down(mongo):
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(synonyms, "read_version", down)
    assert cache.table(db.mongo) == {}  # the search goes ahead without synonyms
    monkeypatch.setattr(synonyms, "read_version", real)
    now[0] += synonyms.RETRY_SECONDS - 0.1
    assert cache.table(db.mongo) == {}
    now[0] += 0.1
    assert len(cache.table(db.mongo)) == 42  # retried after RETRY_SECONDS, not CHECK_SECONDS
    monkeypatch.setattr(synonyms, "read_version", down)
    now[0] += synonyms.CHECK_SECONDS
    assert len(cache.table(db.mongo)) == 42  # once loaded, a failed refresh keeps the entries


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(("body", "code"), [
    ({"term": "jhola", "expansions": ["bag", "Jhola"]}, "synonym_self_loop"),
    ({"term": "jhola", "expansions": [f"bag{n}" for n in range(21)]}, "validation_error"),
    ({"term": "jhola", "expansions": ["x" * 41]}, "synonym_invalid"),
    ({"term": "x" * 41, "expansions": ["bag"]}, "synonym_invalid"),
    ({"term": "!!!", "expansions": ["bag"]}, "synonym_invalid"),
    ({"term": "jhola", "expansions": ["  ", "?"]}, "synonym_invalid"),
    ({"term": "jhola", "expansions": ["bag"], "languages": ["Hindi"]}, "synonym_invalid"),
])
def test_validation_refuses_bad_entries(admin, db, clean, body, code):
    res = admin.post("/api/admin/synonyms", json=body, headers=HEADERS)
    assert res.status_code == 422, res.text
    if code != "validation_error":
        assert res.json()["error"]["code"] == code
    assert db.mongo["synonyms"].count_documents({"term": "jhola"}) == 0


def test_validation_refuses_a_duplicate_term_and_a_self_loop_on_edit(admin, db, clean):
    dup = admin.post("/api/admin/synonyms", json={"term": "Chappal", "expansions": ["shoe"]}, headers=HEADERS)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "synonym_exists"
    tape = db.mongo["synonyms"].find_one({"term": "tape"})
    loop = admin.patch(f"/api/admin/synonyms/{tape['_id']}", json={"expansions": ["tape"]}, headers=HEADERS)
    assert loop.status_code == 422 and loop.json()["error"]["code"] == "synonym_self_loop"
    rename = admin.patch(f"/api/admin/synonyms/{tape['_id']}", json={"term": "leak"}, headers=HEADERS)
    assert rename.status_code == 409
    assert admin.patch("/api/admin/synonyms/987654", json={"active": False}, headers=HEADERS).status_code == 404


def test_validation_refuses_more_than_500_entries(admin, db, clean, monkeypatch):
    assert synonyms.MAX_ENTRIES == 500
    count = db.mongo["synonyms"].count_documents({})
    monkeypatch.setattr(synonyms, "MAX_ENTRIES", count + 1)
    assert admin.post("/api/admin/synonyms", json={"term": "jhola", "expansions": ["bag"]},
                      headers=HEADERS).status_code == 201
    full = admin.post("/api/admin/synonyms", json={"term": "thaila", "expansions": ["bag"]}, headers=HEADERS)
    assert full.status_code == 422 and full.json()["error"]["code"] == "synonym_limit"


def test_concurrent_creates_never_pass_the_limit(db, clean, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from app.core.database import Database
    from app.core.errors import AppError

    admin_id = db.mongo["users"].find_one({"role": "admin"}, {"_id": 1})["_id"]
    count = db.mongo["synonyms"].count_documents({})
    monkeypatch.setattr(synonyms, "MAX_ENTRIES", count + 1)  # room for one more, like 499 of 500

    def create(n):
        try:
            synonyms.create_entry(Database(db.mongo.name), admin_id, f"jhola{n}", ["bag"])
            return "created"
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(create, range(8)))
    assert results.count("created") == 1 and results.count("synonym_limit") == 7
    assert db.mongo["synonyms"].count_documents({}) == count + 1


def test_the_validator_refuses_a_self_written_bad_document(db):
    from pymongo.errors import WriteError

    with pytest.raises(WriteError):
        db.mongo["synonyms"].insert_one({"_id": 999_999, "term": "x", "expansions": [], "active": True,
                                         "created_at": synonyms.utcnow(), "updated_at": synonyms.utcnow()})


# ---------------------------------------------------------------- preview


def test_preview_shows_results_without_and_with_the_entry(admin, db, clean, monkeypatch):
    from app.services import meta

    # The preview searches at the city centre; the test shops are around CENTER.
    monkeypatch.setattr(meta, "city_config", lambda: {"center": {"lat": CENTER[0], "lng": CENTER[1]}})
    res = admin.post("/api/admin/synonyms/preview", json={"term": TERM, "expansions": ["teflon tape"]},
                     headers=HEADERS)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["center"]["lat"] == CENTER[0]
    assert body["query"] == TERM and body["without"]["total"] == 0 and body["without"]["results"] == []
    assert body["with"]["total"] >= 1 and len(body["with"]["results"]) <= 10
    assert body["with"]["expanded_terms"] == ["teflon tape"]
    assert db.mongo["synonyms"].count_documents({"term": TERM}) == 0  # nothing saved
    assert TERM not in synonyms.cache.table(db.mongo)  # the override never leaks into the cache

    # Editing an existing entry: "without" leaves the entry out, "with" uses the edit.
    leak = db.mongo["synonyms"].find_one({"term": "leak"})
    edit = admin.post("/api/admin/synonyms/preview", json={"term": "leak", "expansions": ["charger"],
                                                           "entry_id": leak["_id"]}, headers=HEADERS).json()
    assert edit["without"]["expanded_terms"] == [] and edit["with"]["expanded_terms"] == ["charger"]


# ---------------------------------------------------------------- admin only


@pytest.mark.parametrize(("method", "path"), [
    ("get", "/api/admin/synonyms"), ("post", "/api/admin/synonyms"), ("patch", "/api/admin/synonyms/1"),
    ("post", "/api/admin/synonyms/preview"),
])
def test_synonym_endpoints_are_admin_only(ids, method, path):
    body = {"term": "jhola", "expansions": ["bag"]}
    kwargs = {} if method == "get" else {"json": body}
    with TestClient(app) as anonymous:
        assert getattr(anonymous, method)(path, headers=HEADERS, **kwargs).status_code == 401
    for email in ("cust@example.com", "a@example.com"):
        with TestClient(app) as c:
            res = getattr(login(c, email), method)(path, headers=HEADERS, **kwargs)
            assert res.status_code == 403, (email, res.text)
    assert mongo_client[os.environ["NEARSHOP_MONGODB_DB"]]["synonyms"].count_documents({"term": "jhola"}) == 0
