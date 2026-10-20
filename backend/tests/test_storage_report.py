"""Storage budget (P2.2): the daily snapshot under a lease, the projection, and the admin endpoint."""
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.core.database import Database
from app.main import app
from app.services import analytics
from app.services.analytics import MB, STORAGE_SNAPSHOTS, project_storage, record_storage_snapshot, storage_report
from database import schema

DAY = date(2026, 10, 20)
LIMIT = 512 * MB


@pytest.fixture
def snapshots(ids):
    coll = Database().mongo[STORAGE_SNAPSHOTS]
    coll.delete_many({})
    yield coll
    coll.delete_many({})


def snap(day: date, total_mb: float, collections: list[dict] | None = None, limit_mb: int = 512) -> dict:
    total = int(total_mb * MB)
    return {"_id": day.isoformat(), "taken_at": datetime.combine(day, datetime.min.time()), "limit_mb": limit_mb,
            "data_bytes": total, "index_bytes": 0, "total_bytes": total,
            "collections": collections or [{"name": "sales_history", "documents": 1, "data_bytes": total,
                                            "index_bytes": 0}]}


# ---------------------------------------------------------------- the projection (pure)


def test_steady_growth_projects_the_day_85_percent_is_reached():
    # 200 MB today after 2 MB a day for 30 days; 85% of 512 MB is 435.2 MB: 235.2 MB to go, so 118 days.
    points = [(DAY - timedelta(days=30 - i), (140 + 2 * i) * MB) for i in range(31)]
    p = project_storage(points, LIMIT)
    assert p["projection"] == "projected" and p["growth_days"] == 30
    assert p["growth_bytes_per_day"] == pytest.approx(2 * MB)
    assert p["projected_date"] == (DAY + timedelta(days=118)).isoformat()


