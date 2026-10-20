"""Off-site backups and restore (database/backup.py, database/restore.py).

An in-memory fake stands in for the bucket, so no test touches a real one. Each test that writes gets its
own databases on the local test server, dropped afterwards: a small dataset is backed up from one and
restored into another.
"""
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from bson import Binary, Decimal128
from pydantic import SecretStr

from app.core.config import Settings
from app.core.database import Database
from app.core.database import client as mongo_client
from app.models import CatalogItem, Category, Product, Role, Shop, User
from database import backup, migrate, restore
from database.backup import (
    MANIFEST,
    RUNS_COLLECTION,
    LocalBackupStore,
    S3BackupStore,
    StoredSet,
    create_backup,
    list_sets,
    plan_prune,
    run_backup,
    set_id_for,
    set_key,
    sets_to_keep,
    store_problem,
)
from database.migrations import Migration, discover
from database.restore import RestoreFailed, RestoreRefused, restore_set

PREFIX = "test"
FAKE_HASH = "not-a-real-hash"  # no sign-in here, and hashing would only slow the tests


class FakeStore:
    """An in-memory bucket: key -> bytes."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_file(self, key, path):
        self.objects[key] = Path(path).read_bytes()

    def get_file(self, key, path):
        if key not in self.objects:
            raise FileNotFoundError(key)
        Path(path).write_bytes(self.objects[key])

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_keys(self, keys):
        for key in keys:
            self.objects.pop(key, None)


class BrokenStore(FakeStore):
    def put_file(self, key, path):
        raise ConnectionError("bucket unreachable")


@pytest.fixture
def scratch_dbs():
    names = []

    def make(tag):
        name = f"{os.environ['NEARSHOP_MONGODB_DB']}_{tag}{uuid.uuid4().hex[:6]}"
        names.append(name)
        return mongo_client[name]

    yield make
    for name in names:
        mongo_client.drop_database(name)


def seed_small(mongo) -> None:
    """A schema at the code's migration and a few documents in several collections, ids from counters."""
    migrate.run(mongo)
    db = Database(mongo.name)
    cat = db.categories.insert(Category(slug="plumbing", name="Plumbing", icon="Droplets", color_hue=210))
    item = db.catalog_items.insert(CatalogItem(slug="teflon-tape", name="PTFE Tape", brand="Astral",
                                               category_id=cat.id, typical_price=40, mrp=50, tags=["tape"]))
    owner, _ = db.users.insert_many([
        User(name="Owner", email="owner@example.com", password_hash=FAKE_HASH, role=Role.OWNER),
        User(name="Customer", email="customer@example.com", password_hash=FAKE_HASH, role=Role.CUSTOMER),
    ])
    shop = db.shops.insert(Shop(owner_id=owner.id, name="Shop", slug="shop", address_line="1 Road", city="Mysuru",
                                lat=12.3, lng=76.6, category_ids=[cat.id]))
    tape = Product(shop_id=shop.id, catalog_item_id=item.id, category_id=cat.id, name=item.name, keywords="tape",
                   price=40, mrp=50, quantity=5, low_stock_threshold=1)
    loose = [Product(shop_id=shop.id, category_id=cat.id, name=f"Loose item {i}", keywords="loose", price=10 + i,
                     quantity=i, low_stock_threshold=0) for i in range(3)]
    db.products.insert_many([tape, *loose])
    # BSON types the models never write, to show the copy is byte for byte.
    mongo["text_embeddings"].insert_one({"_id": "m:abc", "model": "m", "vector": Binary(b"\x00\x01\x02" * 8),
                                         "price": Decimal128("12.50")})


def sample(mongo, name):
    return mongo[name].find_one({}, sort=[("_id", 1)])


# ------------------------------------------------------------------ round trip


