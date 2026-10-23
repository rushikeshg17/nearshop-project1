"""Background jobs under a lease, lazy stale-order cancellation, and retraining outside the API (P1.7).

Time is a controllable clock: no test sleeps."""
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import jobs
from app.api.routes.ai import _card_status
from app.core.database import Database, utcnow
from app.core.errors import Conflict
from app.services.orders import PENDING_TIMEOUT_MINUTES
from database import schema
from database.migrations import latest_number, m0002_job_leases
from tests.conftest import CENTER, HEADERS

T0 = datetime(2026, 10, 20, 12, 0, 0)
TRAINING_LIBRARIES = ("sklearn", "pandas", "gensim", "mlxtend")


class Clock:
    def __init__(self, at: datetime = T0):
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def recorder(runs: list, name: str):
    def job():
        runs.append(name)
        return {"by": name}
    return job


@pytest.fixture
def mongo(ids):
    return Database().mongo


@pytest.fixture
def requests_coll(ids):
    coll = Database().mongo[jobs.RETRAIN_REQUESTS]
    coll.delete_many({})
    yield coll
    coll.delete_many({})


# ---------------------------------------------------------------- leases


def test_only_the_lease_holder_runs_the_sweep_across_two_minutes(mongo):
    clock, runs = Clock(), []
    a = jobs.Scheduler(mongo, jobs={"sweep_two": recorder(runs, "a")}, holder="process-a", clock=clock)
    b = jobs.Scheduler(mongo, jobs={"sweep_two": recorder(runs, "b")}, holder="process-b", clock=clock)
    heartbeats = []
    for _ in range(5):  # ticks at 0, 30, 60, 90 and 120 s: a simulated two-minute window
        assert a.tick() == {"sweep_two": True}
        assert b.tick() == {"sweep_two": False}
        lease = mongo[jobs.LEASES].find_one({"_id": "sweep_two"})
        heartbeats.append(lease["last_run_at"])
        assert lease["holder"] == "process-a" and lease["expires_at"] == clock.now + jobs.LEASE_TTL
        assert (clock.now - lease["last_run_at"]) <= timedelta(seconds=90)
        clock.advance(jobs.TICK_SECONDS)
    assert runs == ["a"] * 5  # once per tick, in one process only
    assert heartbeats == [T0 + timedelta(seconds=30 * i) for i in range(5)]
    lease = mongo[jobs.LEASES].find_one({"_id": "sweep_two"})
    assert lease["acquired_at"] == T0 and lease["last_result"] == {"ok": True, "by": "a"}


def test_another_holder_takes_over_after_the_first_stops_renewing(mongo):
    clock, runs = Clock(), []
    a = jobs.Scheduler(mongo, jobs={"sweep_takeover": recorder(runs, "a")}, holder="process-a", clock=clock)
    b = jobs.Scheduler(mongo, jobs={"sweep_takeover": recorder(runs, "b")}, holder="process-b", clock=clock)
    assert a.tick()["sweep_takeover"] and not b.tick()["sweep_takeover"]
    # a stops (killed: no clean shutdown). Its lease, renewed at 0 s, expires at 60 s.
    clock.advance(30)
    assert not b.tick()["sweep_takeover"]
    clock.advance(30)
    assert b.tick()["sweep_takeover"]
    lease = mongo[jobs.LEASES].find_one({"_id": "sweep_takeover"})
    assert lease["holder"] == "process-b" and lease["acquired_at"] == clock.now
    # a comes back: it no longer holds the lease, and b keeps it while it renews.
    clock.advance(30)
    assert not a.tick()["sweep_takeover"] and b.tick()["sweep_takeover"]
    assert runs == ["a", "b", "b"]
    # The heartbeat was never older than 90 s, even across the takeover.
    assert clock.now - lease["last_run_at"] <= timedelta(seconds=90)


