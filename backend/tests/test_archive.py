"""Sales history archiving (database/archive.py, P8.3).

An in-memory fake stands in for the backup bucket, so no test touches a real one. Each test that writes gets
its own databases on the local test server, dropped afterwards.
"""
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bson import Decimal128

from app.core.config import Settings
from app.core.database import client as mongo_client
from database import archive, migrate
from database.archive import (
    RUNS_COLLECTION,
    ArchiveRefused,
    ArchiveVerifyFailed,
    cutoff_for,
    data_key,
    eligible_months,
    list_parts,
    manifest_key,
    restore_month,
    run_archive,
)
from database.backup import RUNS_COLLECTION as BACKUP_RUNS
from database.backup import list_sets, load_manifest, prune, set_id_for, set_key

PREFIX = "test"
NOW = datetime(2026, 10, 22, 23, 0, 0)  # cutoff 2025-04-01: March 2025 and earlier are eligible
DAY = timedelta(days=1)


class FakeStore:
    """An in-memory bucket: key -> bytes. events records every call in order."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.events: list[tuple[str, str]] = []

    def put_file(self, key, path):
        self.events.append(("put", key))
        self.objects[key] = Path(path).read_bytes()

    def get_file(self, key, path):
        self.events.append(("get", key))
        if key not in self.objects:
            raise FileNotFoundError(key)
        Path(path).write_bytes(self.objects[key])

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_keys(self, keys):
        for key in keys:
            self.objects.pop(key, None)


class BrokenStore(FakeStore):
    """Every upload fails."""

    def put_file(self, key, path):
        raise ConnectionError("bucket unreachable")


class ManifestFailsStore(FakeStore):
    """The data file uploads, its manifest does not."""

    def put_file(self, key, path):
        if key.endswith(archive.MANIFEST_SUFFIX):
            raise ConnectionError("bucket unreachable")
        super().put_file(key, path)


class CorruptingStore(FakeStore):
    """Uploads succeed but the stored bytes are damaged: only reading back can tell."""

    def put_file(self, key, path):
        super().put_file(key, path)
        data = self.objects[key]
        self.objects[key] = data[:-1] + bytes([data[-1] ^ 1])


class TruncatingStore(FakeStore):
    """Uploads succeed but only part of each file is kept."""

    def put_file(self, key, path):
        super().put_file(key, path)
        self.objects[key] = self.objects[key][: len(self.objects[key]) // 2]


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


def sale(_id, sold_at, **extra):
    return {"_id": _id, "shop_id": 1, "product_id": 10 + _id % 3, "catalog_item_id": None, "category_id": 2,
            "customer_id": None, "basket_id": f"b{_id}", "quantity": 1 + _id % 4, "unit_price": 40.0 + _id,
            "source": "import", "is_demo": False, "sold_at": sold_at, **extra}


def seed_sales(mongo) -> dict[str, list[dict]]:
    """sales_history at the code's schema, with rows in two eligible months and two recent ones. Returns the
    rows by month."""
    migrate.run(mongo)
    rows = {
        "2025-02": [sale(1, datetime(2025, 2, 1)), sale(2, datetime(2025, 2, 28, 23, 59, 59, 999000))],
        # One row with a BSON type the models never write, to show the copy is byte for byte.
        "2025-03": [sale(3, datetime(2025, 3, 1)), sale(4, datetime(2025, 3, 15, 12),
                                                         note=Decimal128("12.50"))]
                   + [sale(n, datetime(2025, 3, 31, 23, 59, 59)) for n in range(5, 12)],
        "2025-04": [sale(12, datetime(2025, 4, 1)), sale(13, datetime(2025, 4, 30))],  # not yet: 18 months
        "2026-10": [sale(14, datetime(2026, 10, 20))],
    }
    mongo["sales_history"].insert_many([r for month in rows.values() for r in month], bypass_document_validation=True)
    return rows


def backed_up(mongo, finished=NOW - timedelta(hours=1)):
    mongo[BACKUP_RUNS].insert_one({"_id": set_id_for(finished), "outcome": "succeeded",
                                   "started_at": finished - timedelta(minutes=5), "finished_at": finished})


def run(mongo, store, **kwargs):
    defaults = dict(prefix=PREFIX, environment="test", keep_months=18, backup_max_age=timedelta(hours=26), now=NOW,
                    commit="abc123", batch_size=3)
    return run_archive(mongo, store, **{**defaults, **kwargs})


def month_ids(mongo, month):
    start, end = archive.month_range(month)
    return sorted(d["_id"] for d in mongo["sales_history"].find({"sold_at": {"$gte": start, "$lt": end}}, {"_id": 1}))


# ------------------------------------------------------------------ months


def test_cutoff_is_whole_months_eighteen_back():
    assert cutoff_for(NOW, 18) == datetime(2025, 4, 1)
    assert cutoff_for(datetime(2026, 1, 1), 18) == datetime(2024, 7, 1)
    assert cutoff_for(datetime(2027, 6, 30, 23, 59), 24) == datetime(2025, 6, 1)
    # Every row it archives is older than 18 months whenever in the month the run happens.
    for day in range(1, 32):
        assert cutoff_for(datetime(2026, 10, day, 23), 18) <= datetime(2025, 4, min(day, 30), 23)
    assert archive.month_range("2024-12") == (datetime(2024, 12, 1), datetime(2025, 1, 1))
    with pytest.raises(ValueError):
        archive.month_start("2025-13")
    with pytest.raises(ValueError):
        archive.month_start("March")


def test_eligible_months(scratch_dbs):
    mongo = scratch_dbs("ar")
    seed_sales(mongo)
    assert eligible_months(mongo, cutoff_for(NOW, 18)) == [("2025-02", 2), ("2025-03", 9)]
    assert eligible_months(mongo, cutoff_for(NOW, 30)) == []


def test_archive_settings():
    base = {"_env_file": None, "environment": "staging"}
    assert Settings(**base).archive_keep_months == 18
    assert Settings(**base).archive_backup_max_age_hours == 26
    assert Settings(**base, archive_keep_months=24).archive_keep_months == 24
    with pytest.raises(ValueError, match="18 or more"):
        Settings(**base, archive_keep_months=12)
    with pytest.raises(ValueError):
        Settings(**base, archive_backup_max_age_hours=0)


# ------------------------------------------------------------------ the round trip


def test_round_trip_of_one_month(scratch_dbs, tmp_path):
    source, target = scratch_dbs("ar_src"), scratch_dbs("ar_dst")
    rows = seed_sales(source)
    backed_up(source)
    raw_before = {d["_id"]: d for d in source["sales_history"].find({}, sort=[("_id", 1)])}
    store = FakeStore()

    row = run(source, store)
    assert row["outcome"] == "succeeded" and row["cutoff"] == datetime(2025, 4, 1)
    assert [e["month"] for e in row["months"]] == ["2025-02", "2025-03"]
    assert row["documents"] == row["deleted"] == 11 and row["bytes"] > 0
    # The archived months left Atlas; the 18 months the models use did not.
    assert month_ids(source, "2025-02") == month_ids(source, "2025-03") == []
    assert month_ids(source, "2025-04") == [12, 13] and month_ids(source, "2026-10") == [14]

    part = set_id_for(NOW)
    m_key = manifest_key(PREFIX, "2025-03", part)
    assert set(store.objects) == {data_key(PREFIX, m, part) for m in ("2025-02", "2025-03")} | {
        manifest_key(PREFIX, m, part) for m in ("2025-02", "2025-03")}
    assert m_key == f"{PREFIX}/archive/sales_history/2025-03/2026-10-22T230000Z.manifest.json"
    manifest = load_manifest(store.objects[m_key].decode())
    assert manifest["month"] == "2025-03" and manifest["documents"] == 9 and manifest["commit"] == "abc123"
    assert manifest["range"] == {"from": datetime(2025, 3, 1), "to": datetime(2025, 4, 1)}
    assert manifest["first_id"] == 3 and manifest["last_id"] == 11 and len(manifest["sha256"]) == 64
    assert manifest["migration"] == migrate.discover()[-1].number and manifest["database"] == source.name
    assert manifest["bytes"] == len(store.objects[data_key(PREFIX, "2025-03", part)])

    summary = restore_month(target, store, prefix=PREFIX, month="2025-03", environment="test", work_dir=tmp_path,
                            batch_size=4)
    assert summary == {"month": "2025-03", "parts": 1, "documents": 9, "inserted": 9, "skipped_duplicates": 0,
                       "rows_in_target_month": 9, "target": target.name}
    restored = list(target["sales_history"].find({}, sort=[("_id", 1)]))
    assert restored == [raw_before[r["_id"]] for r in rows["2025-03"]]  # every field and type, as it was
    assert restored[1]["note"] == Decimal128("12.50")

    # Restoring again inserts nothing twice.
    again = restore_month(target, store, prefix=PREFIX, month="2025-03", environment="test", work_dir=tmp_path)
    assert again["inserted"] == 0 and again["skipped_duplicates"] == 9
    assert target["sales_history"].count_documents({}) == 9


def test_a_second_run_archives_only_rows_that_arrived_since(scratch_dbs, tmp_path):
    source, target = scratch_dbs("ar_src"), scratch_dbs("ar_dst")
    seed_sales(source)
    backed_up(source)
    store = FakeStore()
    run(source, store)
    assert run(source, store, now=NOW + timedelta(minutes=5))["months"] == []  # nothing new: no new part

    # An old sales report imported after the run: its rows go in a new part of March.
    later = NOW + DAY
    source["sales_history"].insert_one(sale(20, datetime(2025, 3, 9)))
    backed_up(source, later - timedelta(hours=1))
    row = run(source, store, now=later)
    assert [(e["month"], e["documents"]) for e in row["months"]] == [("2025-03", 1)]
    parts = [p.part_id for p in list_parts(store, PREFIX, "2025-03")]
    assert parts == [set_id_for(NOW), set_id_for(later)]

    summary = restore_month(target, store, prefix=PREFIX, month="2025-03", environment="test", work_dir=tmp_path)
    assert summary["parts"] == 2 and summary["inserted"] == 10 and summary["rows_in_target_month"] == 10


def test_a_month_in_two_parts_restores_each_row_once(scratch_dbs, tmp_path, monkeypatch):
    """A run that died after March's manifest, part way through its deletes: the next run archives the rest
    of March again, as a second part, and a restore puts each row back once."""
    source, target = scratch_dbs("ar_src"), scratch_dbs("ar_dst")
    seed_sales(source)
    backed_up(source)
    store = FakeStore()
    original = archive.delete_rows

    def dies_in_march(mongo, month, ids, batch_size=archive.BATCH_SIZE):
        if month != "2025-03":
            return original(mongo, month, ids, batch_size=batch_size)
        original(mongo, month, ids[:3], batch_size=batch_size)
        raise ConnectionError("lost the database mid-delete")

    monkeypatch.setattr(archive, "delete_rows", dies_in_march)
    with pytest.raises(ConnectionError):
        run(source, store)
    monkeypatch.setattr(archive, "delete_rows", original)
    assert month_ids(source, "2025-02") == [] and month_ids(source, "2025-03") == [6, 7, 8, 9, 10, 11]

    row = run(source, store, now=NOW + timedelta(hours=1))
    assert [(e["month"], e["documents"]) for e in row["months"]] == [("2025-03", 6)]
    assert month_ids(source, "2025-03") == []
    summary = restore_month(target, store, prefix=PREFIX, month="2025-03", environment="test", work_dir=tmp_path)
    assert (summary["parts"], summary["documents"], summary["inserted"], summary["skipped_duplicates"]) == (2, 15, 9, 6)
    assert sorted(d["_id"] for d in target["sales_history"].find()) == list(range(3, 12))


# ------------------------------------------------------------------ nothing is deleted before a verified upload


def test_rows_are_deleted_only_after_a_verified_upload(scratch_dbs):
    source = scratch_dbs("ar_src")
    seed_sales(source)
    backed_up(source)

    class WatchingStore(FakeStore):
        """Every call happens while the month's rows are all still in the database."""

        def _check(self, key):
            month = key.split("/")[-2]
            assert len(month_ids(source, month)) == {"2025-02": 2, "2025-03": 9}[month], key

        def put_file(self, key, path):
            self._check(key)
            super().put_file(key, path)

        def get_file(self, key, path):
            self._check(key)
            super().get_file(key, path)

    store = WatchingStore()
    run(source, store)
    part = set_id_for(NOW)
    march = [(kind, key) for kind, key in store.events if "/2025-03/" in key]
    d_key, m_key = data_key(PREFIX, "2025-03", part), manifest_key(PREFIX, "2025-03", part)
    # Uploaded, read back, manifest uploaded and read back: and only then deleted.
    assert march == [("put", d_key), ("get", d_key), ("put", m_key), ("get", m_key)]
    assert month_ids(source, "2025-03") == []