def test_round_trip_restores_counts_documents_counters_and_schema(scratch_dbs, tmp_path):
    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    store = FakeStore()

    row = run_backup(source, store, prefix=PREFIX, environment="test", keep_daily=30, keep_monthly=12, commit="abc123")
    assert row["outcome"] == "succeeded"
    assert row["size_bytes"] > 0 and row["documents"] > 0 and row["deleted_sets"] == []
    set_id = row["_id"]
    keys = store.list_keys(f"{PREFIX}/{set_id}/")
    assert set_key(PREFIX, set_id, MANIFEST) in keys
    assert set_key(PREFIX, set_id, "users.bson.gz") in keys
    assert set_key(PREFIX, set_id, RUNS_COLLECTION + ".bson.gz") not in keys

    with_docs = [n for n in source.list_collection_names() if n != RUNS_COLLECTION and source[n].count_documents({})]
    assert {"users", "shops", "products", "counters", "text_embeddings", "schema_migrations"} <= set(with_docs)

    manifest = restore.fetch_manifest(store, PREFIX, set_id, tmp_path)
    assert manifest["commit"] == "abc123" and manifest["database"] == source.name
    assert manifest["migration"] == discover()[-1].number
    assert manifest["collections"]["products"]["documents"] == 4
    assert len(manifest["collections"]["users"]["sha256"]) == 64
    assert list(manifest["collections"])[-1] == "counters"  # read last
    restore.fetch_files(store, PREFIX, manifest, tmp_path)
    summary = restore_set(target, manifest, tmp_path, batch_size=2)  # several batches per collection

    assert summary["counters_raised"] == {} and summary["created_from_manifest"] == []
    for name in [*with_docs, *[n for n in source.list_collection_names() if n not in with_docs]]:
        if name in (RUNS_COLLECTION, migrate.LOCK_COLLECTION):
            continue
        assert target[name].count_documents({}) == source[name].count_documents({}), name
        assert sample(target, name) == sample(source, name), name
        assert target[name].index_information() == source[name].index_information(), name
    assert list(target["counters"].find({}, sort=[("_id", 1)])) == list(source["counters"].find({}, sort=[("_id", 1)]))
    assert list(target["schema_migrations"].find()) == list(source["schema_migrations"].find())
    options = {c["name"]: c["options"] for c in source.list_collections()}
    for info in target.list_collections():
        if info["name"] != migrate.LOCK_COLLECTION:
            assert info["options"] == options[info["name"]], info["name"]  # validators included
    assert RUNS_COLLECTION not in target.list_collection_names()

    # The restored database hands out new ids after the restored ones.
    restored = Database(target.name)
    assert restored.next_id("products") == source["counters"].find_one({"_id": "products"})["seq"] + 1


def test_restore_raises_a_counter_behind_its_collection(scratch_dbs, tmp_path):
    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    source["counters"].update_one({"_id": "products"}, {"$set": {"seq": 1}})  # as if read before an insert
    store = FakeStore()
    manifest = create_backup(source, store, prefix=PREFIX, set_id="2026-10-20T213000Z", environment="test", commit="x")
    summary = _fetch_and_restore(store, manifest["set_id"], target, tmp_path)
    top = source["products"].find_one(sort=[("_id", -1)])["_id"]
    assert summary["counters_raised"] == {"products": top}
    assert target["counters"].find_one({"_id": "products"})["seq"] == top


def _fetch_and_restore(store, set_id, target, tmp: Path):
    manifest = restore.fetch_manifest(store, PREFIX, set_id, tmp)
    restore.fetch_files(store, PREFIX, manifest, tmp)
    return restore_set(target, manifest, tmp)


# ------------------------------------------------------------------ restore safeguards


def test_restore_refuses_a_non_empty_target(scratch_dbs, tmp_path):
    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    store = FakeStore()
    manifest = create_backup(source, store, prefix=PREFIX, set_id="2026-10-20T213000Z", environment="test", commit="x")
    target["notes"].insert_one({"_id": 1, "text": "someone's data"})

    with pytest.raises(RestoreRefused, match="not empty"):
        _fetch_and_restore(store, manifest["set_id"], target, tmp_path)
    assert target.list_collection_names() == ["notes"]  # nothing was written
    assert list(target["notes"].find()) == [{"_id": 1, "text": "someone's data"}]


def test_restore_refuses_a_damaged_file_before_writing(scratch_dbs, tmp_path):
    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    store = FakeStore()
    manifest = create_backup(source, store, prefix=PREFIX, set_id="2026-10-20T213000Z", environment="test", commit="x")
    key = set_key(PREFIX, manifest["set_id"], "users.bson.gz")
    store.objects[key] = store.objects[key][:-1] + bytes([store.objects[key][-1] ^ 1])

    loaded = restore.fetch_manifest(store, PREFIX, manifest["set_id"], tmp_path)
    with pytest.raises(RestoreRefused, match="users"):
        restore.fetch_files(store, PREFIX, loaded, tmp_path)
    assert target.list_collection_names() == []


def test_restore_refuses_a_set_newer_than_the_code(scratch_dbs, tmp_path):
    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    store = FakeStore()
    manifest = create_backup(source, store, prefix=PREFIX, set_id="2026-10-20T213000Z", environment="test", commit="x")
    only_baseline = [m for m in discover() if m.number == 1]
    manifest["migration"] = 2
    with pytest.raises(RestoreRefused, match="migration 2"):
        restore_set(target, manifest, tmp_path, migrations=only_baseline)
    assert target.list_collection_names() == []


