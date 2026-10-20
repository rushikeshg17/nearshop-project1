"""Background jobs: the expiry sweep and the storage snapshot under a lease, and the retraining queue.

The expiry sweep
----------------
Every API process starts a Scheduler (main.py lifespan, unless NEARSHOP_ENABLE_SCHEDULER is false).
Every TICK_SECONDS (30 s) it tries to take or renew each job's lease, and runs the job only while
it holds the lease, so at any instance count each job runs once per tick.

  * job_leases holds one document per job: _id (the job name), holder (host:pid:random of the
    process), acquired_at, expires_at, last_run_at and last_result.
  * A lease is taken atomically when it is free or expired, and lasts LEASE_TTL (60 s). The holder
    renews it on every tick, so it never lapses while the holder is alive; when the holder stops (a
    deploy, a crash), another process takes over within 60 s. A clean shutdown gives the lease up at
    once: shutdown waits for a tick in progress before giving the leases up, so that tick cannot renew
    a lease after its release.
  * last_run_at is the heartbeat: it is never older than 90 s while some process is running
    (scorecard check 4.2). /api/health/ready reports it, and an uptime monitor alerts on it
    (docs/runbooks/monitoring.md).

The leased jobs are sweep_expired, every tick, and snapshot_storage (P2.2), which records each
collection's data and index size in storage_snapshots once a day (app/services/analytics.py).

Reservation and order lists also expire reservations and cancel stale orders lazily when read, so a
page is right even between ticks.

Besides the leased jobs, every process also runs its local jobs on each tick, without a lease. The one
local job, SearchIndexRefresher, marks the process's own semantic search index stale when a retrain
has succeeded since the last tick: the retrain cron job rebuilds the index only in its own memory, so
each API process rebuilds its copy on its next search (cheaply: the embeddings are cached in
text_embeddings). Sharing one index across processes is P8.1.

Retraining
----------
Training never runs inside the API: the training libraries (scikit-learn, pandas, gensim, mlxtend)
would stay in the API's memory. POST /api/ai/retrain only enqueues a request (enqueue_retrain). A
Render cron job (render.yaml, nearshop-retrain-*) runs python -m app.ai.pipeline --cron every 30
minutes in its own process: it processes a queued request, or the nightly run in the slot at
NIGHTLY_AT (21:00 UTC, 02:30 IST), then exits (run_retrain_cron).

Render runs at most one run of a cron job at a time, and each database has exactly one retrain cron
job, so a request still "running" when a cron run starts belongs to a run that was killed outright (out
of memory, Render's run time limit, a deploy): the new run marks it failed before anything else, so the
queue and the nightly slot are blocked for at most one cron interval. A run stopped by SIGTERM records
its own failure (pipeline.py turns SIGTERM into SystemExit).

retrain_requests holds one document per request: _id (from counters), kind (manual or nightly),
requested_by (the admin's user id; null for the nightly run), requested_at, status (queued, running,
succeeded or failed), started_at, finished_at and outcome. While a request is queued or running it
also carries active, under a unique index, so the database itself keeps at most one request queued
or running; nightly runs carry nightly_for (the slot's date), also unique, so a night trains once.

This module never imports the training code: run_retrain_cron is given the train function.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, time, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.database import Database as MongoDatabase
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, utcnow
from app.core.errors import AppError, Conflict
from app.services.orders import cancel_stale_pending
from app.services.reservations import expire_due

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

LEASES = "job_leases"
TICK_SECONDS = 30
LEASE_TTL = timedelta(seconds=60)

RETRAIN_REQUESTS = "retrain_requests"
RETRAIN_DAILY_LIMIT = 3  # manual requests in any 24 hours
RETRAIN_WINDOW = timedelta(hours=24)
RETRAIN_ACTIVE = "retrain"  # the value of active while a request is queued or running
NIGHTLY_AT = time(21, 0)  # UTC: 02:30 IST
NIGHTLY_GRACE = timedelta(hours=3)  # a nightly slot missed (a busy queue, a failed cron run) is caught up this late
CRON_EVERY_MINUTES = 30  # render.yaml schedule of the retrain cron jobs


def default_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- leases


def acquire_lease(mongo: MongoDatabase, job: str, holder: str, now: datetime, ttl: timedelta = LEASE_TTL) -> bool:
    """Take job's lease for holder, or renew it if holder has it. True when holder holds it until now + ttl.

    One atomic write: the update matches only a lease that is holder's or has expired, and when none
    matches the upsert inserts the job's _id, which fails while another holder's live lease exists."""
    renewing = {"$eq": ["$holder", {"$literal": holder}]}
    try:
        before = mongo[LEASES].find_one_and_update(
            {"_id": job, "$or": [{"holder": holder}, {"expires_at": {"$lte": now}}]},
            [{"$set": {"acquired_at": {"$cond": [renewing, "$acquired_at", now]},
                       "holder": {"$literal": holder}, "expires_at": now + ttl,
                       "last_run_at": {"$ifNull": ["$last_run_at", None]},
                       "last_result": {"$ifNull": ["$last_result", None]}}}],
            upsert=True, return_document=ReturnDocument.BEFORE, projection={"holder": 1})
    except DuplicateKeyError:
        return False  # another holder's lease is live
    if before is not None and before.get("holder") != holder:
        log.info("job %s: lease taken over from %s", job, before.get("holder"))
    return True


def release_lease(mongo: MongoDatabase, job: str, holder: str, now: datetime) -> None:
    """Give up holder's lease at once (a clean shutdown), so another process need not wait for it to expire."""
    mongo[LEASES].update_one({"_id": job, "holder": holder}, {"$set": {"expires_at": now}})