@pytest.mark.parametrize("store_class, error", [
    (BrokenStore, ConnectionError),
    (ManifestFailsStore, ConnectionError),
    (CorruptingStore, ArchiveVerifyFailed),
    (TruncatingStore, ArchiveVerifyFailed),
])
def test_a_failed_upload_deletes_nothing(scratch_dbs, store_class, error):
    source = scratch_dbs("ar_src")
    seed_sales(source)
    backed_up(source)
    before = list(source["sales_history"].find({}, sort=[("_id", 1)]))

    with pytest.raises(error):
        run(source, store_class())
    assert list(source["sales_history"].find({}, sort=[("_id", 1)])) == before
    row = source[RUNS_COLLECTION].find_one()
    assert row["outcome"] == "failed" and row["error"] and row["finished_at"] and row["months"] == []


def test_a_failure_in_a_later_month_keeps_the_earlier_month_archived(scratch_dbs):
    source = scratch_dbs("ar_src")
    seed_sales(source)
    backed_up(source)

    class MarchFails(FakeStore):
        def put_file(self, key, path):
            if "/2025-03/" in key:
                raise ConnectionError("bucket unreachable")
            super().put_file(key, path)

    with pytest.raises(ConnectionError):
        run(source, MarchFails())
    assert month_ids(source, "2025-02") == [] and len(month_ids(source, "2025-03")) == 9
    row = source[RUNS_COLLECTION].find_one()
    assert row["outcome"] == "failed" and [e["month"] for e in row["months"]] == ["2025-02"]