def test_restore_refuses_a_missing_or_incomplete_set(tmp_path):
    store = FakeStore()
    store.objects[f"{PREFIX}/2026-10-20T213000Z/users.bson.gz"] = b"x"  # no manifest: incomplete
    with pytest.raises(RestoreRefused, match="no manifest"):
        restore.fetch_manifest(store, PREFIX, "2026-10-20T213000Z", tmp_path)
    with pytest.raises(RestoreRefused, match="no manifest"):
        restore.fetch_manifest(store, PREFIX, "2026-10-19T213000Z", tmp_path)


def test_restore_applies_migrations_newer_than_the_set(scratch_dbs, tmp_path):
    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    store = FakeStore()
    manifest = create_backup(source, store, prefix=PREFIX, set_id="2026-10-20T213000Z", environment="test", commit="x")
    calls = []

    def add_flag(mongo):
        calls.append(mongo.name)
        mongo["products"].update_many({"flag": {"$exists": False}}, {"$set": {"flag": True}})

    newer = [*discover(), Migration(len(discover()) + 1, "add_flag", add_flag)]
    loaded = restore.fetch_manifest(store, PREFIX, manifest["set_id"], tmp_path)
    restore.fetch_files(store, PREFIX, loaded, tmp_path)
    summary = restore_set(target, loaded, tmp_path, migrations=newer)
    assert calls == [target.name]  # on the restored documents, after the count check
    assert summary["migrations_applied_after"] == [newer[-1].number]
    assert target["products"].count_documents({"flag": True}) == 4


def test_production_name_needs_the_emergency_flag_and_the_typed_name():
    protected = restore.protected_names(environment="production", production_db="nearshop_prod", app_db="nearshop")
    assert protected == {"nearshop_prod", "nearshop"}
    assert restore.protected_names(environment="staging", production_db="nearshop_prod",
                                   app_db="nearshop") == {"nearshop_prod"}

    def never(_prompt):
        raise AssertionError("asked without --emergency")

    assert restore.name_problem("nearshop_drill", protected, emergency=False, ask=never) is None
    assert "production database" in restore.name_problem("nearshop", protected, emergency=False, ask=never)
    assert "does not match" in restore.name_problem("nearshop", protected, emergency=True, ask=lambda _: "nearshop_x")
    assert restore.name_problem("nearshop", protected, emergency=True, ask=lambda _: "nearshop") is None

    def no_terminal(_prompt):
        raise EOFError

    assert "nothing was typed" in restore.name_problem("nearshop", protected, emergency=True, ask=no_terminal)
    assert "--target-db" in restore.name_problem("", protected, emergency=False, ask=never)


def test_production_sets_restore_only_in_production():
    manifest = {"set_id": "2026-10-20T213000Z", "environment": "production"}
    assert "production" in restore.environment_problem(manifest, "staging")
    assert "production" in restore.environment_problem(manifest, "development")
    assert restore.environment_problem(manifest, "production") is None
    assert restore.environment_problem({**manifest, "environment": "staging"}, "development") is None


def test_restore_command_refuses_the_production_name_and_a_hosted_database(monkeypatch, tmp_path, capsys,
                                                                           scratch_dbs):
    from app.core.config import settings

    monkeypatch.setattr(settings, "backup_backend", "local")
    monkeypatch.setattr(settings, "backup_dir", tmp_path)
    assert restore.main(["--target-db", settings.production_mongodb_db], ask=lambda _: "") == 2
    assert "production database" in capsys.readouterr().err
    busy = scratch_dbs("bk_busy")
    busy["users"].insert_one({"_id": 1})
    assert restore.main(["--target-db", busy.name]) == 2
    assert "not empty" in capsys.readouterr().err

    monkeypatch.setattr(settings, "mongodb_uri", "mongodb+srv://cluster0.abcd1.mongodb.net/")
    assert restore.main(["--target-db", "nearshop_drill"]) == 2
    assert "not local" in capsys.readouterr().err
    assert backup.main([]) == 2
    assert "not local" in capsys.readouterr().err


def test_commands_refuse_when_backups_are_off(monkeypatch, capsys):
    from app.core.config import settings

    monkeypatch.setattr(settings, "backup_backend", "off")
    assert backup.main([]) == 2
    assert "NEARSHOP_BACKUP_BACKEND is off" in capsys.readouterr().err
    assert restore.main(["--target-db", "nearshop_drill"]) == 2