def run_leased(mongo: MongoDatabase, job: str, holder: str, fn: Callable[[], Mapping[str, Any] | None],
               clock: Clock = utcnow, ttl: timedelta = LEASE_TTL) -> bool:
    """Run fn if holder holds (or can take) job's lease, and record the run on the lease. True when it ran."""
    if not acquire_lease(mongo, job, holder, clock(), ttl):
        return False
    try:
        result: dict[str, Any] = {"ok": True, **(fn() or {})}
    except Exception as exc:  # one bad run must not stop the scheduler
        log.exception("job %s failed", job)
        result = {"ok": False, "error": type(exc).__name__}
    mongo[LEASES].update_one({"_id": job, "holder": holder},
                             {"$set": {"last_run_at": clock(), "last_result": result}})
    return True


# ---------------------------------------------------------------- the jobs


def sweep_expired() -> dict[str, int]:
    db = Database()
    n = expire_due(db)
    m = cancel_stale_pending(db)
    if n or m:
        log.info("expired %d reservations, auto-cancelled %d stale orders", n, m)
    return {"expired_reservations": n, "cancelled_orders": m}


def snapshot_storage() -> dict[str, Any]:
    """Daily storage snapshot (P2.2): records today's (IST) collection sizes in storage_snapshots once,
    on the first tick of the day; every later tick that day is one lookup by _id."""
    from app.services.analytics import record_storage_snapshot  # analytics loads the search code

    return record_storage_snapshot(Database().mongo, utcnow())


JOBS: dict[str, Callable[[], Mapping[str, Any] | None]] = {
    "sweep_expired": sweep_expired, "snapshot_storage": snapshot_storage}


class SearchIndexRefresher:
    """Local job: marks this process's semantic search index stale when a retrain succeeded since the
    last tick, so the next search rebuilds it from the listings (the retrain cron job rebuilt only its
    own copy). The first tick only notes the latest retrain: the index was just built at startup."""

    def __init__(self) -> None:
        self.seen: int | None = None

    def __call__(self, mongo: MongoDatabase) -> bool:
        latest = mongo[RETRAIN_REQUESTS].find_one({"status": "succeeded"}, {"_id": 1}, sort=[("_id", -1)])
        latest_id = latest["_id"] if latest is not None else 0
        refresh = self.seen is not None and latest_id != self.seen
        if refresh:
            from app.ai.semantic_index import index

            index.mark_dirty()
            log.info("retrain %s succeeded: this process's search index rebuilds on the next search", latest_id)
        self.seen = latest_id
        return refresh