def test_an_incomplete_part_is_never_restored(scratch_dbs, tmp_path):
    source, target = scratch_dbs("ar_src"), scratch_dbs("ar_dst")
    seed_sales(source)
    backed_up(source)
    store = ManifestFailsStore()
    with pytest.raises(ConnectionError):
        run(source, store)
    assert [p.complete for p in list_parts(store, PREFIX)] == [False]
    with pytest.raises(ArchiveRefused, match="no complete archived part"):
        restore_month(target, store, prefix=PREFIX, month="2025-02", environment="test", work_dir=tmp_path)
    assert target.list_collection_names() == []


def test_a_damaged_part_is_refused_before_anything_is_restored(scratch_dbs, tmp_path):
    source, target = scratch_dbs("ar_src"), scratch_dbs("ar_dst")
    seed_sales(source)
    backed_up(source)
    store = FakeStore()
    run(source, store)
    key = data_key(PREFIX, "2025-03", set_id_for(NOW))
    store.objects[key] = store.objects[key][:-1] + bytes([store.objects[key][-1] ^ 1])
    with pytest.raises(ArchiveRefused, match="does not match its manifest"):
        restore_month(target, store, prefix=PREFIX, month="2025-03", environment="test", work_dir=tmp_path)
    assert target.list_collection_names() == []


