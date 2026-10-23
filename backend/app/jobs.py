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

The leased jobs are sweep_expired, every tick; snapshot_storage (P2.2), which records each
collection's data and index size in storage_snapshots once a day (app/services/analytics.py); retention
(P3.2), the nightly retention rules of plan section 8.4 (app/services/privacy.py, docs/legal/retention.md),
once a day at RETENTION_AT; process_deletions (P3.2), every tick, which runs account deletions that were
waiting for orders in flight and flags overdue ones for the admins; and phone_migration (P4.2), once per IST day,
which moves customers and owners to phone sign-in before the email sign-in sunset (NEARSHOP_EMAIL_LOGIN_SUNSET):
it warns those without a verified phone in the app 30, 7 and 1 days before the sunset, and switches off any still
unverified 30 days after it (an owner's shops are hidden with the account). The sunset in force is never earlier than
NEARSHOP_EMAIL_LOGIN_MIN_NOTICE_DAYS after the first day the job saw phone sign-in available, which it keeps on its
lease (phone_signin_since), so a provider that goes live late moves the sunset later (sunset_in_force).
send_notifications (P4.3), every tick, delivers the WhatsApp notification outbox (app/services/whatsapp.py): rows
written by notify() in the reservation and order transactions, sent here, outside them, with retries after 30 s,
2 min and 10 min. Each run also computes the hourly failure rate and, above 5%, reports the warning event the
WhatsApp alert fires on, at most once every 15 minutes (failure_alert_at on its lease). stock_check (P4.4, cadence
P9.3), every tick, considers each shop once a day from its opening time (IST) and sends its stock check, a stock-check
notification delivered on WhatsApp through the outbox, listing up to 5 listings counted more than 48 hours ago
(app/services/whatsapp.py send_stock_checks): daily as a free-form message when the owner's 24-hour service window
stays open at least 30 minutes more, otherwise the template once a week (NEARSHOP_STOCK_CHECK_WEEKLY_DAY); never on
the shop's closed day or when every listing is fresh. A shop's day is recorded once considered, so each tick after
that reads today's rows and the active shops (their id, owner, name, opening hours and closed day), and sends nothing
more to that shop that day.
lead_counts (P9.2), once a month, on the 1st at 02:00 IST or on the first tick after it, writes the previous IST
month's lead_counts row for every shop (app/services/leads.py rollup_month); the month it ran for is kept in the
lease's last_result (ran_for), and every later tick that month only repeats the last result. A re-run replaces the
month's rows.
subscriptions (P9.4), every tick, runs the plan rules of app/services/plans.py run_jobs: advances become active on
NEARSHOP_BILLING_STARTS_ON, the outcome guarantee credits paid quarters with too few customer requests, subscriptions
past their end date lapse to Free, owners are reminded 30, 7 and 1 days before the end, and admins are alerted about
an advance still not active 300 days after its payment. Each step is one indexed query when nothing is due, and each
change is conditional on the state it read, so a run repeated or overlapping a lease take-over changes nothing twice.
billing_reconciliation (P9.5), once per IST day (the first tick of the day), first compares the payment gateway's
settlement reports with the UPI Autopay payment rows (P9.6, app/services/gateway.py reconcile; while Autopay is off it
does nothing), putting every mismatch in the unmatched queue, then alerts the admins about every bank credit still
unmatched NEARSHOP_BILLING_UNMATCHED_ALERT_WORKING_DAYS (3) working days after it was credited
(app/services/billing.py alert_unmatched), once per credit; the day it ran for is kept in the lease's last_result
(ran_for), like lead_counts. A gateway that cannot be reached is logged and the alert still runs; the next day's run
reads the missed day's report again (NEARSHOP_AUTOPAY_RECONCILE_DAYS).
boost (P10.12, app/services/boost.py), every tick, closes the Boost periods that have ended (a period with no request
earns its free make-good period, once per purchase; any other ends), and once per IST day (the first tick of the day)
records each city's eligibility figures and pauses the packs of a city below a threshold, or resumes them with the
paused days added back; the day the daily part ran for is kept in the lease's last_result (ran_for).
demand_report (P9.7), once a week, on Monday at 03:00 IST or on the first tick after it, builds the "asked for near you"
report of every verified live shop and draws R5's weekly audit sample (app/services/demand_report.py build_week); the
ISO week it ran for is kept in the lease's last_result (ran_for), like lead_counts. Counts only, never a query.
statements (P9.9), every tick: once a month, on the 1st at 10:00 IST or on the first tick after it, it builds and
sends the previous IST month's statement to every active shop (app/services/statements.py build_month: a shadow bill
before billing starts and for Free shops, an ROI statement for paying shops, through notify and the WhatsApp outbox);
the month it ran for is kept in the lease's last_result (ran_for), like lead_counts, and a re-run skips every shop
that has its statement, so none is sent twice. Every tick it also copies the outbox rows' delivery statuses onto
the statements still queued or sent, built in the last 40 days (sync_delivery), and sends again by template a
statement the sender cancelled before sending it: one indexed query when there are none.
low_stock_alerts (P9.14), every tick, sends each shop on Growth or above with the alert on one message a day, from
its alert hour (IST, 18:00 by default) until 3 hours after it, listing up to 10 listings at or below their low-stock
level, lowest first (app/services/alerts.py send_low_stock_alerts): in the app and, through the outbox, on WhatsApp,
as a free-form message inside the owner's open window, otherwise as the template. The day it considered a shop is
recorded on the shop with a conditional write, so a shop gets at most one alert a day; a Free shop, a shop with
nothing low and a shop with the alert off get none. It runs beside the stock check and never replaces it.
kpis (P9.12, app/services/kpis.py run_job), every tick: once per IST day it records the gate city's median stock age
(kpi_days, for the consecutive fresh weeks), and once a month, on the 2nd at 06:00 IST or on the first tick after it,
it stores the previous month's snapshot of every gate figure (kpi_snapshots), which is never changed afterwards; the
day and the month it ran for are kept in the lease's last_result (reading_for, snapshot_for), so every other tick
does nothing. Nothing is recorded from demo data.

