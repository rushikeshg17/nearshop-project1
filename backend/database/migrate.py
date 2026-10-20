"""The migration runner: pending numbered migrations in order, then ensure_schema, under a lock.

    uv run python -m database.migrate            # apply pending migrations, then ensure_schema
    uv run python -m database.migrate --status   # read-only: applied, expected and pending numbers
    uv run python -m database.migrate --release-lock  # clear a lock left by a killed runner (runbook)

Render runs it as each API service's preDeployCommand (render.yaml): it finishes before the new code
starts, and a non-zero exit fails the deploy while the previous version keeps serving. How to write
a migration, the helpers and the expand-then-contract rule: database/migrations/__init__.py.

  * Lock: one document in schema_migrations_lock, so two runners never overlap. A second runner is
    refused (exit 3) while the lock is held. The holder refreshes it before each migration; a lock
    left by a runner that died is taken over once it has expired (LOCK_TTL). A runner that is killed
    (a pre-deploy timeout or a cancelled deploy) never reaches its release, so its lock blocks every
    deploy until it expires; --release-lock clears it at once, only once no runner is still working
    (docs/runbooks/release-and-rollback.md, "Schema migrations").
  * Records: schema_migrations holds one document per applied migration: _id and number, name,
    applied_at and duration (seconds).
  * A database ahead of the code (it records migrations this code does not know, as after a
    rollback) is left alone: no migration runs and ensure_schema does not rewrite the newer
    validators.

In development and test the command runs only against a local MongoDB (the hosts of the reset guard
in database/schema.py), so a backend/.env pointing at a hosted cluster cannot be migrated from a
laptop by mistake; staging and production migrate through the deploy.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pymongo.database import Database as MongoDatabase
from pymongo.errors import DuplicateKeyError

from database.migrations import Migration, check_sequence, discover
from database.schema import ensure_schema

log = logging.getLogger("nearshop.migrate")

MIGRATIONS_COLLECTION = "schema_migrations"
LOCK_COLLECTION = "schema_migrations_lock"
LOCK_ID = "migrate"
LOCK_TTL = timedelta(hours=1)  # longer than any single migration may take


class MigrationLocked(RuntimeError):
    """Another runner holds the migration lock. Nothing was applied."""


def _now() -> datetime:
    """Naive UTC, truncated to milliseconds, like app.core.database.utcnow."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- the lock


def acquire_lock(mongo: MongoDatabase, owner: str, ttl: timedelta = LOCK_TTL) -> None:
    """Take the lock for owner, or raise MigrationLocked while another runner holds an unexpired lock."""
    locks = mongo[LOCK_COLLECTION]
    now = _now()
    doc = {"_id": LOCK_ID, "owner": owner, "acquired_at": now, "expires_at": now + ttl}
    try:
        locks.insert_one(doc)
        return
    except DuplicateKeyError:
        pass
    stale = locks.find_one_and_replace({"_id": LOCK_ID, "expires_at": {"$lt": now}}, doc)
    if stale is None:
        held = locks.find_one({"_id": LOCK_ID}) or {}
        raise MigrationLocked(
            f"Another migration runner holds the lock ({held.get('owner', 'unknown')}, since "
            f"{held.get('acquired_at')} UTC, expires {held.get('expires_at')} UTC). Nothing was applied.")
    log.warning("took over an expired migration lock left by %s", stale.get("owner"))


def refresh_lock(mongo: MongoDatabase, owner: str, ttl: timedelta = LOCK_TTL) -> None:
    """Extend owner's lock; raise MigrationLocked if it is no longer owner's."""
    res = mongo[LOCK_COLLECTION].update_one({"_id": LOCK_ID, "owner": owner},
                                            {"$set": {"expires_at": _now() + ttl}})
    if res.matched_count != 1:
        raise MigrationLocked("The migration lock was lost (it expired and another runner took it). Stopping.")


def release_lock(mongo: MongoDatabase, owner: str) -> None:
    mongo[LOCK_COLLECTION].delete_one({"_id": LOCK_ID, "owner": owner})


def force_release_lock(mongo: MongoDatabase) -> dict | None:
    """Remove the lock whoever holds it, and return the removed lock document (None when unlocked).

    For an operator clearing the lock of a runner that was killed. Never while a runner may still be
    working: two runners would then overlap."""
    return mongo[LOCK_COLLECTION].find_one_and_delete({"_id": LOCK_ID})


# ---------------------------------------------------------------- versions


def applied_numbers(mongo: MongoDatabase) -> set[int]:
    return {d["number"] for d in mongo[MIGRATIONS_COLLECTION].find({}, {"number": 1})}


