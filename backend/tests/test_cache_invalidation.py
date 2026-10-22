"""Shared cache invalidation across processes (P8.1): the cache_versions collection, the 5-second version check,
the semantic index's incremental refresh against a full rebuild, the nightly rebuild, and the spelling vocabulary
and synonyms following another process's change.

Two Database handles, each with its own VersionWatcher (on a fake clock) and its own SemanticIndex, stand in for
two API processes sharing one database."""
import dataclasses
import os
import uuid
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.ai import semantic_index
from app.ai.embeddings import get_provider
from app.ai.semantic_index import SemanticIndex, index, nightly_slot, vectors
from app.core import cache_version
from app.core.database import Database
from app.core.database import client as mongo_client
from app.models import Product
from app.services import search, synonyms
from database import schema
from tests.conftest import HEADERS

WORDS = ["steel bucket", "garden hose pipe", "copper wire coil", "led tube light", "pvc elbow joint",
         "teflon thread tape", "brass ball valve", "wall socket switch"]


class Process:
    """One API process: its own database handle, version watcher (on a clock the test moves) and index."""

    def __init__(self, db_name: str | None = None, background: bool = False):
        self.now = 1000.0
        self.db = Database(db_name)
        self.watcher = cache_version.VersionWatcher(clock=lambda: self.now)
        self.index = SemanticIndex(watcher=self.watcher, background=background)

    def wait(self, seconds: float) -> None:
        self.now += seconds

    def bump(self) -> int:
        return cache_version.bump(self.db.mongo, cache_version.LISTINGS, local=self.watcher)


@pytest.fixture
def scratch(ids):
    """A database of its own, with the schema and a few listings, so the tests control every listing."""
    name = f"{os.environ['NEARSHOP_MONGODB_DB']}_c{uuid.uuid4().hex[:8]}"
    schema.ensure_schema(mongo_client[name])
    db = Database(name)
    db.products.insert_many([Product(shop_id=1, category_id=1, name=w.title(), keywords=w, price=10 + i)
                             for i, w in enumerate(WORDS)])
    yield name
    mongo_client.drop_database(name)


def listing(db: Database, name: str, **fields) -> Product:
    return db.products.insert(Product(shop_id=1, category_id=1, name=name, keywords=name.lower(), price=99, **fields))


def same_as_full_rebuild(proc: Process) -> None:
    full = SemanticIndex(watcher=cache_version.VersionWatcher())
    full.ensure(Database(proc.db.mongo.name))
    assert np.array_equal(proc.index.snapshot.product_ids, full.snapshot.product_ids)
    assert proc.index.snapshot.matrix.shape == full.snapshot.matrix.shape
    assert np.array_equal(proc.index.snapshot.matrix, full.snapshot.matrix)


# ---------------------------------------------------------------- the version store


def test_bump_keeps_one_document_per_cache(scratch):
    mongo = mongo_client[scratch]
    assert cache_version.read_versions(mongo) == {}
    assert cache_version.bump(mongo, cache_version.LISTINGS) == 1
    assert cache_version.bump(mongo, cache_version.LISTINGS) == 2
    assert cache_version.bump(mongo, cache_version.SYNONYMS) == 1
    assert cache_version.read_versions(mongo) == {"listings": 2, "synonyms": 1}
    doc = mongo[cache_version.COLLECTION].find_one({"_id": "listings"})
    assert set(doc) == {"_id", "version", "updated_at"} and isinstance(doc["updated_at"], datetime)
    assert "cache_versions" in schema.VALIDATORS
    with pytest.raises(ValueError):
        cache_version.bump(mongo, "everything")


def test_versions_are_read_at_most_once_every_5_seconds_with_one_query(scratch, monkeypatch):
    proc = Process(scratch)
    reads = []
    real = cache_version.read_versions
    monkeypatch.setattr(cache_version, "read_versions", lambda mongo: reads.append(1) or real(mongo))
    for _ in range(20):  # twenty searches in the same second
        proc.index.ensure(proc.db)
        search._vocabulary(proc.db)  # the vocabulary uses the process-wide watcher, not this one
    assert len(reads) == 1 + 1
    proc.wait(cache_version.CHECK_SECONDS)
    proc.index.ensure(proc.db)
    assert len(reads) == 3