def test_a_clean_shutdown_hands_the_lease_over_at_once(mongo):
    clock, runs = Clock(), []
    a = jobs.Scheduler(mongo, jobs={"sweep_shutdown": recorder(runs, "a")}, holder="process-a", clock=clock)
    b = jobs.Scheduler(mongo, jobs={"sweep_shutdown": recorder(runs, "b")}, holder="process-b", clock=clock)
    assert a.tick()["sweep_shutdown"]
    a.shutdown()
    assert b.tick()["sweep_shutdown"]
    assert runs == ["a", "b"]


def test_shutdown_waits_for_a_tick_in_progress_before_releasing(mongo, monkeypatch):
    """A tick still running at shutdown must not take or renew a lease after the release (it would make
    the next process wait for the lease to expire): shutdown waits for it, then releases every lease."""
    clock, entered, go_on, released = Clock(), threading.Event(), threading.Event(), threading.Event()

    def slow():
        entered.set()
        go_on.wait(10)

    real_release = jobs.release_lease

    def release(*args):
        released.set()
        real_release(*args)

    monkeypatch.setattr(jobs, "release_lease", release)
    for job in ("sweep_slow", "sweep_after"):
        mongo[jobs.LEASES].delete_one({"_id": job})
    s = jobs.Scheduler(mongo, jobs={"sweep_slow": slow, "sweep_after": lambda: None}, holder="process-a",
                       clock=clock).start(seconds=3600)  # one tick, at once
    assert entered.wait(10)
    stopper = threading.Thread(target=s.shutdown)
    stopper.start()
    assert not released.wait(0.3)  # nothing released while the tick (and its next lease) is still to come
    go_on.set()
    stopper.join(10)
    assert not stopper.is_alive() and released.is_set()
    for job in ("sweep_slow", "sweep_after"):  # both free at once: another process takes them on its next tick
        assert mongo[jobs.LEASES].find_one({"_id": job})["expires_at"] <= clock.now
    b = jobs.Scheduler(mongo, jobs={"sweep_slow": lambda: None, "sweep_after": lambda: None}, holder="process-b",
                       clock=clock)
    assert b.tick() == {"sweep_slow": True, "sweep_after": True}


def test_each_process_marks_its_search_index_stale_after_a_retrain(ids, requests_coll, monkeypatch):
    from app.ai.semantic_index import index

    marked = []
    monkeypatch.setattr(index, "mark_dirty", lambda: marked.append(True))
    db = Database()
    refresh = jobs.SearchIndexRefresher()
    assert refresh(db.mongo) is False  # the first tick only notes the latest retrain
    jobs.enqueue_retrain(db, 5, now=T0)
    assert refresh(db.mongo) is False and not marked  # queued: nothing new yet
    jobs.run_retrain_cron(db, fake_train([]), clock=lambda: T0 + timedelta(minutes=30))
    assert refresh(db.mongo) is True and marked == [True]  # rebuilt on this process's next search
    assert refresh(db.mongo) is False and marked == [True]  # once per retrain
    # The default scheduler runs it on every tick, in every process, without a lease.
    assert set(jobs.Scheduler(db.mongo).local) == {"refresh_search_index"}
    assert jobs.Scheduler(db.mongo, jobs={"x": lambda: None}).local == {}


def test_a_failing_job_is_recorded_and_keeps_its_lease(mongo):
    clock = Clock()

    def broken():
        raise RuntimeError("boom")

    s = jobs.Scheduler(mongo, jobs={"sweep_broken": broken}, holder="process-a", clock=clock)
    assert s.tick() == {"sweep_broken": True}
    lease = mongo[jobs.LEASES].find_one({"_id": "sweep_broken"})
    assert lease["last_result"] == {"ok": False, "error": "RuntimeError"} and lease["holder"] == "process-a"