@dataclass(frozen=True)
class SchemaVersion:
    applied: int  # highest applied migration number, 0 for none
    expected: int  # highest migration number this code has
    pending: list[int] = field(default_factory=list)

    @property
    def behind(self) -> bool:
        return bool(self.pending)

    @property
    def ahead(self) -> bool:
        return self.applied > self.expected


def check_version(mongo: MongoDatabase, migrations: Sequence[Migration] | None = None) -> SchemaVersion:
    """Compare the applied migrations with the ones this code has. Read-only."""
    numbers = [m.number for m in (check_sequence(migrations) if migrations is not None else discover())]
    done = applied_numbers(mongo)
    return SchemaVersion(applied=max(done, default=0), expected=max(numbers, default=0),
                         pending=[n for n in numbers if n not in done])


# ---------------------------------------------------------------- the runner


def run(mongo: MongoDatabase, *, migrations: Sequence[Migration] | None = None, owner: str | None = None,
        lock_ttl: timedelta = LOCK_TTL) -> list[int]:
    """Under the lock, apply pending migrations in order, record each, then run ensure_schema.

    Returns the numbers applied (empty when the database was up to date). Raises MigrationLocked when
    another runner holds the lock, and lets a failing migration's error propagate, leaving it
    unrecorded so the next run tries it again."""
    ordered = check_sequence(migrations) if migrations is not None else discover()
    owner = owner or default_owner()
    acquire_lock(mongo, owner, lock_ttl)
    try:
        done = applied_numbers(mongo)
        known = {m.number for m in ordered}
        if unknown := sorted(done - known):
            log.warning("The database has migrations %s that this code does not know (a rollback?). "
                        "Nothing applied, and validators and indexes are left as they are.", unknown)
            return []
        applied = []
        for migration in (m for m in ordered if m.number not in done):
            refresh_lock(mongo, owner, lock_ttl)
            log.info("applying migration %d %s", migration.number, migration.name)
            started = time.perf_counter()
            migration.apply(mongo)
            duration = round(time.perf_counter() - started, 3)
            mongo[MIGRATIONS_COLLECTION].replace_one(
                {"_id": migration.number},
                {"number": migration.number, "name": migration.name, "applied_at": _now(), "duration": duration},
                upsert=True)
            log.info("applied migration %d %s in %.3f s", migration.number, migration.name, duration)
            applied.append(migration.number)
        refresh_lock(mongo, owner, lock_ttl)
        ensure_schema(mongo)
        return applied
    finally:
        release_lock(mongo, owner)


# ---------------------------------------------------------------- command line


def remote_problem(environment: str, hosts: Sequence[str]) -> str | None:
    """Why this command must not migrate these hosts from this environment, or None."""
    from database.schema import LOCAL_HOSTS, LOCAL_ONLY_ENVIRONMENTS

    if environment in LOCAL_ONLY_ENVIRONMENTS:
        if remote := [h for h in hosts if h not in LOCAL_HOSTS]:
            return (f"NEARSHOP_ENVIRONMENT is {environment!r} but MongoDB host {remote[0]!r} is not local. "
                    f"Staging and production migrate in their deploy's pre-deploy step; to migrate another "
                    f"hosted database, set NEARSHOP_ENVIRONMENT to its environment.")
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m database.migrate", description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="only report applied and pending migrations")
    action.add_argument("--release-lock", action="store_true",
                        help="remove the migration lock left by a runner that was killed; apply nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from app.core.config import settings
    from app.core.database import client
    from database.schema import uri_hosts

    mongo = client[settings.mongodb_db]
    if args.status:
        version = check_version(mongo)
        print(f"database {mongo.name!r}: applied {version.applied}, code expects {version.expected}, "
              f"pending {version.pending or 'none'}")
        return 0
    if problem := remote_problem(settings.environment, uri_hosts(settings.mongodb_uri)):
        print(f"{'Lock release' if args.release_lock else 'Migration'} refused; nothing was written. {problem}", file=sys.stderr)
        return 2
    if args.release_lock:
        removed = force_release_lock(mongo)
        if removed is None:
            print(f"database {mongo.name!r}: no migration lock was held")
        else:
            print(f"database {mongo.name!r}: removed the migration lock of {removed.get('owner', 'unknown')} "
                  f"(since {removed.get('acquired_at')} UTC, expiring {removed.get('expires_at')} UTC)")
        return 0
    try:
        applied = run(mongo)
    except MigrationLocked as exc:
        print(exc, file=sys.stderr)
        return 3
    version = check_version(mongo)
    print(f"database {mongo.name!r} at migration {version.applied} (code expects {version.expected}); "
          f"applied {applied or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