A failed run is retried on the next tick, except for a job in RETRY_AFTER_FAILURE: its failure is
recorded on the lease with retry_at, and until then the holder only renews the lease without running
the job. snapshot_storage is one: a failing measurement lists every collection and reads each one's
stats, and logs an error (an error tracker issue), so it is retried at most hourly, not every 30 s.

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
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.database import Database as MongoDatabase
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, utcnow
from app.core.errors import AppError, Conflict
from app.models import Notification
from app.services.orders import cancel_stale_pending
from app.services.reservations import expire_due

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

LEASES = "job_leases"
TICK_SECONDS = 30
LEASE_TTL = timedelta(seconds=60)
# Jobs too costly, or too loud, to repeat on every tick after a failure: retried after this long.
RETRY_AFTER_FAILURE: dict[str, timedelta] = {"snapshot_storage": timedelta(hours=1), "retention": timedelta(hours=1),
                                              "phone_migration": timedelta(hours=1),
                                              "stock_check": timedelta(minutes=15), "lead_counts": timedelta(hours=1),
                                              "billing_reconciliation": timedelta(hours=1),
                                              "boost": timedelta(minutes=15),
                                              "demand_report": timedelta(hours=1),
                                              "statements": timedelta(minutes=15),
                                              "low_stock_alerts": timedelta(minutes=15),
                                              "kpis": timedelta(hours=1)}