# ------------------------------------------------------------------ safeguards


def test_archiving_needs_a_recent_backup(scratch_dbs):
    source = scratch_dbs("ar_src")
    seed_sales(source)
    store = FakeStore()
    with pytest.raises(ArchiveRefused, match="no backup"):
        run(source, store)
    backed_up(source, NOW - timedelta(hours=30))
    with pytest.raises(ArchiveRefused, match="more than 26 hours ago"):
        run(source, store)
    source[BACKUP_RUNS].insert_one({"_id": "x", "outcome": "failed", "started_at": NOW, "finished_at": NOW})
    with pytest.raises(ArchiveRefused):
        run(source, store)  # a failed backup does not count
    assert store.objects == {} and RUNS_COLLECTION not in source.list_collection_names()
    assert source["sales_history"].count_documents({}) == 14


def test_production_archives_restore_only_in_production(scratch_dbs, tmp_path):
    source, target = scratch_dbs("ar_src"), scratch_dbs("ar_dst")
    seed_sales(source)
    backed_up(source)
    store = FakeStore()
    run(source, store, environment="production")
    for environment in ("staging", "development"):
        with pytest.raises(ArchiveRefused, match="archived in production"):
            restore_month(target, store, prefix=PREFIX, month="2025-02", environment=environment, work_dir=tmp_path)
    assert target.list_collection_names() == []
    assert restore_month(target, store, prefix=PREFIX, month="2025-02", environment="production",
                         work_dir=tmp_path)["inserted"] == 2


def test_restore_refuses_the_live_and_production_databases():
    problem = archive.target_problem
    assert "scratch database" in problem("nearshop", live_db="nearshop", production_db="nearshop_prod",
                                         environment="staging")
    assert "scratch database" in problem("nearshop_prod", live_db="nearshop", production_db="nearshop_prod",
                                         environment="development")
    assert "--target-db" in problem("", live_db="nearshop", production_db="nearshop_prod", environment="test")
    assert problem("nearshop_sales_2025_03", live_db="nearshop", production_db="nearshop_prod",
                   environment="production") is None