def test_commands_back_up_and_restore_through_a_local_directory(monkeypatch, tmp_path, scratch_dbs, capsys):
    """make backup and make restore: the same tools with a local directory as the store."""
    from app.core.config import settings

    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    monkeypatch.setattr(settings, "backup_backend", "local")
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(settings, "mongodb_db", source.name)
    assert backup.main([]) == 0
    assert '"outcome": "succeeded"' in capsys.readouterr().out
    assert source[RUNS_COLLECTION].count_documents({"outcome": "succeeded"}) == 1
    assert restore.main(["--target-db", target.name]) == 0
    assert '"total_seconds"' in capsys.readouterr().out
    assert target["products"].count_documents({}) == source["products"].count_documents({})
    assert restore.main(["--target-db", target.name]) == 2  # now it holds documents
    assert "not empty" in capsys.readouterr().err


# ------------------------------------------------------------------ runs and retention


def test_a_failed_backup_is_recorded_and_raised(scratch_dbs):
    source = scratch_dbs("bk_src")
    seed_small(source)
    with pytest.raises(ConnectionError):
        run_backup(source, BrokenStore(), prefix=PREFIX, environment="test", keep_daily=30, keep_monthly=12, commit="x")
    row = source[RUNS_COLLECTION].find_one()
    assert row["outcome"] == "failed" and "bucket unreachable" in row["error"] and row["finished_at"]


def _fake_set(store, moment, complete=True):
    set_id = set_id_for(moment)
    store.objects[set_key(PREFIX, set_id, "users.bson.gz")] = b"data"
    if complete:
        store.objects[set_key(PREFIX, set_id, MANIFEST)] = b"{}"
    return set_id


def test_backup_prunes_old_sets_by_the_retention_rule(scratch_dbs):
    source = scratch_dbs("bk_src")
    seed_small(source)
    store = FakeStore()
    now = datetime(2026, 10, 20, 21, 30, 0)
    nightly = [_fake_set(store, now - timedelta(days=d)) for d in range(1, 500)]  # every night for 16 months
    extra_today = _fake_set(store, now.replace(hour=9))  # an earlier set today
    stale_incomplete = _fake_set(store, now - timedelta(days=3, hours=1), complete=False)
    fresh_incomplete = _fake_set(store, now - timedelta(hours=2), complete=False)  # maybe still uploading
    store.objects[f"{PREFIX}/not-a-set/readme.txt"] = b"left alone"
    store.objects["other-env/2020-01-01T000000Z/manifest.json"] = b"{}"

    row = run_backup(source, store, prefix=PREFIX, environment="test", keep_daily=30, keep_monthly=12, now=now,
                     commit="x")
    remaining = {s.set_id for s in list_sets(store, PREFIX)}
    today = set_id_for(now)
    daily = {set_id_for(now - timedelta(days=d)) for d in range(1, 30)} | {today}
    # The first set of each month from November 2025 to October 2026 (October's is the 1st at 21:30).
    monthly = {set_id_for(datetime(y, m, 1, 21, 30)) for y, m in
               [(2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 11)]}
    assert remaining == daily | monthly | {fresh_incomplete}
    assert len(daily) == 30 and len(monthly) == 12
    assert extra_today not in remaining and stale_incomplete not in remaining
    assert set(row["deleted_sets"]) == ({*nightly, extra_today, stale_incomplete} - remaining)
    assert f"{PREFIX}/not-a-set/readme.txt" in store.objects
    assert "other-env/2020-01-01T000000Z/manifest.json" in store.objects
    assert not any(k.startswith(f"{PREFIX}/{sid}/") for sid in row["deleted_sets"] for k in store.objects)


def test_no_prune_keeps_every_set(scratch_dbs):
    source = scratch_dbs("bk_src")
    seed_small(source)
    store = FakeStore()
    now = datetime(2026, 10, 20, 21, 30, 0)
    old = _fake_set(store, now - timedelta(days=400))
    run_backup(source, store, prefix=PREFIX, environment="test", keep_daily=30, keep_monthly=12, now=now,
               do_prune=False, commit="x")
    assert old in {s.set_id for s in list_sets(store, PREFIX)}


def test_retention_rule_is_pure_and_keeps_the_newest_set():
    today = date(2026, 3, 15)
    ids = ["2026-03-15T213000Z", "2026-03-15T090000Z", "2026-03-01T213000Z", "2026-02-14T213000Z",
           "2026-02-03T213000Z", "2025-03-01T213000Z", "2025-04-02T213000Z", "2027-01-01T000000Z", "garbage"]
    keep = sets_to_keep(ids, today, keep_daily=30, keep_monthly=12)
    # Days 2026-02-14 to 2026-03-15 are the last 30 days; months 2025-04 to 2026-03 the last 12.
    assert keep == {"2026-03-15T213000Z", "2026-03-01T213000Z", "2026-02-14T213000Z", "2026-02-03T213000Z",
                    "2025-04-02T213000Z", "2027-01-01T000000Z"}
    # A long outage: the only complete set is years old, and it is kept.
    lonely = [StoredSet("2020-01-01T000000Z", datetime(2020, 1, 1), True, ("k",))]
    assert plan_prune(lonely, datetime(2026, 3, 15), keep_daily=30, keep_monthly=12) == []