def test_the_default_scheduler_runs_the_real_sweep(mongo):
    mongo[jobs.LEASES].delete_many({"_id": {"$in": list(jobs.JOBS)}})
    s = jobs.Scheduler(mongo, holder="process-real")
    # The storage snapshot (P2.2), the retention and scheduled-deletion jobs (P3.2), the phone migration (P4.2) and
    # the WhatsApp outbox sender (P4.3), the daily stock check (P4.4), the monthly lead counts (P9.2), the
    # subscriptions job (P9.4), the daily billing reconciliation (P9.5), the weekly demand report (P9.7) and the daily
    # low-stock alert (P9.14).
    assert s.tick() == {"sweep_expired": True, "snapshot_storage": True, "retention": True, "process_deletions": True,
                        "phone_migration": True, "send_notifications": True, "stock_check": True,
                        "lead_counts": True, "subscriptions": True, "billing_reconciliation": True,
                        "demand_report": True, "low_stock_alerts": True}
    result = mongo[jobs.LEASES].find_one({"_id": "sweep_expired"})["last_result"]
    assert result["ok"] is True and {"expired_reservations", "cancelled_orders"} <= set(result)
    s.shutdown()


def test_migration_2_creates_the_collections_and_is_idempotent(ids):
    from app.core.database import client

    scratch = client[f"{Database().mongo.name}_m2"]
    try:
        m0002_job_leases.apply(scratch)
        m0002_job_leases.apply(scratch)
        for name in m0002_job_leases.COLLECTIONS:
            info = next(iter(scratch.list_collections(filter={"name": name})))
            assert info["options"]["validator"] == schema.VALIDATORS[name]
            built = set(scratch[name].index_information())
            assert {m.document["name"] for m in schema.INDEXES.get(name, ())} <= built
        assert latest_number() >= m0002_job_leases.NUMBER
    finally:
        client.drop_database(scratch.name)


# ---------------------------------------------------------------- lazy stale-order cancellation