class Scheduler:
    """Runs each job of `jobs` on every tick, only while this process holds the job's lease, and each
    of `local` (given the database) in this process on every tick, without a lease. With no jobs given,
    the jobs are JOBS and the local job is a SearchIndexRefresher."""

    def __init__(self, mongo: MongoDatabase, *, jobs: Mapping[str, Callable[[], Mapping[str, Any] | None]] | None = None,
                 local: Mapping[str, Callable[[MongoDatabase], Any]] | None = None,
                 holder: str | None = None, clock: Clock = utcnow, ttl: timedelta = LEASE_TTL):
        self.mongo = mongo
        self.jobs = dict(JOBS if jobs is None else jobs)
        if local is None:
            local = {"refresh_search_index": SearchIndexRefresher()} if jobs is None else {}
        self.local = dict(local)
        self.holder = holder or default_holder()
        self.clock = clock
        self.ttl = ttl
        self._background = None

    def tick(self) -> dict[str, bool]:
        """One round: for each job, renew or take its lease and run it. Returns which jobs ran here."""
        ran = {}
        for job, fn in self.jobs.items():
            try:
                ran[job] = run_leased(self.mongo, job, self.holder, fn, self.clock, self.ttl)
            except Exception as exc:  # the database unreachable: try again next tick
                log.warning("job %s: could not take its lease (%s)", job, type(exc).__name__)
                ran[job] = False
        for job, fn in self.local.items():
            try:
                fn(self.mongo)
            except Exception as exc:
                log.warning("local job %s failed (%s)", job, type(exc).__name__)
        return ran

    def start(self, seconds: int = TICK_SECONDS) -> Scheduler:
        from apscheduler.schedulers.background import BackgroundScheduler

        self._background = BackgroundScheduler(timezone="UTC")
        self._background.add_job(self.tick, "interval", seconds=seconds, id="lease_tick", max_instances=1,
                                 coalesce=True, next_run_time=datetime.now().astimezone())
        self._background.start()
        return self

    def shutdown(self) -> None:
        if self._background is not None:
            self._background.shutdown(wait=True)  # a tick in progress finishes first: it cannot renew after the release
            self._background = None
        for job in self.jobs:
            try:
                release_lease(self.mongo, job, self.holder, self.clock())
            except Exception as exc:
                log.warning("job %s: could not release its lease (%s)", job, type(exc).__name__)


def start_scheduler() -> Scheduler:
    return Scheduler(Database().mongo).start()


# ---------------------------------------------------------------- the retraining queue

ACTIVE_STATUSES = ("queued", "running")


class RetrainRefused(AppError):
    status_code = 429
    code = "retrain_limit"


class RetrainFailed(Exception):
    """The training of a claimed request failed; the failure is recorded on that request. __cause__ is
    the training's own error."""

    def __init__(self, request_id: int):
        super().__init__(f"retrain request {request_id} failed")
        self.request_id = request_id


def _public(doc: Mapping[str, Any] | None) -> dict | None:
    if doc is None:
        return None
    return {"id": doc["_id"], "kind": doc.get("kind"), "status": doc.get("status"),
            "requested_by": doc.get("requested_by"), "requested_at": doc.get("requested_at"),
            "started_at": doc.get("started_at"), "finished_at": doc.get("finished_at"),
            "outcome": doc.get("outcome")}


def enqueue_retrain(db: Database, requested_by: int, now: datetime | None = None) -> dict:
    """Queue a manual retrain and return it. Raises Conflict while a request is queued or running, and
    RetrainRefused once RETRAIN_DAILY_LIMIT manual requests were made in the last 24 hours."""
    now = now or utcnow()
    coll = db.mongo[RETRAIN_REQUESTS]
    active = coll.find_one({"status": {"$in": list(ACTIVE_STATUSES)}}, sort=[("_id", -1)])
    if active is not None:
        raise Conflict(f"A retrain is already {active['status']}; wait for it to finish.", code="retrain_in_progress")
    recent = list(coll.find({"kind": "manual", "requested_at": {"$gt": now - RETRAIN_WINDOW}},
                            {"requested_at": 1}, sort=[("requested_at", 1)]))
    if len(recent) >= RETRAIN_DAILY_LIMIT:
        next_at = recent[-RETRAIN_DAILY_LIMIT]["requested_at"] + RETRAIN_WINDOW
        raise RetrainRefused(f"At most {RETRAIN_DAILY_LIMIT} retrains a day. The next one can be requested "
                             f"after {next_at:%d %b %H:%M} UTC; the nightly run also retrains every model.")
    doc = {"_id": db.next_id(RETRAIN_REQUESTS), "kind": "manual", "requested_by": requested_by,
           "requested_at": now, "status": "queued", "started_at": None, "finished_at": None, "outcome": None,
           "active": RETRAIN_ACTIVE}
    try:
        coll.insert_one(doc)
    except DuplicateKeyError:  # another request was queued at the same moment
        raise Conflict("A retrain is already queued; wait for it to finish.", code="retrain_in_progress") from None
    return _public(doc)