RETENTION_AT = time(21, 30)  # UTC: 03:00 IST, the nightly retention job (P3.2)
# The phone migration (P4.2): in-app warnings this many days before the email sign-in sunset, and accounts still
# without a verified phone switched off this many days after it.
SUNSET_WARN_DAYS = (30, 7, 1)
SUNSET_DEACTIVATE_AFTER_DAYS = 30
PHONE_MIGRATION_KIND = "phone_migration"  # the notifications' kind
PHONE_MIGRATION_JOB = "phone_migration"
PHONE_SIGNIN_SINCE = "phone_signin_since"  # on that job's lease: the first IST day it saw phone sign-in available
DEACTIVATED_NO_PHONE = "phone_not_verified"  # User.deactivated_reason of an account the job switched off
IST = ZoneInfo("Asia/Kolkata")
SEND_NOTIFICATIONS_JOB = "send_notifications"  # the WhatsApp outbox sender (P4.3)
FAILURE_ALERT_AT = "failure_alert_at"  # on that job's lease: when the failure-rate warning was last reported
STOCK_CHECK_JOB = "stock_check"  # the WhatsApp stock check (P4.4; daily in the window, else weekly: P9.3)
LEAD_COUNTS_JOB = "lead_counts"  # the monthly lead counts per shop (P9.2)
LEAD_COUNTS_AT = time(2, 0)  # IST, on the 1st of each month
SUBSCRIPTIONS_JOB = "subscriptions"  # plans and subscriptions (P9.4)
BILLING_JOB = "billing_reconciliation"  # the daily unmatched-credit alert (P9.5)
BOOST_JOB = "boost"  # Boost periods every tick, city eligibility once per IST day (P10.12)
DEMAND_REPORT_JOB = "demand_report"  # the weekly "asked for near you" report (P9.7)
STATEMENTS_JOB = "statements"  # the monthly statements (P9.9)
LOW_STOCK_ALERTS_JOB = "low_stock_alerts"  # the daily low-stock alert for Growth shops (P9.14)
KPIS_JOB = "kpis"  # the gate KPIs' daily reading and monthly snapshot (P9.12)

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
               clock: Clock = utcnow, ttl: timedelta = LEASE_TTL, retry_after: timedelta | None = None) -> bool:
    """Run fn if holder holds (or can take) job's lease, and record the run on the lease. True when it ran.

    With retry_after, a failed run records retry_at (the failure's time plus retry_after) in last_result,
    and fn is not run again before then: the lease is still renewed, and last_run_at and last_result
    keep the failure. The backoff is on the lease, so it holds across a lease take-over."""
    now = clock()
    if not acquire_lease(mongo, job, holder, now, ttl):
        return False
    if retry_after is not None:
        last = (mongo[LEASES].find_one({"_id": job}, {"last_result": 1}) or {}).get("last_result") or {}
        retry_at = last.get("retry_at")
        if last.get("ok") is False and isinstance(retry_at, datetime) and now < retry_at:
            return False
    try:
        result: dict[str, Any] = {"ok": True, **(fn() or {})}
    except Exception as exc:  # one bad run must not stop the scheduler
        log.exception("job %s failed", job)
        result = {"ok": False, "error": type(exc).__name__}
        if retry_after is not None:
            result["retry_at"] = clock() + retry_after
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
    on the first tick of the day; every later tick that day is one lookup by _id. A failed measurement
    is retried an hour later (RETRY_AFTER_FAILURE), not on every tick."""
    from app.services.analytics import record_storage_snapshot  # analytics loads the search code

    return record_storage_snapshot(Database().mongo, utcnow())


def retention_slot(now: datetime) -> datetime:
    """The most recent retention slot (RETENTION_AT, UTC) at or before now."""
    slot = datetime.combine(now.date(), RETENTION_AT)
    return slot if slot <= now else slot - timedelta(days=1)


def retention(clock: Clock = utcnow) -> dict[str, Any]:
    """Nightly retention (P3.2; plan section 8.4; app/services/privacy.py run_retention). Runs once per slot,
    at RETENTION_AT or on the first tick after it: the slot it ran for is kept in the lease's last_result
    (ran_for), and every later tick of that slot only repeats the last result. A failed run is retried an
    hour later (RETRY_AFTER_FAILURE)."""
    from app.services.privacy import run_retention

    now = clock()
    slot = retention_slot(now).isoformat()
    db = Database()
    last = (db.mongo[LEASES].find_one({"_id": "retention"}, {"last_result": 1}) or {}).get("last_result") or {}
    if last.get("ok") is True and last.get("ran_for") == slot:
        return {k: v for k, v in last.items() if k != "ok"}
    return {"ran_for": slot, **run_retention(db, now)}


def process_deletions() -> dict[str, int]:
    """Scheduled account deletions (P3.2): each runs once its orders in flight have closed, and is flagged
    for the admins when still waiting 7 days after the request. One indexed query per tick when none wait."""
    from app.services.privacy import process_scheduled_deletions

    return process_scheduled_deletions(Database(), utcnow())


def ist_day(at: datetime) -> date:
    """The IST calendar day of a naive UTC time."""
    return at.replace(tzinfo=ZoneInfo("UTC")).astimezone(IST).date()


def ist_midnight_utc(day: date) -> datetime:
    """The start of an IST day, as a naive UTC time."""
    return datetime.combine(day, time(0), tzinfo=IST).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def phone_signin_since(mongo: MongoDatabase) -> date | None:
    """The first IST day the phone_migration job saw phone sign-in available here, or None before it has."""
    doc = mongo[LEASES].find_one({"_id": PHONE_MIGRATION_JOB}, {PHONE_SIGNIN_SINCE: 1}) or {}
    value = doc.get(PHONE_SIGNIN_SINCE)
    return date.fromisoformat(value) if isinstance(value, str) else None


def sunset_in_force(configured: date, since: date | None, today: date, min_notice_days: int) -> date:
    """The email sign-in sunset that applies: the configured one, or min_notice_days after phone sign-in became
    available (since; today when not recorded yet) if that is later, so nobody loses email sign-in, or their
    account, with less notice than that however late the providers go live. The caller checks availability."""
    start = today if since is None else min(since, today)
    return max(configured, start + timedelta(days=min_notice_days))


def current_sunset(mongo: MongoDatabase, today: date) -> date:
    """sunset_in_force for settings, reading phone_signin_since only when it could matter: when the configured date
    is at least the minimum notice away, no start date (at the latest today) can move it."""
    from app.core.config import settings

    configured, notice = settings.email_login_sunset, settings.email_login_min_notice_days
    if configured >= today + timedelta(days=notice):
        return configured
    return sunset_in_force(configured, phone_signin_since(mongo), today, notice)


def _unverified() -> dict[str, Any]:
    """Active customer and owner accounts without a verified phone (deleted accounts are switched off)."""
    return {"role": {"$in": ["customer", "owner"]}, "is_active": True, "phone_verified_at": None}


def _warning(sunset: date, days_left: int) -> tuple[str, str]:
    when = f"{sunset.day} {sunset:%B %Y}"
    left = "tomorrow" if days_left == 1 else f"in {days_left} days"
    return (f"Verify your phone number: email sign-in ends {left}",
            f"From {when} you sign in to NearShop with a code sent to your phone. Verify your phone number now so you "
            f"can keep using your account. Accounts without a verified phone are switched off "
            f"{SUNSET_DEACTIVATE_AFTER_DAYS} days after that.")


def warn_before_sunset(db: Database, sunset: date, now: datetime) -> int:
    """Warn every unverified customer and owner once per stage: the stage is the smallest of SUNSET_WARN_DAYS that is
    at least the days left, so a day the job missed is caught up on the next run. A stage's warning goes to users
    without a phone_migration notification since the stage began. The notifications are written as notify() writes
    them (app/services/notifications.py), dated now. Returns the number warned."""
    days_left = (sunset - ist_day(now)).days
    stages = [d for d in SUNSET_WARN_DAYS if days_left <= d]
    if days_left < 1 or not stages:
        return 0
    stage_start = ist_midnight_utc(sunset - timedelta(days=min(stages)))
    title, body = _warning(sunset, days_left)
    warned = 0
    batch: list[int] = []

    def flush() -> int:
        done = {n["user_id"] for n in db.mongo["notifications"].find(
            {"user_id": {"$in": batch}, "kind": PHONE_MIGRATION_KIND, "created_at": {"$gte": stage_start}},
            {"user_id": 1})}
        todo = [uid for uid in batch if uid not in done]
        if todo:
            db.notifications.insert_many([Notification(user_id=uid, kind=PHONE_MIGRATION_KIND, title=title, body=body,
                                                        link="/verify-phone", created_at=now) for uid in todo])
        batch.clear()
        return len(todo)

    for doc in db.mongo["users"].find(_unverified(), {"_id": 1}).sort("_id", 1):
        batch.append(doc["_id"])
        if len(batch) >= 500:
            warned += flush()
    if batch:
        warned += flush()
    return warned


def deactivate_after_sunset(db: Database, sunset: date, now: datetime) -> dict[str, int]:
    """From SUNSET_DEACTIVATE_AFTER_DAYS after the sunset, switch off every customer and owner account still without
    a verified phone, ending its sessions and recording when and why (deactivated_at, deactivated_reason). An owner's
    shops are hidden with the account (is_active false, so customers no longer find or reserve at them; their
    listings are kept for the day support switches the account back on), in the same transaction. Returns the
    numbers of accounts switched off and shops hidden."""
    if (ist_day(now) - sunset).days < SUNSET_DEACTIVATE_AFTER_DAYS:
        return {"deactivated": 0, "shops_hidden": 0}
    from app.core.security import revoke_all_update

    def run() -> dict[str, int]:
        owners = [d["_id"] for d in db.users.find_raw({**_unverified(), "role": "owner"}, {"_id": 1})]
        count = db.users.update_many(_unverified(), revoke_all_update(
            {"is_active": False, "deactivated_at": now, "deactivated_reason": DEACTIVATED_NO_PHONE}))
        hidden = db.shops.update_many({"owner_id": {"$in": owners}, "is_active": True},
                                      {"$set": {"is_active": False, "updated_at": now}}) if owners else 0
        return {"deactivated": count, "shops_hidden": hidden}

    result = db.transaction(run)
    if result["deactivated"]:
        log.warning("phone migration: switched off %d accounts without a verified phone and hid %d of their shops",
                    result["deactivated"], result["shops_hidden"])
    return result


def phone_migration(clock: Clock = utcnow) -> dict[str, Any]:
    """The move to phone sign-in (P4.2), once per IST day: the day it ran for is kept in the lease's last_result
    (ran_for), and every later tick that day only repeats the last result. While phone sign-in is unavailable nobody
    can verify a phone, so it only counts. The first day it finds phone sign-in available is kept on the lease
    (phone_signin_since), and the sunset in force is current_sunset. last_result: warned, deactivated, shops_hidden,
    and remaining (active customer and owner accounts still without a verified phone), with the sunset in force and
    the configured one. A failed run is retried an hour later (RETRY_AFTER_FAILURE)."""
    from app.core.config import settings
    from app.services import otp

    now = clock()
    today = ist_day(now)
    db = Database()
    lease = db.mongo[LEASES].find_one({"_id": PHONE_MIGRATION_JOB}, {"last_result": 1}) or {}
    last = lease.get("last_result") or {}
    if last.get("ok") is True and last.get("ran_for") == today.isoformat():
        return {k: v for k, v in last.items() if k != "ok"}
    available = otp.available()
    if available:  # the lease is this process's while the job runs; $min keeps the first day
        db.mongo[LEASES].update_one({"_id": PHONE_MIGRATION_JOB}, {"$min": {PHONE_SIGNIN_SINCE: today.isoformat()}})
    sunset = current_sunset(db.mongo, today)
    warned = warn_before_sunset(db, sunset, now) if available else 0
    switched = deactivate_after_sunset(db, sunset, now) if available else {"deactivated": 0, "shops_hidden": 0}
    remaining = db.mongo["users"].count_documents(_unverified())
    since = phone_signin_since(db.mongo)
    return {"ran_for": today.isoformat(), "sunset": sunset.isoformat(),
            "configured_sunset": settings.email_login_sunset.isoformat(),
            "phone_signin_since": since.isoformat() if since else None, "phone_signin_available": available,
            "warned": warned, **switched, "remaining": remaining}


def send_notifications(clock: Clock = utcnow) -> dict[str, Any]:
    """The WhatsApp outbox sender (P4.3, app/services/whatsapp.py send_due), then the failure rate over the last hour.
    Above 5% it reports the warning event (whatsapp.report_failure_rate) when none was reported in the last
    FAILURE_ALERT_EVERY: the lease keeps failure_alert_at, so one incident is not an event every 30 s. last_result:
    the sender's counts, the hour's finished and failed rows, the rate, and whether the warning went out."""
    from app.services import whatsapp

    db = Database()
    result: dict[str, Any] = dict(whatsapp.send_due(db, clock=clock))
    now = clock()
    rate = whatsapp.failure_rate(db, now)
    result.update(rate, alerted=False)
    if whatsapp.above_threshold(rate):
        due = {"$or": [{FAILURE_ALERT_AT: None}, {FAILURE_ALERT_AT: {"$lte": now - whatsapp.FAILURE_ALERT_EVERY}}]}
        if db.mongo[LEASES].update_one({"_id": SEND_NOTIFICATIONS_JOB, **due},
                                       {"$set": {FAILURE_ALERT_AT: now}}).modified_count:
            whatsapp.report_failure_rate(rate)
            result["alerted"] = True
    return result


def stock_check(clock: Clock = utcnow) -> dict[str, Any]:
    """The stock check (P4.4, cadence P9.3, app/services/whatsapp.py send_stock_checks): nothing while WhatsApp is
    off here. last_result: the IST day and the shops considered this run: sent (in_window plus template), fresh
    (nothing older than 48 hours), unreachable, closed (the shop's closed day) and waiting (no open window and not
    the weekly template day). A failed run is retried 15 minutes later (RETRY_AFTER_FAILURE)."""
    from app.services import whatsapp

    return whatsapp.send_stock_checks(Database(), clock())


def lead_counts_month(now: datetime) -> tuple[int, int]:
    """The month the lead_counts job rolls up at now: the one before the most recent slot (the 1st at LEAD_COUNTS_AT
    IST) at or before now."""
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(IST)
    year, month = local.year, local.month
    if local < datetime.combine(date(year, month, 1), LEAD_COUNTS_AT, tzinfo=IST):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)  # this month's slot is still ahead
    return (year - 1, 12) if month == 1 else (year, month - 1)


def lead_counts(clock: Clock = utcnow) -> dict[str, Any]:
    """The monthly lead counts (P9.2, app/services/leads.py rollup_month), once per month: the month it ran for is kept
    in the lease's last_result (ran_for), and every later tick only repeats the last result. A failed run is retried
    an hour later (RETRY_AFTER_FAILURE)."""
    from app.services import leads

    now = clock()
    year, month = lead_counts_month(now)
    key = leads.month_key(year, month)
    db = Database()
    last = (db.mongo[LEASES].find_one({"_id": LEAD_COUNTS_JOB}, {"last_result": 1}) or {}).get("last_result") or {}
    if last.get("ok") is True and last.get("ran_for") == key:
        return {k: v for k, v in last.items() if k != "ok"}
    return {"ran_for": key, **leads.rollup_month(db, year, month, now)}


def subscriptions(clock: Clock = utcnow) -> dict[str, Any]:
    """Plans and subscriptions (P9.4, app/services/plans.py run_jobs), every tick. last_result: the numbers of advances
    activated, guarantee credits and quarters met, lapses, reminders and advance alerts of this run."""
    from app.services import plans

    return plans.run_jobs(Database(), clock())


def billing_reconciliation(clock: Clock = utcnow) -> dict[str, Any]:
    """The daily Autopay reconciliation (P9.6, app/services/gateway.py reconcile) and unmatched-credit alert (P9.5,
    app/services/billing.py alert_unmatched), once per IST day: the day it ran for is kept in the lease's last_result
    (ran_for), and every later tick that day only repeats the last result. A failed run is retried an hour later
    (RETRY_AFTER_FAILURE)."""
    from app.services import billing, gateway

    now = clock()
    today = ist_day(now).isoformat()
    db = Database()
    last = (db.mongo[LEASES].find_one({"_id": BILLING_JOB}, {"last_result": 1}) or {}).get("last_result") or {}
    if last.get("ok") is True and last.get("ran_for") == today:
        return {k: v for k, v in last.items() if k != "ok"}
    try:
        autopay: dict[str, Any] = gateway.reconcile(db, now)
    except AppError as exc:
        log.warning("Autopay reconciliation failed: %s", exc.message)
        autopay = {"error": exc.code}
    except Exception as exc:  # noqa: BLE001 - the unmatched-credit alert below must still run today
        log.exception("Autopay reconciliation failed unexpectedly: %s", type(exc).__name__)
        autopay = {"error": "unexpected"}
    return {"ran_for": today, **billing.alert_unmatched(db, now), "autopay": autopay}


def boost(clock: Clock = utcnow) -> dict[str, Any]:
    """Boost packs (P10.12, app/services/boost.py): the periods that have ended, every tick; the city eligibility check
    with pausing and resuming, once per IST day (ran_for in the lease's last_result). A failed run is retried 15 minutes
    later (RETRY_AFTER_FAILURE)."""
    from app.services import boost as boost_service

    now = clock()
    today = ist_day(now).isoformat()
    db = Database()
    result: dict[str, Any] = dict(boost_service.end_periods(db, now))
    last = (db.mongo[LEASES].find_one({"_id": BOOST_JOB}, {"last_result": 1}) or {}).get("last_result") or {}
    if last.get("ok") is True and last.get("ran_for") == today:
        return {**result, "ran_for": today, "daily": last.get("daily")}
    return {**result, "ran_for": today, "daily": boost_service.run_daily(db, now)}


def demand_report(clock: Clock = utcnow) -> dict[str, Any]:
    """The weekly demand report (P9.7, app/services/demand_report.py build_week), once per week from Monday 03:00 IST:
    the ISO week it ran for is kept in the lease's last_result (ran_for), and every later tick that week only repeats
    the last result. A failed run is retried an hour later (RETRY_AFTER_FAILURE)."""
    from app.services import demand_report as report

    now = clock()
    week = report.week_of(report.weekly_slot(now))
    db = Database()
    last = (db.mongo[LEASES].find_one({"_id": DEMAND_REPORT_JOB}, {"last_result": 1}) or {}).get("last_result") or {}
    if last.get("ok") is True and last.get("ran_for") == week:
        return {k: v for k, v in last.items() if k != "ok"}
    built = report.build_week(db, now)
    return {"ran_for": week, **{k: v for k, v in built.items() if k != "week"}}


def statements(clock: Clock = utcnow) -> dict[str, Any]:
    """The monthly statements (P9.9, app/services/statements.py), every tick: the previous month's statements once per
    month from the 1st at 10:00 IST (build_month), the month it ran for kept in the lease's last_result (ran_for), and
    the delivery statuses synced from the outbox (sync_delivery, synced). A failed run is retried 15 minutes later
    (RETRY_AFTER_FAILURE); the retry builds only the statements still missing."""
    from app.services import statements as service

    now = clock()
    year, month = service.statement_month(now)
    key = f"{year:04d}-{month:02d}"
    db = Database()
    synced = service.sync_delivery(db, now=now)
    last = (db.mongo[LEASES].find_one({"_id": STATEMENTS_JOB}, {"last_result": 1}) or {}).get("last_result") or {}
    if last.get("ok") is True and last.get("ran_for") == key:
        return {**{k: v for k, v in last.items() if k not in ("ok", "synced")}, "synced": synced}
    built = service.build_month(db, year, month, now)
    return {"ran_for": key, **{k: v for k, v in built.items() if k != "month"}, "synced": synced}


def low_stock_alerts(clock: Clock = utcnow) -> dict[str, Any]:
    """The daily low-stock alert (P9.14, app/services/alerts.py send_low_stock_alerts), every tick. last_result: the
    IST day and the shops considered this run: sent (with its route: in_window, template, or app when the owner gets
    no WhatsApp), nothing_low and not_entitled. A failed run is retried 15 minutes later (RETRY_AFTER_FAILURE), still
    inside the 3 hours after a shop's alert hour."""
    from app.services import alerts

    return alerts.send_low_stock_alerts(Database(), clock())


def kpis(clock: Clock = utcnow) -> dict[str, Any]:
    """The gate KPIs (P9.12, app/services/kpis.py run_job): the day's median stock age reading once per IST day, and
    the previous month's snapshot from the 2nd at 06:00 IST, each once (reading_for and snapshot_for in the lease's
    last_result). A failed run is retried an hour later (RETRY_AFTER_FAILURE)."""
    from app.services import kpis as service

    now = clock()
    db = Database()
    last = (db.mongo[LEASES].find_one({"_id": KPIS_JOB}, {"last_result": 1}) or {}).get("last_result") or {}
    if last.get("ok") is not True:
        last = {}
    return service.run_job(db, now, last)


JOBS: dict[str, Callable[[], Mapping[str, Any] | None]] = {
    "sweep_expired": sweep_expired, "snapshot_storage": snapshot_storage, "retention": retention,
    "process_deletions": process_deletions, PHONE_MIGRATION_JOB: phone_migration,
    SEND_NOTIFICATIONS_JOB: send_notifications, STOCK_CHECK_JOB: stock_check, LEAD_COUNTS_JOB: lead_counts,
    SUBSCRIPTIONS_JOB: subscriptions, BILLING_JOB: billing_reconciliation, BOOST_JOB: boost,
    DEMAND_REPORT_JOB: demand_report, STATEMENTS_JOB: statements, LOW_STOCK_ALERTS_JOB: low_stock_alerts,
    KPIS_JOB: kpis}


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
    the jobs are JOBS and the local job is a SearchIndexRefresher. `retry` (default RETRY_AFTER_FAILURE)
    names the jobs whose failures are retried only after a delay (run_leased)."""

    def __init__(self, mongo: MongoDatabase, *, jobs: Mapping[str, Callable[[], Mapping[str, Any] | None]] | None = None,
                 local: Mapping[str, Callable[[MongoDatabase], Any]] | None = None,
                 holder: str | None = None, clock: Clock = utcnow, ttl: timedelta = LEASE_TTL,
                 retry: Mapping[str, timedelta] | None = None):
        self.mongo = mongo
        self.jobs = dict(JOBS if jobs is None else jobs)
        self.retry = dict(RETRY_AFTER_FAILURE if retry is None else retry)
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
                ran[job] = run_leased(self.mongo, job, self.holder, fn, self.clock, self.ttl, self.retry.get(job))
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