def _stale_order(customer, ids) -> int:
    body = {"items": [{"product_id": ids["tape"], "quantity": 1}], "lat": CENTER[0], "lng": CENTER[1],
            "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    o = customer.post("/api/orders", json=body, headers=HEADERS).json()
    assert o["status"] == "PENDING"
    Database().orders.update_one({"_id": o["id"]}, {"$set": {
        "created_at": utcnow() - timedelta(minutes=PENDING_TIMEOUT_MINUTES + 1)}})
    return o["id"]


def _status(order_id: int) -> str:
    return Database().orders.get(order_id).status


def test_customer_order_list_and_detail_cancel_stale_orders(customer, ids):
    order_id = _stale_order(customer, ids)
    rows = customer.get("/api/orders", headers=HEADERS).json()
    assert next(o for o in rows if o["id"] == order_id)["status"] == "CANCELLED"

    order_id = _stale_order(customer, ids)
    assert customer.get(f"/api/orders/{order_id}", headers=HEADERS).json()["status"] == "CANCELLED"
    events = Database().status_events.find({"entity": "order", "entity_id": order_id, "to_status": "CANCELLED"})
    assert len(events) == 1 and events[0].actor_role == "system"


def test_owner_order_list_and_detail_cancel_stale_orders(customer, owner_a, ids):
    order_id = _stale_order(customer, ids)
    active = owner_a.get("/api/owner/orders", headers=HEADERS).json()
    assert order_id not in {o["id"] for o in active} and _status(order_id) == "CANCELLED"

    order_id = _stale_order(customer, ids)
    assert owner_a.get(f"/api/owner/orders/{order_id}", headers=HEADERS).json()["status"] == "CANCELLED"


# ---------------------------------------------------------------- retraining outside the API


def fake_train(calls: list):
    def train(db):
        calls.append(db.mongo.name)
        return {"embeddings": {}, "demand": {}}
    return train


def test_retrain_endpoint_enqueues_returns_at_once_and_refuses_duplicates_and_a_fourth(
        admin, requests_coll, monkeypatch):
    # The endpoint must not import the training code: any import of the pipeline would fail here.
    monkeypatch.setitem(sys.modules, "app.ai.pipeline", None)
    calls = []
    for n in range(jobs.RETRAIN_DAILY_LIMIT):
        res = admin.post("/api/ai/retrain", headers=HEADERS)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["queued"] is True and body["started"] is False and body["request"]["status"] == "queued"
        assert len(calls) == n and requests_coll.count_documents({"status": "queued"}) == 1  # not trained yet

        cards = admin.get("/api/ai/models", headers=HEADERS).json()
        # queued, not training: the page polls every 3 s only while a retrain runs.
        assert cards["training"] is False and cards["queued"] is True
        assert {m["status"] for m in cards["models"]} == {"queued"}

        dup = admin.post("/api/ai/retrain", headers=HEADERS)
        assert dup.status_code == 409 and dup.json()["error"]["code"] == "retrain_in_progress"

        done = jobs.run_retrain_cron(Database(), fake_train(calls), clock=lambda n=n: T0 + timedelta(minutes=n))
        assert done["status"] == "succeeded" and len(calls) == n + 1

    fourth = admin.post("/api/ai/retrain", headers=HEADERS)
    assert fourth.status_code == 429 and fourth.json()["error"]["code"] == "retrain_limit"
    assert requests_coll.count_documents({"kind": "manual"}) == jobs.RETRAIN_DAILY_LIMIT


def test_a_running_request_also_blocks_a_new_one(admin, requests_coll):
    assert admin.post("/api/ai/retrain", headers=HEADERS).status_code == 200
    requests_coll.update_one({"status": "queued"}, {"$set": {"status": "running", "started_at": utcnow()}})
    cards = admin.get("/api/ai/models", headers=HEADERS).json()
    assert cards["training"] is True and cards["queued"] is False
    assert {m["status"] for m in cards["models"]} == {"training"}
    assert admin.post("/api/ai/retrain", headers=HEADERS).status_code == 409


def test_the_daily_limit_is_a_rolling_24_hours(ids, requests_coll):
    db, calls = Database(), []
    for hour in range(jobs.RETRAIN_DAILY_LIMIT):
        jobs.enqueue_retrain(db, 1, now=T0 + timedelta(hours=hour))
        jobs.run_retrain_cron(db, fake_train(calls), clock=lambda h=hour: T0 + timedelta(hours=h, minutes=5))
    with pytest.raises(jobs.RetrainRefused):
        jobs.enqueue_retrain(db, 1, now=T0 + timedelta(hours=23, minutes=59))
    assert jobs.enqueue_retrain(db, 1, now=T0 + timedelta(hours=24, minutes=1))["status"] == "queued"


def test_only_admins_can_retrain(customer, requests_coll):
    assert customer.post("/api/ai/retrain", headers=HEADERS).status_code == 403
    assert requests_coll.count_documents({}) == 0


def test_the_cron_entry_point_processes_the_queue(ids, requests_coll, monkeypatch, capsys):
    from app.ai import pipeline

    calls = []
    monkeypatch.setattr(pipeline, "train_all", fake_train(calls))
    queued = jobs.enqueue_retrain(Database(), 5)
    assert pipeline.main(["--cron"]) == 0
    assert len(calls) == 1
    row = requests_coll.find_one({"_id": queued["id"]})
    assert row["status"] == "succeeded" and row["outcome"]["ok"] is True and row["outcome"]["models"] == [
        "demand", "embeddings"]
    assert row["started_at"] and row["finished_at"] and "active" not in row
    assert f'"request": {queued["id"]}' in capsys.readouterr().out


def test_the_cron_records_a_failed_training_and_exits_non_zero(ids, requests_coll, monkeypatch, capsys):
    from app.ai import pipeline

    def broken(_db):
        raise ValueError("not enough sales at mongodb://user:secret@host/x")

    monkeypatch.setattr(pipeline, "train_all", broken)
    queued = jobs.enqueue_retrain(Database(), 5)
    assert pipeline.main(["--cron"]) == 1
    row = requests_coll.find_one({"_id": queued["id"]})
    assert row["status"] == "failed" and "active" not in row
    assert row["outcome"]["error"].startswith("ValueError: not enough sales") and "secret" not in row["outcome"]["error"]
    assert f"recorded on retrain request {queued['id']}" in capsys.readouterr().err
    assert _card_status(None, jobs.retrain_state(Database())) == "failed"  # what the model cards show


def test_the_cron_runs_the_nightly_retrain_once_per_night(ids, requests_coll):
    db, calls = Database(), []
    train = fake_train(calls)
    noon = datetime(2026, 10, 20, 12, 0)
    assert jobs.run_retrain_cron(db, train, clock=lambda: noon) is None  # nothing queued, no nightly due
    nightly = datetime(2026, 10, 20, 21, 0)
    done = jobs.run_retrain_cron(db, train, clock=lambda: nightly)
    assert done["kind"] == "nightly" and done["status"] == "succeeded" and done["requested_by"] is None
    assert jobs.run_retrain_cron(db, train, clock=lambda: nightly + timedelta(minutes=30)) is None  # done tonight
    # A queued request at night is processed first; the nightly run is still caught up later that night.
    jobs.enqueue_retrain(db, 5, now=datetime(2026, 10, 21, 20, 50))
    assert jobs.run_retrain_cron(db, train, clock=lambda: datetime(2026, 10, 21, 21, 0))["kind"] == "manual"
    assert jobs.run_retrain_cron(db, train, clock=lambda: datetime(2026, 10, 21, 21, 30))["kind"] == "nightly"
    # Past the grace period a missed night is skipped rather than run at midday.
    assert jobs.run_retrain_cron(db, train, clock=lambda: datetime(2026, 10, 23, 1, 0)) is None
    assert len(calls) == 3
    assert requests_coll.count_documents({"kind": "nightly"}) == 2


def test_a_run_killed_outright_is_failed_by_the_next_cron_run(ids, requests_coll):
    """Render never overlaps runs of one cron job, so a request still running when a cron run starts was
    left by a run killed outright (out of memory, the run time limit): the next run fails it at once."""
    db, calls = Database(), []
    jobs.enqueue_retrain(db, 5, now=T0)
    requests_coll.update_one({"status": "queued"}, {"$set": {"status": "running", "started_at": T0}})
    with pytest.raises(Conflict):
        jobs.enqueue_retrain(db, 5, now=T0 + timedelta(minutes=10))
    next_run = T0 + timedelta(minutes=jobs.CRON_EVERY_MINUTES)
    assert jobs.run_retrain_cron(db, fake_train(calls), clock=lambda: next_run) is None
    row = requests_coll.find_one()
    assert row["status"] == "failed" and "abandoned" in row["outcome"]["error"] and "active" not in row
    assert jobs.enqueue_retrain(db, 5, now=next_run)["status"] == "queued"
    assert not calls


def test_a_nightly_slot_after_a_killed_run_still_trains(ids, requests_coll):
    db, calls = Database(), []
    jobs.enqueue_retrain(db, 5, now=datetime(2026, 10, 20, 20, 30))
    # The 20:30 run claimed it and was killed outright (out of memory): it recorded nothing.
    requests_coll.update_one({"status": "queued"}, {"$set": {"status": "running",
                                                              "started_at": datetime(2026, 10, 20, 20, 30)}})
    done = jobs.run_retrain_cron(db, fake_train(calls), clock=lambda: datetime(2026, 10, 20, 21, 0))
    assert done["kind"] == "nightly" and done["status"] == "succeeded" and len(calls) == 1
    assert requests_coll.find_one({"kind": "manual"})["status"] == "failed"


def test_a_stopped_training_is_recorded_as_failed(ids, requests_coll):
    db = Database()
    queued = jobs.enqueue_retrain(db, 5, now=T0)

    def stopped(_db):
        raise SystemExit("stopped by signal 15")

    with pytest.raises(SystemExit):
        jobs.run_retrain_cron(db, stopped, clock=lambda: T0 + timedelta(minutes=30))
    row = requests_coll.find_one({"_id": queued["id"]})
    assert row["status"] == "failed" and "active" not in row and row["outcome"]["error"].startswith("SystemExit")


def test_the_cron_entry_point_turns_sigterm_into_a_recorded_failure(ids, requests_coll, monkeypatch):
    from app.ai import pipeline

    class Unhandled(BaseException):
        pass

    def outer(_signum, _frame):  # what SIGTERM would do here if the cron entry point did not handle it
        raise Unhandled

    def killed(_db):
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(1000):  # the handler runs at the next bytecode boundary
            pass
        raise AssertionError("SIGTERM was not delivered")

    previous = signal.signal(signal.SIGTERM, outer)
    try:
        monkeypatch.setattr(pipeline, "train_all", killed)
        queued = jobs.enqueue_retrain(Database(), 5)
        with pytest.raises(SystemExit):
            pipeline.main(["--cron"])
        assert signal.getsignal(signal.SIGTERM) is outer  # restored afterwards
    finally:
        signal.signal(signal.SIGTERM, previous)
    row = requests_coll.find_one({"_id": queued["id"]})
    assert row["status"] == "failed" and "signal" in row["outcome"]["error"]


def test_the_cron_says_when_a_failure_was_not_recorded(ids, requests_coll, monkeypatch, capsys):
    from pymongo.errors import ServerSelectionTimeoutError

    from app.ai import pipeline

    def unreachable(_db, _now):
        raise ServerSelectionTimeoutError("no servers")

    monkeypatch.setattr(jobs, "_claim", unreachable)
    assert pipeline.main(["--cron"]) == 1
    err = capsys.readouterr().err
    assert "outside the training" in err and "recorded on retrain request" not in err


def test_the_database_keeps_at_most_one_request_active(ids, requests_coll):
    from pymongo.errors import DuplicateKeyError

    now = utcnow()
    doc = {"kind": "manual", "requested_by": 1, "requested_at": now, "status": "queued", "active": jobs.RETRAIN_ACTIVE}
    requests_coll.insert_one({"_id": 10_001, **doc})
    with pytest.raises(DuplicateKeyError):
        requests_coll.insert_one({"_id": 10_002, **doc})


def test_the_api_process_never_imports_the_training_libraries():
    """Importing the app and every route module loads none of the training libraries."""
    backend = Path(__file__).resolve().parents[1]
    code = ("import sys, app.main, app.jobs, app.api.routes.ai; "
            f"print([m for m in {TRAINING_LIBRARIES!r} + ('app.ai.pipeline',) if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, env={**os.environ}, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"


def test_the_retrain_cron_jobs_are_in_the_blueprint():
    yaml = pytest.importorskip("yaml")
    blueprint = yaml.safe_load((Path(__file__).resolve().parents[2] / "render.yaml").read_text(encoding="utf-8"))
    services = {s["name"]: s for s in blueprint["services"]}
    for job, api in (("nearshop-retrain-staging", "nearshop-api-staging"),
                     ("nearshop-retrain-production", "nearshop-api")):
        cron = services[job]
        assert cron["type"] == "cron" and cron["startCommand"].endswith("python -m app.ai.pipeline --cron")
        assert cron["schedule"] == f"*/{jobs.CRON_EVERY_MINUTES} * * * *"  # includes 21:00 UTC, the nightly slot
        assert cron["branch"] == services[api]["branch"]
        env = {v["key"]: v for v in cron["envVars"]}
        assert env["NEARSHOP_MONGODB_URI"]["fromService"]["name"] == api
        assert env["NEARSHOP_ENVIRONMENT"]["value"] == next(
            v["value"] for v in services[api]["envVars"] if v["key"] == "NEARSHOP_ENVIRONMENT")