def test_the_projection_rounds_up_to_the_first_day_at_or_over_85_percent():
    limit, target = 100 * MB, 85 * MB
    p = project_storage([(DAY - timedelta(days=1), target - 3 * MB), (DAY, target - 2 * MB)], limit)
    assert p["projected_date"] == (DAY + timedelta(days=2)).isoformat()  # 1 MB a day, 2 MB to go
    p = project_storage([(DAY - timedelta(days=1), target - 2 * MB - MB // 2 - MB // 2), (DAY, target - 2 * MB - MB // 2)],
                        limit)
    assert p["projected_date"] == (DAY + timedelta(days=5)).isoformat()  # 0.5 MB a day, 2.5 MB to go: 5 days


def test_zero_growth_has_no_date():
    points = [(DAY - timedelta(days=i), 193 * MB) for i in range(10)]
    p = project_storage(points, LIMIT)
    assert p == {"growth_bytes_per_day": 0, "growth_days": 9, "projection": "not_growing", "projected_date": None}


def test_shrinking_storage_has_no_date():
    p = project_storage([(DAY - timedelta(days=5), 200 * MB), (DAY, 150 * MB)], LIMIT)
    assert p["projection"] == "not_growing" and p["projected_date"] is None
    assert p["growth_bytes_per_day"] == pytest.approx(-10 * MB)


def test_growth_uses_only_the_last_30_days():
    # Fast growth 40 days ago, then flat for the last 30 days: no date.
    points = [(DAY - timedelta(days=40), 10 * MB), (DAY - timedelta(days=30), 190 * MB), (DAY, 190 * MB)]
    p = project_storage(points, LIMIT)
    assert p["growth_days"] == 30 and p["projection"] == "not_growing"
    # Points in any order; the oldest point inside the window is the base.
    p = project_storage([(DAY, 220 * MB), (DAY - timedelta(days=45), 0), (DAY - timedelta(days=20), 200 * MB)], LIMIT)
    assert p["growth_days"] == 20 and p["growth_bytes_per_day"] == pytest.approx(1 * MB)


def test_already_over_85_percent_is_reached_today():
    p = project_storage([(DAY - timedelta(days=1), 430 * MB), (DAY, 440 * MB)], LIMIT)
    assert p["projection"] == "reached" and p["projected_date"] == DAY.isoformat()
    # Even with no growth figure yet.
    assert project_storage([(DAY, 500 * MB)], LIMIT)["projection"] == "reached"


def test_one_snapshot_is_not_enough_and_none_is_no_data():
    p = project_storage([(DAY, 193 * MB)], LIMIT)
    assert p["projection"] == "insufficient" and p["growth_bytes_per_day"] is None and p["projected_date"] is None
    assert project_storage([], LIMIT)["projection"] == "no_data"


def test_growth_too_slow_for_ten_years_has_no_date():
    p = project_storage([(DAY - timedelta(days=30), 100 * MB), (DAY, 100 * MB + 30)], LIMIT)  # 1 byte a day
    assert p["projection"] == "beyond_horizon" and p["projected_date"] is None


# ---------------------------------------------------------------- the snapshot job


def test_the_snapshot_is_recorded_once_a_day_with_every_collection(snapshots):
    mongo = Database().mongo
    now = datetime(2026, 10, 20, 19, 0)  # 00:30 IST on 21 Oct: the IST day names the snapshot
    first = record_storage_snapshot(mongo, now, limit_mb=512)
    assert first["recorded"] is True and first["snapshot"] == "2026-10-21"
    assert record_storage_snapshot(mongo, now + timedelta(hours=5), limit_mb=512) == {
        "snapshot": "2026-10-21", "recorded": False}
    doc = snapshots.find_one({"_id": "2026-10-21"})
    names = {c["name"] for c in doc["collections"]}
    assert {"products", "users", "shops"} <= names and not any(n.startswith("system.") for n in names)
    assert doc["total_bytes"] == doc["data_bytes"] + doc["index_bytes"] > 0
    assert doc["data_bytes"] == sum(c["data_bytes"] for c in doc["collections"])
    assert doc["index_bytes"] == sum(c["index_bytes"] for c in doc["collections"])
    totals = [c["data_bytes"] + c["index_bytes"] for c in doc["collections"]]
    assert totals == sorted(totals, reverse=True)
    products = next(c for c in doc["collections"] if c["name"] == "products")
    assert products["documents"] == mongo["products"].count_documents({}) and products["index_bytes"] > 0


def test_the_snapshot_warns_at_70_and_errors_at_85_percent(snapshots, caplog, monkeypatch):
    sizes = iter([60, 75, 90])  # MB, against a 100 MB limit

    def fake_sizes(_mongo):
        mb = next(sizes)
        return [{"name": "sales_history", "documents": 1, "data_bytes": (mb - 1) * MB, "index_bytes": MB}]

    monkeypatch.setattr(analytics, "collection_sizes", fake_sizes)
    mongo = Database().mongo
    with caplog.at_level(logging.WARNING, logger="nearshop.storage"):
        results = [record_storage_snapshot(mongo, datetime(2026, 1, d, 6), limit_mb=100) for d in (1, 2, 3)]
    assert [r["used_pct"] for r in results] == [60, 75, 90]
    logged = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "nearshop.storage"]
    assert logged == [(logging.WARNING, "database storage is above 70% of the 100 MB limit: 75.0% used"),
                      (logging.ERROR, "database storage is above 85% of the 100 MB limit: 90.0% used")]


def test_the_storage_job_runs_under_its_lease_once_a_day(snapshots):
    mongo = Database().mongo
    assert jobs.JOBS["snapshot_storage"] is jobs.snapshot_storage
    mongo[jobs.LEASES].delete_one({"_id": "snapshot_storage"})
    a = jobs.Scheduler(mongo, jobs={"snapshot_storage": jobs.snapshot_storage}, holder="process-a")
    b = jobs.Scheduler(mongo, jobs={"snapshot_storage": jobs.snapshot_storage}, holder="process-b")
    assert a.tick() == {"snapshot_storage": True}
    assert b.tick() == {"snapshot_storage": False}  # a holds the lease
    assert snapshots.count_documents({}) == 1
    first = mongo[jobs.LEASES].find_one({"_id": "snapshot_storage"})["last_result"]
    assert first["ok"] is True and first["recorded"] is True
    a.tick()
    again = mongo[jobs.LEASES].find_one({"_id": "snapshot_storage"})["last_result"]
    assert again["recorded"] is False and snapshots.count_documents({}) == 1
    a.shutdown()
    b.shutdown()


def test_the_validator_rejects_a_malformed_snapshot(snapshots):
    from pymongo.errors import WriteError

    assert "storage_snapshots" in schema.VALIDATORS
    with pytest.raises(WriteError):
        snapshots.insert_one({**snap(DAY, 10), "_id": "20 Oct"})
    with pytest.raises(WriteError):
        snapshots.insert_one({**snap(DAY, 10), "total_bytes": -1})


# ---------------------------------------------------------------- the report and the endpoint


def test_the_report_on_synthetic_snapshots(snapshots):
    cols = [{"name": f"c{i}", "documents": i, "data_bytes": i * MB, "index_bytes": MB} for i in range(7, 0, -1)]
    snapshots.insert_many([snap(DAY - timedelta(days=30 - i), 140 + 2 * i) for i in range(30)])
    snapshots.insert_one(snap(DAY, 200, cols))
    r = storage_report(Database().mongo, limit_mb=512)
    assert r["snapshot_day"] == DAY.isoformat() and r["total_mb"] == 200 and r["used_pct"] == round(20000 / 512, 1)
    assert r["growth_mb_per_day"] == 2 and r["growth_days"] == 30
    assert r["projection"] == "projected" and r["projected_85_date"] == (DAY + timedelta(days=118)).isoformat()
    assert r["thresholds"] == [{"pct": 70, "mb": 358.4}, {"pct": 85, "mb": 435.2}]
    assert [c["name"] for c in r["largest_collections"]] == ["c7", "c6", "c5", "c4", "c3"]
    assert r["largest_collections"][0] == {"name": "c7", "documents": 7, "data_mb": 7, "index_mb": 1, "total_mb": 8}


def test_the_report_with_zero_growth_and_with_no_snapshots(snapshots):
    empty = storage_report(Database().mongo, limit_mb=5120)
    assert empty["snapshot_day"] is None and empty["projection"] == "no_data" and empty["projected_85_date"] is None
    assert empty["largest_collections"] == [] and empty["thresholds"][1] == {"pct": 85, "mb": 4352.0}
    snapshots.insert_many([snap(DAY - timedelta(days=i), 193) for i in range(5)])
    flat = storage_report(Database().mongo, limit_mb=512)
    assert flat["growth_mb_per_day"] == 0 and flat["projection"] == "not_growing" and flat["projected_85_date"] is None


def test_the_endpoint_uses_the_configured_limit(admin, snapshots, monkeypatch):
    monkeypatch.setattr(analytics.settings, "storage_limit_mb", 5120)
    snapshots.insert_many([snap(DAY - timedelta(days=10), 1000), snap(DAY, 1100)])
    res = admin.get("/api/admin/storage")
    assert res.status_code == 200, res.text
    body = res.json()
    # 10 MB a day; 85% of 5120 MB is 4352 MB: 3252 MB to go, 326 days (rounded up).
    assert body["limit_mb"] == 5120 and body["growth_mb_per_day"] == 10
    assert body["projected_85_date"] == (DAY + timedelta(days=326)).isoformat()


def test_the_storage_endpoint_is_admin_only(ids, customer, owner_a, snapshots):
    assert customer.get("/api/admin/storage").status_code == 403
    assert owner_a.get("/api/admin/storage").status_code == 403
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/admin/storage").status_code == 401


def test_the_blueprint_sets_each_tier_limit_and_pings_the_backup_heartbeat_only_after_success():
    yaml = pytest.importorskip("yaml")
    blueprint = yaml.safe_load((Path(__file__).resolve().parents[2] / "render.yaml").read_text(encoding="utf-8"))
    services = {s["name"]: s for s in blueprint["services"]}

    def env(name: str) -> dict:
        return {v["key"]: v for v in services[name]["envVars"]}

    assert env("nearshop-api-staging")["NEARSHOP_STORAGE_LIMIT_MB"]["value"] == "512"  # M0
    assert env("nearshop-api")["NEARSHOP_STORAGE_LIMIT_MB"]["value"] == "5120"  # Flex
    for job in ("nearshop-backup-staging", "nearshop-backup-production"):
        command = services[job]["startCommand"]
        assert command.startswith("uv run --no-dev python -m database.backup &&")
        assert env(job)["NEARSHOP_BACKUP_HEARTBEAT_URL"]["sync"] is False