def test_a_failed_read_keeps_the_versions_already_read(scratch, monkeypatch):
    proc = Process(scratch)
    proc.bump()
    assert proc.watcher.version(proc.db.mongo, cache_version.LISTINGS) == 1

    def down(mongo):
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(cache_version, "read_versions", down)
    proc.wait(cache_version.CHECK_SECONDS)
    assert proc.watcher.version(proc.db.mongo, cache_version.LISTINGS) == 1  # no exception, the old version


# ---------------------------------------------------------------- two processes


def test_a_new_listing_made_through_one_process_reaches_the_other_within_5_seconds(scratch):
    a, b = Process(scratch), Process(scratch)
    a.index.ensure(a.db)
    b.index.ensure(b.db)
    new = listing(a.db, "Zinc Coated Gate Hinge")
    a.bump()
    assert new.id in a.index.query(a.db, "gate hinge")  # at once in the process that made the change

    b.wait(cache_version.CHECK_SECONDS - 0.1)
    assert new.id not in b.index.query(b.db, "gate hinge")  # the other still has its copy of the versions
    b.wait(0.1)
    assert new.id in b.index.query(b.db, "gate hinge")  # 5 s after its last check: refreshed
    assert (b.index.full_builds, b.index.refreshes) == (1, 1)  # incrementally, not rebuilt
    same_as_full_rebuild(b)


def test_an_incremental_update_gives_the_same_index_as_a_full_rebuild(scratch):
    a, b = Process(scratch), Process(scratch)
    hidden = listing(a.db, "Hidden Copper Bend", is_active=False)
    b.index.ensure(b.db)
    assert hidden.id not in b.index.snapshot.product_ids

    added = [listing(a.db, f"Fresh {w.title()}") for w in WORDS[:3]]
    renamed = a.db.products.find_one({"name": WORDS[4].title()})
    a.db.products.set(renamed, name="Pvc Elbow Joint 90 Degree", keywords="elbow bend fitting")
    removed = a.db.products.find_one({"name": WORDS[0].title()})
    a.db.products.set(removed, is_active=False)
    a.db.products.set(hidden, is_active=True)  # reactivated
    a.db.products.set(added[1], is_active=False)  # added and removed between two checks
    a.bump()

    b.wait(cache_version.CHECK_SECONDS)
    b.index.ensure(b.db)
    assert (b.index.full_builds, b.index.refreshes) == (1, 1)
    ids = set(b.index.snapshot.product_ids.tolist())
    assert {added[0].id, added[2].id, hidden.id, renamed.id} <= ids
    assert removed.id not in ids and added[1].id not in ids
    assert list(b.index.snapshot.product_ids) == sorted(ids)
    same_as_full_rebuild(b)

    # A second round on top of the first, with an edit to a listing the first round added.
    a.db.products.set(added[0], description="Heavy duty, with a lid")
    a.bump()
    b.wait(cache_version.CHECK_SECONDS)
    b.index.ensure(b.db)
    assert b.index.refreshes == 2
    same_as_full_rebuild(b)


def test_a_bump_without_a_listing_change_costs_no_rebuild(scratch):
    b = Process(scratch)
    b.index.ensure(b.db)
    before = b.index.snapshot
    Process(scratch).bump()  # an admin verified a shop in another process
    b.wait(cache_version.CHECK_SECONDS)
    b.index.ensure(b.db)
    assert b.index.full_builds == 1 and b.index.snapshot.version == 1
    assert np.array_equal(b.index.snapshot.product_ids, before.product_ids)


def test_a_version_that_goes_down_rebuilds_in_full(scratch):
    b = Process(scratch)
    b.bump()
    b.bump()
    b.index.ensure(b.db)
    mongo_client[scratch][cache_version.COLLECTION].delete_many({})  # a reset database starts again at 0
    b.wait(cache_version.CHECK_SECONDS)
    b.index.ensure(b.db)
    assert b.index.full_builds == 2 and b.index.snapshot.version == 0


def test_the_nightly_full_rebuild(scratch):
    b = Process(scratch)
    b.index.ensure(b.db)
    assert not b.index.full_rebuild_due()
    built = b.index.snapshot.full_at
    assert b.index.full_rebuild_due(nightly_slot(built + timedelta(days=1)) + timedelta(minutes=1))
    assert nightly_slot(datetime(2026, 10, 22, 21, 29)) == datetime(2026, 10, 21, 21, 30)  # 03:00 IST
    assert nightly_slot(datetime(2026, 10, 22, 21, 30)) == datetime(2026, 10, 22, 21, 30)

    # A write that changed a listing without a bump is caught by the nightly rebuild.
    quiet = listing(b.db, "Unannounced Shower Head")
    b.index._install(dataclasses.replace(b.index.snapshot, full_at=built - timedelta(days=1)))
    b.index.ensure(b.db)
    assert b.index.full_builds == 2 and quiet.id in b.index.snapshot.product_ids
    assert not b.index.full_rebuild_due()