def test_backup_retention_never_deletes_archived_months(scratch_dbs):
    source = scratch_dbs("ar_src")
    seed_sales(source)
    backed_up(source)
    store = FakeStore()
    run(source, store)
    archived = dict(store.objects)
    old_set = set_key(PREFIX, "2020-01-01T000000Z", "manifest.json")
    store.objects[old_set] = b"{}"
    assert [s.set_id for s in list_sets(store, PREFIX)] == ["2020-01-01T000000Z"]  # archive keys are not sets
    prune(store, PREFIX, datetime(2030, 1, 1), keep_daily=1, keep_monthly=1)
    assert store.objects == {**archived, old_set: b"{}"}  # the only set is the newest, so it stays too


# ------------------------------------------------------------------ the command


def test_command_refuses_without_a_store_or_with_a_hosted_database(monkeypatch, capsys):
    from app.core.config import settings

    monkeypatch.setattr(settings, "backup_backend", "off")
    assert archive.main([]) == 2
    assert "NEARSHOP_BACKUP_BACKEND is off" in capsys.readouterr().err
    assert archive.main(["--restore", "2025-03", "--target-db", settings.mongodb_db]) == 2
    assert "scratch database" in capsys.readouterr().err
    assert archive.main(["--restore", "March", "--target-db", "x"]) == 2
    assert "YYYY-MM" in capsys.readouterr().err
    monkeypatch.setattr(settings, "mongodb_uri", "mongodb+srv://cluster0.abcd1.mongodb.net/")
    assert archive.main(["--plan"]) == 2
    assert "not local" in capsys.readouterr().err
    monkeypatch.setattr(settings, "environment", "staging")
    assert archive.main(["--skip-backup-check"]) == 2


def test_command_archives_and_restores_through_a_local_directory(monkeypatch, tmp_path, scratch_dbs, capsys):
    """make archive: the same tool with a local directory as the store."""
    from app.core.config import settings

    source, target = scratch_dbs("ar_src"), scratch_dbs("ar_dst")
    # Dated from now, as the command uses the clock: two rows 20 months old, one row from this month.
    old = archive.add_months(datetime.now(), -20)
    migrate.run(source)
    source["sales_history"].insert_many([sale(1, old), sale(2, old + DAY), sale(3, datetime.now() - DAY)])
    month = archive.month_label(old)
    monkeypatch.setattr(settings, "backup_backend", "local")
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(settings, "mongodb_db", source.name)

    assert archive.main(["--plan"]) == 0
    assert f'"months": [{{"month": "{month}", "rows": 2}}]' in capsys.readouterr().out
    assert archive.main([]) == 2  # no backup yet
    assert "no backup" in capsys.readouterr().err
    assert archive.main(["--skip-backup-check"]) == 0
    out = capsys.readouterr().out
    assert '"outcome": "succeeded"' in out and f'"months": ["{month}"]' in out and '"deleted": 2' in out
    assert source["sales_history"].count_documents({}) == 1
    assert archive.main(["--list"]) == 0
    assert f"{month}  part " in capsys.readouterr().out
    assert list((tmp_path / "backups" / "test" / "archive" / "sales_history" / month).iterdir())
    assert archive.main(["--restore", month, "--target-db", target.name]) == 0
    assert '"inserted": 2' in capsys.readouterr().out
    assert sorted(d["_id"] for d in target["sales_history"].find()) == [1, 2]


def test_archive_cron_jobs_are_monthly_and_read_their_api():
    yaml = pytest.importorskip("yaml")
    blueprint = yaml.safe_load((Path(__file__).resolve().parents[2] / "render.yaml").read_text(encoding="utf-8"))
    services = {s["name"]: s for s in blueprint["services"]}
    for env, api, branch in (("staging", "nearshop-api-staging", "main"), ("production", "nearshop-api", "release")):
        cron = services[f"nearshop-archive-{env}"]
        assert cron["type"] == "cron" and cron["branch"] == branch == services[api]["branch"]
        assert cron["schedule"] == "0 23 1 * *"  # monthly, after the 21:30 UTC backup
        assert cron["startCommand"] == "uv run --no-dev python -m database.archive"
        vars_ = {v["key"]: v for v in cron["envVars"]}
        assert vars_["NEARSHOP_ENVIRONMENT"]["value"] == env
        assert vars_["NEARSHOP_BACKUP_PREFIX"]["value"] == env  # the same prefix as the backups
        for key in ("NEARSHOP_MONGODB_URI", "NEARSHOP_MEDIA_S3_BUCKET"):
            assert vars_[key]["fromService"] == {"type": "web", "name": api, "envVarKey": key}
        for key in ("NEARSHOP_BACKUP_BACKEND", "NEARSHOP_BACKUP_S3_BUCKET", "NEARSHOP_BACKUP_S3_SECRET_ACCESS_KEY"):
            assert vars_[key]["sync"] is False