# ------------------------------------------------------------------ stores and settings


def test_local_store_round_trip_and_containment(tmp_path):
    store = LocalBackupStore(tmp_path / "b")
    src = tmp_path / "f"
    src.write_bytes(b"abc")
    store.put_file("p/2026-10-20T213000Z/users.bson.gz", src)
    assert store.list_keys("p/") == ["p/2026-10-20T213000Z/users.bson.gz"]
    store.get_file("p/2026-10-20T213000Z/users.bson.gz", tmp_path / "g")
    assert (tmp_path / "g").read_bytes() == b"abc"
    store.delete_keys(["p/2026-10-20T213000Z/users.bson.gz"])
    assert store.list_keys("p/") == [] and not (tmp_path / "b" / "p").exists()
    with pytest.raises(ValueError):
        store.put_file("../escape", src)


class FakeS3Client:
    def __init__(self):
        self.calls = []

    def upload_file(self, filename, bucket, key):
        self.calls.append(("upload_file", bucket, key))

    def download_file(self, bucket, key, filename):
        self.calls.append(("download_file", bucket, key))

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, **kwargs):
                client.calls.append(("list", kwargs["Bucket"], kwargs["Prefix"]))
                return [{"Contents": [{"Key": "p/b"}, {"Key": "p/a"}]}, {}]

        return Paginator()

    def delete_objects(self, Bucket, Delete):
        self.calls.append(("delete_objects", Bucket, len(Delete["Objects"])))
        return {}


def test_s3_store_calls(tmp_path):
    fake = FakeS3Client()
    store = S3BackupStore("backups", fake)
    store.put_file("p/a", tmp_path / "x")
    store.get_file("p/a", tmp_path / "y")
    assert store.list_keys("p/") == ["p/a", "p/b"]
    store.delete_keys([f"p/{i}" for i in range(2500)])
    assert fake.calls == [("upload_file", "backups", "p/a"), ("download_file", "backups", "p/a"),
                          ("list", "backups", "p/"), ("delete_objects", "backups", 1000),
                          ("delete_objects", "backups", 1000), ("delete_objects", "backups", 500)]


def test_backup_settings():
    base = {"_env_file": None, "environment": "staging"}
    assert Settings(**base, backup_backend=" ").backup_backend == "off"
    assert Settings(**base).backup_set_prefix == "staging"
    assert Settings(**base, backup_prefix="/prod/").backup_set_prefix == "prod"
    with pytest.raises(ValueError):
        Settings(**base, backup_backend="ftp")
    with pytest.raises(ValueError):
        Settings(**base, backup_keep_daily=0)
    assert "NEARSHOP_BACKUP_DIR" in store_problem(Settings(**base, backup_backend="local"))
    missing = store_problem(Settings(**base, backup_backend="s3", backup_s3_bucket="b"))
    assert "NEARSHOP_BACKUP_S3_ENDPOINT" in missing and "NEARSHOP_BACKUP_S3_BUCKET" not in missing
    full = dict(backup_backend="s3", backup_s3_endpoint="https://x.r2.cloudflarestorage.com",
                backup_s3_bucket="nearshop-backups", backup_s3_access_key_id="id",
                backup_s3_secret_access_key=SecretStr("secret"))
    assert store_problem(Settings(**base, **full)) is None
    assert "photo bucket" in store_problem(Settings(**base, **full, media_s3_bucket="nearshop-backups"))


def test_restore_failure_after_writing_says_to_drop_the_target(scratch_dbs, tmp_path):
    source, target = scratch_dbs("bk_src"), scratch_dbs("bk_dst")
    seed_small(source)
    store = FakeStore()
    manifest = create_backup(source, store, prefix=PREFIX, set_id="2026-10-20T213000Z", environment="test", commit="x")
    loaded = restore.fetch_manifest(store, PREFIX, manifest["set_id"], tmp_path)
    restore.fetch_files(store, PREFIX, loaded, tmp_path)
    loaded["collections"]["products"]["documents"] += 1  # the manifest says one more than the file holds
    with pytest.raises(RestoreFailed, match="products 5 expected, 4 restored"):
        restore_set(target, loaded, tmp_path)