def test_the_nightly_rebuild_runs_in_the_background(scratch):
    b = Process(scratch, background=True)
    b.index.ensure(b.db)
    old = b.index.snapshot
    b.index._install(dataclasses.replace(old, full_at=old.full_at - timedelta(days=1)))
    b.index.query(b.db, "bucket")  # served from the current matrix; the rebuild starts beside it
    b.index.wait_for_rebuild(30)
    assert b.index.full_builds == 2 and not b.index.full_rebuild_due()


def test_two_processes_caching_the_same_new_text_is_harmless(scratch):
    db = Database(scratch)
    provider = get_provider()
    text = "Brand New Pipe Wrench. wrench"
    first = vectors(db, provider, [text])
    store = db.mongo["text_embeddings"]
    key = semantic_index.cache_key(provider.name, text)
    store.update_one({"_id": key}, {"$set": {"vector": b"\x00" * 4}})  # as if another process's, unusable here
    again = vectors(db, provider, [text])  # embeds it again; the insert meets the existing key
    assert np.array_equal(first, again) and store.count_documents({"_id": key}) == 1


# ---------------------------------------------------------------- the spelling vocabulary and synonyms


def test_the_spelling_vocabulary_follows_another_processs_change(scratch, monkeypatch):
    this = Process(scratch)
    monkeypatch.setattr(cache_version, "watcher", this.watcher)  # what search reads, on this test's clock
    db = Database(scratch)
    assert "galvanised" not in search._vocabulary(db)
    other = Process(scratch)
    listing(other.db, "Galvanised Nail Pack")
    other.bump()
    this.wait(cache_version.CHECK_SECONDS - 0.1)
    assert "galvanised" not in search._vocabulary(db)
    this.wait(0.1)
    assert "galvanised" in search._vocabulary(db)
    assert search.suggest_correction(db, "galvanized") == "galvanised"


def test_the_vocabulary_refreshes_only_the_changed_listings(scratch, monkeypatch):
    this = Process(scratch)
    monkeypatch.setattr(cache_version, "watcher", this.watcher)
    db = Database(scratch)
    search.reset_vocabulary()
    builds = dict(search.vocab_builds)
    search._vocabulary(db)
    assert search.vocab_builds["full"] == builds["full"] + 1

    guard = [True]
    real_read = db.catalog_items.find_raw

    def catalogue_read(*args, **kwargs):
        assert not guard[0], "a refresh must not read the catalogue"
        return real_read(*args, **kwargs)

    monkeypatch.setattr(db.catalog_items, "find_raw", catalogue_read)
    Process(scratch).bump()  # an admin verified a shop: no listing changed
    this.wait(cache_version.CHECK_SECONDS)
    before = search._vocabulary(db)
    assert search._vocab[scratch].version == 1 and search.vocab_builds["full"] == builds["full"] + 1

    other = Process(scratch)
    listing(other.db, "Ornamental Trellis Panel")
    bucket = other.db.products.find_one({"name": WORDS[0].title()})
    other.db.products.set(bucket, is_active=False)  # "steel" and "bucket" only came from this listing
    other.bump()
    this.wait(cache_version.CHECK_SECONDS)
    after = search._vocabulary(db)
    guard[0] = False
    assert search.vocab_builds == {"full": builds["full"] + 1, "refresh": builds["refresh"] + 2}
    assert {"ornamental", "trellis"} <= after - before and {"steel", "bucket"} <= before - after
    search.reset_vocabulary()
    assert search._vocabulary(db) == after  # the same words as a full build


def test_a_synonym_change_whose_read_fails_is_retried_within_5_seconds(db, ids, monkeypatch):
    other = Process()
    clock = [0.0]
    cache = synonyms.SynonymCache(clock=lambda: clock[0], watcher=other.watcher)
    before = cache.table(db.mongo)
    admin_id = db.users.find_one({"role": "admin"}).id
    entry = synonyms.create_entry(db, admin_id, "quaffle", ["ball"])
    try:
        real = synonyms.read_version

        def down(mongo):
            raise ConnectionError("database unreachable")

        monkeypatch.setattr(synonyms, "read_version", down)
        clock[0] += cache_version.CHECK_SECONDS
        other.wait(cache_version.CHECK_SECONDS)
        assert cache.table(db.mongo) == before  # the shared change is seen, its read fails: the old entries
        monkeypatch.setattr(synonyms, "read_version", real)
        clock[0] += synonyms.RETRY_SECONDS - 0.1
        assert "quaffle" not in cache.table(db.mongo)  # not retried on every search
        clock[0] += 0.1
        assert cache.table(db.mongo)["quaffle"] == ["ball"]  # retried soon, not at the next 30 s check
    finally:
        db.mongo["synonyms"].delete_one({"_id": entry["id"]})
        synonyms._bump(db)