def retrain_state(db: Database) -> dict:
    """What the AI model cards show: the latest request, and whether one is queued or running."""
    coll = db.mongo[RETRAIN_REQUESTS]
    latest = coll.find_one(sort=[("_id", -1)])
    active = coll.find_one({"status": {"$in": list(ACTIVE_STATUSES)}}, sort=[("_id", -1)])
    finished = coll.find_one({"status": {"$in": ["succeeded", "failed"]}}, sort=[("_id", -1)])
    return {"latest": _public(latest), "active": _public(active), "last_finished": _public(finished)}


def nightly_slot(now: datetime) -> datetime:
    """The most recent nightly slot at or before now."""
    slot = datetime.combine(now.date(), NIGHTLY_AT)
    return slot if slot <= now else slot - timedelta(days=1)


def _fail_abandoned(coll, now: datetime) -> int:
    """Mark every request still running as failed. Called only when a cron run starts: Render never
    overlaps two runs of one cron job and each database has one retrain cron job, so any such request
    was left by a run killed outright (see the module docstring)."""
    res = coll.update_many(
        {"status": "running"},
        {"$set": {"status": "failed", "finished_at": now,
                  "outcome": {"ok": False, "error": "abandoned: the training process stopped before finishing"}},
         "$unset": {"active": ""}})
    if res.modified_count:
        log.warning("marked %d abandoned retrain runs as failed", res.modified_count)
    return res.modified_count


def _claim(db: Database, now: datetime) -> dict | None:
    """Take the oldest queued request, or start the nightly run when its slot is due. None: nothing to do."""
    coll = db.mongo[RETRAIN_REQUESTS]
    claimed = coll.find_one_and_update({"status": "queued"}, {"$set": {"status": "running", "started_at": now}},
                                       sort=[("_id", 1)], return_document=ReturnDocument.AFTER)
    if claimed is not None:
        return claimed
    slot = nightly_slot(now)
    if now - slot >= NIGHTLY_GRACE:
        return None
    slot_key = slot.date().isoformat()
    if coll.find_one({"nightly_for": slot_key}, {"_id": 1}) is not None:
        return None
    doc = {"_id": db.next_id(RETRAIN_REQUESTS), "kind": "nightly", "requested_by": None, "requested_at": now,
           "status": "running", "started_at": now, "finished_at": None, "outcome": None,
           "nightly_for": slot_key, "active": RETRAIN_ACTIVE}
    try:
        coll.insert_one(doc)
    except DuplicateKeyError:  # a request is running, or this night's run exists: try again next time
        return None
    return doc


def _error_text(exc: BaseException) -> str:
    text = re.sub(r"//[^@/\s]*@", "//***@", str(exc))[:300]  # never store credentials from a URI
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def run_retrain_cron(db: Database, train: Callable[[Database], Mapping[str, Any]], clock: Clock = utcnow) -> dict | None:
    """One cron run: fail runs abandoned by a killed process, then train for one queued request or the
    nightly slot, and record the outcome. Returns the finished request, or None when there was nothing
    to do. A training error is recorded and raised as RetrainFailed (from the error), so the cron run
    shows as failed; SystemExit and KeyboardInterrupt are recorded too, and re-raised as they are. Any
    other exception (the database unreachable before a claim, or while recording) recorded nothing."""
    coll = db.mongo[RETRAIN_REQUESTS]
    _fail_abandoned(coll, clock())
    request = _claim(db, clock())
    if request is None:
        log.info("retrain: nothing queued and no nightly run due")
        return None
    log.info("retrain: request %s (%s) started", request["_id"], request["kind"])
    started = clock()
    try:
        result = train(db)
    except BaseException as exc:  # SystemExit too: pipeline.py turns SIGTERM into one
        coll.update_one({"_id": request["_id"]}, {"$set": {
            "status": "failed", "finished_at": clock(), "outcome": {"ok": False, "error": _error_text(exc)}},
            "$unset": {"active": ""}})
        log.exception("retrain: request %s failed", request["_id"])
        if isinstance(exc, Exception):
            raise RetrainFailed(request["_id"]) from exc
        raise
    finished = clock()
    outcome = {"ok": True, "models": sorted(result), "seconds": round((finished - started).total_seconds(), 1)}
    coll.update_one({"_id": request["_id"]}, {"$set": {"status": "succeeded", "finished_at": finished,
                                                       "outcome": outcome}, "$unset": {"active": ""}})
    log.info("retrain: request %s succeeded in %.1f s", request["_id"], outcome["seconds"])
    return _public(coll.find_one({"_id": request["_id"]}))