def test_an_admins_synonym_reaches_another_process_within_5_seconds(db, ids):
    term = "zorbix"
    other = Process()
    clock = [0.0]
    cache = synonyms.SynonymCache(clock=lambda: clock[0], watcher=other.watcher)
    assert term not in cache.table(db.mongo)
    admin_id = db.users.find_one({"role": "admin"}).id
    entry = synonyms.create_entry(db, admin_id, term, ["broom"])
    try:
        clock[0] += cache_version.CHECK_SECONDS - 0.1
        other.wait(cache_version.CHECK_SECONDS - 0.1)
        assert term not in cache.table(db.mongo)
        clock[0] += 0.1
        other.wait(0.1)
        assert cache.table(db.mongo)[term] == ["broom"]  # long before the 30 s counters check
    finally:
        db.mongo["synonyms"].delete_one({"_id": entry["id"]})
        synonyms._bump(db)


# ---------------------------------------------------------------- the routes that bump


def test_owner_import_and_admin_changes_bump_the_listings_version(owner_a, admin, ids):
    db = Database()
    other = Process()
    other.index.ensure(other.db)
    was_verified = db.shops.get(ids["shop_a"]).is_verified
    # The admin switch goes through the verification rules (P3.3): turning it off suspends the shop, which
    # changes several fields, so the whole document is restored afterwards.
    shop_before = db.mongo["shops"].find_one({"_id": ids["shop_a"]})
    made: list[int] = []

    def version() -> int:
        return cache_version.read_versions(db.mongo).get("listings", 0)

    try:
        start = version()
        res = owner_a.post("/api/owner/products", json={
            "name": "Quixotic Drain Snake", "category_slug": "plumbing", "price": 210, "quantity": 2,
            "tags": ["drain", "snake"]}, headers=HEADERS)
        assert res.status_code == 200, res.text
        new_id = res.json()["id"]
        made.append(new_id)
        assert version() == start + 1
        other.wait(cache_version.CHECK_SECONDS)
        assert new_id in other.index.query(other.db, "drain snake")  # the other process, incrementally
        assert other.index.full_builds == 1

        rows = [{"row": 2, "action": "add_new", "name": "Quixotic Basin Plug", "category_slug": "plumbing",
                 "price": 35, "quantity": 4}]
        done = owner_a.post("/api/owner/import/commit", json={"rows": rows}, headers=HEADERS)
        assert done.status_code == 200, done.text
        plug = db.products.find_one({"name": "Quixotic Basin Plug"})
        made.append(plug.id)
        assert version() == start + 2  # one bump per commit, from the importer itself
        other.wait(cache_version.CHECK_SECONDS)
        assert plug.id in other.index.query(other.db, "basin plug")

        assert admin.patch(f"/api/admin/shops/{ids['shop_a']}", json={"is_verified": not was_verified},
                           headers=HEADERS).status_code == 200
        assert version() == start + 3

        # Both removed through the owner's route, so each removal moves updated_at as every real write does.
        for product_id in made:
            assert owner_a.delete(f"/api/owner/products/{product_id}", headers=HEADERS).status_code == 200
        assert version() == start + 5
        other.wait(cache_version.CHECK_SECONDS)
        other.index.ensure(other.db)
        assert not set(made) & set(other.index.snapshot.product_ids.tolist())
        assert other.index.full_builds == 1
        same_as_full_rebuild(other)
    finally:  # the session's shared data as it was: shop_a's verification, and no listing of this test's
        db.mongo["shops"].replace_one({"_id": ids["shop_a"]}, shop_before)
        db.mongo["products"].delete_many({"_id": {"$in": made}})
        db.mongo["inventory_events"].delete_many({"product_id": {"$in": made}})
        cache_version.bump(db.mongo, cache_version.LISTINGS)
        index.mark_dirty()  # the process-wide index rebuilds without the deleted listings
