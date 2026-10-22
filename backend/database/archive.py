"""Sales history archiving: months of sales_history older than the models use, off-site, then out of Atlas.

    uv run python -m database.archive                      # archive every eligible month, then delete its rows
    uv run python -m database.archive --plan               # read-only: the eligible months and their rows
    uv run python -m database.archive --list               # read-only: the archived months in the store
    uv run python -m database.archive --restore 2025-03 --target-db nearshop_sales_2025_03

Render runs it on the 1st of every month at 23:00 UTC (04:30 IST on the 2nd), after that night's backup, as a
cron job for production and for staging (render.yaml). Bringing a month back: docs/runbooks/restore.md.

Eligible rows: the models use at most the last 18 months of sales (NEARSHOP_ARCHIVE_KEEP_MONTHS, 18 or more),
so a calendar month (UTC, as sold_at is stored) is archived once it ends on or before the first day of the
current month less that many months. On 22 Oct 2026 the cutoff is 1 Apr 2025: March 2025 and earlier go.

Each month is archived on its own, oldest first, in these steps:
  1. its rows are read (sorted by _id) into a gzipped BSON file, byte for byte (the backup format, P1.6);
  2. the file goes to the private backup bucket (the NEARSHOP_BACKUP_* store, never a photo bucket) and is
     read back: its size and SHA-256 must match, and its documents must be exactly the rows read, by _id;
  3. the part's manifest (month, range, documents, size, SHA-256, a SHA-256 of the _ids, first and last _id,
     the database, environment, git commit and migration) is uploaded and read back byte for byte;
  4. only then are those rows, by _id, deleted from sales_history.
A failure in steps 1-3 stops the run with the month's rows untouched (step 4 is never reached). A failure
during step 4 (the deletes go in batches) leaves the month partly in Atlas but fully archived: its data file
and manifest are complete and verified in the store, and the next run archives the rows still in Atlas as a
new part of that month. Either way the run is recorded as failed. Rows that reach an archived month later
(an old sales report imported after the run) are left in place and go in a new part of that month in the
next run.

The parts of a month live under <prefix>/archive/sales_history/<YYYY-MM>/ in the store, where the prefix is
NEARSHOP_BACKUP_PREFIX (default: the environment name), as <part id>.bson.gz and <part id>.manifest.json,
the part id being the run's UTC start time (2026-11-01T230000Z). A part without its manifest is incomplete
and never restored. These keys are outside every backup set (<prefix>/<set id>/), so backup retention never
deletes them, and nothing in this code deletes an archived file.

Safeguards, before anything is deleted:
  * the store must be configured (NEARSHOP_BACKUP_BACKEND is off by default: the tool refuses to run);
  * in development and test, only a local MongoDB (as database/backup.py);
  * a backup of the same database must have succeeded in the last NEARSHOP_ARCHIVE_BACKUP_MAX_AGE_HOURS
    hours (backup_runs), so every archived row is also in a recent off-site set; --skip-backup-check is
    for a laptop's local database only.

Each run is recorded in the archive_runs collection of the database it archives: _id (the part id),
started_at, finished_at, outcome (running, succeeded or failed), cutoff, months (per month: documents,
deleted, bytes, sha256 and keys), documents, deleted and error.

--restore brings one month back: every complete part of it is downloaded, checked against its manifest and
inserted into the sales_history collection of the database named by --target-db, skipping _ids already
there (a month archived in two parts may hold a row twice). The target is a scratch database on the same
cluster: the database the API serves and the production database name are refused, and a production
archive restores only with NEARSHOP_ENVIRONMENT=production.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import re
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import bson
from bson import json_util
from bson.raw_bson import RawBSONDocument
from pymongo.database import Database as MongoDatabase
from pymongo.errors import BulkWriteError
from pymongo.read_concern import ReadConcern

from database.backup import (
    RAW,
    BackupNotConfigured,
    BackupStore,
    build_store,
    git_commit,
    latest_migration,
    load_manifest,
    local_only_problem,
    manifest_json,
    now_utc,
    set_id_for,
    sha256_file,
)
from database.backup import RUNS_COLLECTION as BACKUP_RUNS_COLLECTION

log = logging.getLogger("nearshop.archive")

FORMAT_VERSION = 1
SUPPORTED_FORMATS = (1,)
COLLECTION = "sales_history"
RUNS_COLLECTION = "archive_runs"
ARCHIVE_DIR = f"archive/{COLLECTION}"
DATA_SUFFIX = ".bson.gz"
MANIFEST_SUFFIX = ".manifest.json"
MONTH = re.compile(r"^(\d{4})-(\d{2})$")
BATCH_SIZE = 1000


class ArchiveRefused(RuntimeError):
    """A safeguard stopped the run before anything was written or deleted."""


class ArchiveVerifyFailed(RuntimeError):
    """What the store gave back differs from what was uploaded. Nothing was deleted."""


# ---------------------------------------------------------------- months


def month_start(month: str) -> datetime:
    match = MONTH.match(month)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ValueError(f"{month!r} is not a month; write it as YYYY-MM, for example 2025-03")
    return datetime(int(match.group(1)), int(match.group(2)), 1)


def add_months(moment: datetime, months: int) -> datetime:
    """The first day of the month `months` after moment's month (negative: before)."""
    index = moment.year * 12 + moment.month - 1 + months
    return datetime(index // 12, index % 12 + 1, 1)


def month_label(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def month_range(month: str) -> tuple[datetime, datetime]:
    start = month_start(month)
    return start, add_months(start, 1)


def cutoff_for(now: datetime, keep_months: int) -> datetime:
    """Rows sold before this are eligible: the first day of now's month, less keep_months months. Pure."""
    return add_months(now, -keep_months)


def eligible_months(mongo: MongoDatabase, cutoff: datetime) -> list[tuple[str, int]]:
    """The months with rows sold before cutoff, oldest first, with their row counts."""
    rows = mongo[COLLECTION].aggregate([
        {"$match": {"sold_at": {"$lt": cutoff}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$sold_at"}}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ])
    return [(r["_id"], r["n"]) for r in rows]


# ---------------------------------------------------------------- keys


def month_prefix(prefix: str, month: str) -> str:
    return f"{prefix}/{ARCHIVE_DIR}/{month}/"


def data_key(prefix: str, month: str, part_id: str) -> str:
    return month_prefix(prefix, month) + part_id + DATA_SUFFIX


def manifest_key(prefix: str, month: str, part_id: str) -> str:
    return month_prefix(prefix, month) + part_id + MANIFEST_SUFFIX


@dataclass(frozen=True)
class StoredPart:
    month: str
    part_id: str
    complete: bool  # its manifest exists


def list_parts(store: BackupStore, prefix: str, month: str | None = None) -> list[StoredPart]:
    """The archived parts under prefix (of one month, or all), oldest month and part first."""
    root = f"{prefix}/{ARCHIVE_DIR}/"
    found: dict[tuple[str, str], set[str]] = {}
    for key in store.list_keys(month_prefix(prefix, month) if month else root):
        parts = key[len(root):].split("/")
        if len(parts) != 2 or not MONTH.match(parts[0]):
            continue
        for suffix in (DATA_SUFFIX, MANIFEST_SUFFIX):
            if parts[1].endswith(suffix):
                found.setdefault((parts[0], parts[1][:-len(suffix)]), set()).add(suffix)
    return [StoredPart(m, p, MANIFEST_SUFFIX in kinds and DATA_SUFFIX in kinds)
            for (m, p), kinds in sorted(found.items())]


# ---------------------------------------------------------------- files


def ids_digest(ids: Iterable[Any]) -> str:
    """A SHA-256 of the _ids in order, so the documents read back can be checked as well as the bytes."""
    digest = hashlib.sha256()
    for value in ids:
        digest.update(json_util.dumps(value).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def read_documents(path: Path) -> Iterable[RawBSONDocument]:
    with gzip.open(path, "rb") as f:
        yield from bson.decode_file_iter(f, codec_options=RAW)


def export_month(mongo: MongoDatabase, month: str, path: Path) -> list[Any]:
    """Write the month's rows, sorted by _id, to a gzipped BSON file. Returns their _ids in that order."""
    start, end = month_range(month)
    coll = mongo.get_collection(COLLECTION, codec_options=RAW, read_concern=ReadConcern("majority"))
    ids: list[Any] = []
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
        for doc in coll.find({"sold_at": {"$gte": start, "$lt": end}}, sort=[("_id", 1)], batch_size=BATCH_SIZE):
            out.write(doc.raw)
            ids.append(doc["_id"])
    return ids


def check_data_file(path: Path, *, size: int, sha256: str, ids_sha256: str, documents: int) -> str | None:
    """Why the file is not the one described, or None: its size, SHA-256, document count and _ids."""
    if path.stat().st_size != size:
        return f"size {path.stat().st_size} bytes, expected {size}"
    if sha256_file(path) != sha256:
        return "SHA-256 differs"
    try:
        ids = [doc["_id"] for doc in read_documents(path)]
    except (OSError, EOFError, bson.errors.BSONError) as exc:
        return f"it does not decode ({type(exc).__name__})"
    if len(ids) != documents:
        return f"{len(ids)} documents, expected {documents}"
    if ids_digest(ids) != ids_sha256:
        return "its _ids differ"
    return None


def delete_rows(mongo: MongoDatabase, month: str, ids: Sequence[Any], batch_size: int = BATCH_SIZE) -> int:
    """Delete exactly these rows (by _id, within the month), in batches. Returns how many went."""
    start, end = month_range(month)
    deleted = 0
    for first in range(0, len(ids), batch_size):
        batch = list(ids[first:first + batch_size])
        res = mongo[COLLECTION].delete_many({"_id": {"$in": batch}, "sold_at": {"$gte": start, "$lt": end}})
        deleted += res.deleted_count
    return deleted


# ---------------------------------------------------------------- archiving


def archive_month(mongo: MongoDatabase, store: BackupStore, *, prefix: str, month: str, part_id: str,
                  environment: str, commit: str, migration: int, work_dir: Path,
                  batch_size: int = BATCH_SIZE) -> dict[str, Any] | None:
    """Export one month, upload it, verify the upload by reading it back, and only then delete its rows.
    Returns the month's entry for the run row, or None when the month has no rows."""
    start, end = month_range(month)
    path = work_dir / f"{month}{DATA_SUFFIX}"
    ids = export_month(mongo, month, path)
    if not ids:
        path.unlink()
        return None
    size, sha256, ids_sha256 = path.stat().st_size, sha256_file(path), ids_digest(ids)
    d_key, m_key = data_key(prefix, month, part_id), manifest_key(prefix, month, part_id)

    store.put_file(d_key, path)
    back = work_dir / f"{month}.check{DATA_SUFFIX}"
    store.get_file(d_key, back)
    if problem := check_data_file(back, size=size, sha256=sha256, ids_sha256=ids_sha256, documents=len(ids)):
        raise ArchiveVerifyFailed(f"{d_key} read back from the store differs from the upload: {problem}. "
                                  f"No row of {month} was deleted.")
    manifest = {
        "format": FORMAT_VERSION,
        "collection": COLLECTION,
        "month": month,
        "part": part_id,
        "file": part_id + DATA_SUFFIX,
        "range": {"from": start, "to": end},
        "database": mongo.name,
        "environment": environment,
        "commit": commit,
        "migration": migration,
        "documents": len(ids),
        "bytes": size,
        "sha256": sha256,
        "ids_sha256": ids_sha256,
        "first_id": ids[0],
        "last_id": ids[-1],
        "created_at": now_utc(),
    }
    text = manifest_json(manifest)
    m_path = work_dir / f"{month}{MANIFEST_SUFFIX}"
    m_path.write_text(text, encoding="utf-8")
    store.put_file(m_key, m_path)
    m_back = work_dir / f"{month}.check{MANIFEST_SUFFIX}"
    store.get_file(m_key, m_back)
    if m_back.read_text(encoding="utf-8") != text:
        raise ArchiveVerifyFailed(f"{m_key} read back from the store differs from the upload. "
                                  f"No row of {month} was deleted.")

    deleted = delete_rows(mongo, month, ids, batch_size=batch_size)
    for p in (path, back, m_path, m_back):
        p.unlink()
    log.info("archived %s: %d rows, %d bytes, %d deleted", month, len(ids), size, deleted)
    return {"month": month, "documents": len(ids), "deleted": deleted, "bytes": size, "sha256": sha256,
            "data_key": d_key, "manifest_key": m_key}


def backup_problem(mongo: MongoDatabase, now: datetime, max_age: timedelta) -> str | None:
    """Why there is no recent enough backup of this database, or None."""
    newest = mongo[BACKUP_RUNS_COLLECTION].find_one({"outcome": "succeeded"}, sort=[("finished_at", -1)])
    if newest is None:
        return (f"no backup of {mongo.name!r} has succeeded ({BACKUP_RUNS_COLLECTION} is empty). Archiving deletes "
                f"rows only once a recent off-site backup also holds them.")
    finished = newest.get("finished_at") or newest["started_at"]
    if finished < now - max_age:
        return (f"the newest backup of {mongo.name!r} that succeeded finished at {finished:%Y-%m-%d %H:%M} UTC, "
                f"more than {max_age.total_seconds() / 3600:g} hours ago. Fix the backup first "
                f"(docs/runbooks/restore.md); archiving deletes rows only once a recent backup also holds them.")
    return None


def run_archive(mongo: MongoDatabase, store: BackupStore, *, prefix: str, environment: str, keep_months: int,
                backup_max_age: timedelta | None, now: datetime | None = None, commit: str | None = None,
                work_dir: Path | None = None, batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """One recorded run: every eligible month, oldest first. Returns the archive_runs row. A refusal
    (ArchiveRefused) writes nothing; any other failure is recorded as outcome failed and then raised."""
    started = now or now_utc()
    if backup_max_age is not None and (problem := backup_problem(mongo, started, backup_max_age)):
        raise ArchiveRefused(problem)
    part_id = set_id_for(started)
    cutoff = cutoff_for(started, keep_months)
    runs = mongo[RUNS_COLLECTION]
    runs.insert_one({"_id": part_id, "prefix": prefix, "environment": environment, "started_at": started,
                     "cutoff": cutoff, "outcome": "running", "months": []})
    done: list[dict[str, Any]] = []
    try:
        commit = commit if commit is not None else git_commit()
        migration = latest_migration(mongo)
        with tempfile.TemporaryDirectory(prefix="nearshop-archive-", dir=work_dir) as tmp:
            for month, _ in eligible_months(mongo, cutoff):
                entry = archive_month(mongo, store, prefix=prefix, month=month, part_id=part_id,
                                      environment=environment, commit=commit, migration=migration,
                                      work_dir=Path(tmp), batch_size=batch_size)
                if entry:
                    done.append(entry)
                    runs.update_one({"_id": part_id}, {"$push": {"months": entry}})
    except Exception as exc:
        runs.update_one({"_id": part_id}, {"$set": {
            "outcome": "failed", "finished_at": now_utc(), "error": f"{type(exc).__name__}: {exc}"[:500]}})
        raise
    runs.update_one({"_id": part_id}, {"$set": {
        "outcome": "succeeded", "finished_at": now_utc(), "documents": sum(e["documents"] for e in done),
        "deleted": sum(e["deleted"] for e in done), "bytes": sum(e["bytes"] for e in done)}})
    return runs.find_one({"_id": part_id})


# ---------------------------------------------------------------- bringing a month back


def target_problem(target_db: str, *, live_db: str, production_db: str, environment: str) -> str | None:
    """Why archived rows may not be restored into target_db, or None."""
    from database.restore import protected_names

    if not target_db or not target_db.strip():
        return "name the target database with --target-db."
    if target_db in protected_names(environment=environment, production_db=production_db, app_db=live_db) | {live_db}:
        return (f"{target_db!r} is the database the API serves or the production database. Restore into a scratch "
                f"database (docs/runbooks/restore.md, Archived sales history) and copy rows across by hand if "
                f"they are needed back.")
    return None


def restore_month(target: MongoDatabase, store: BackupStore, *, prefix: str, month: str, environment: str,
                  work_dir: Path, batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """Insert every complete part of the month into target's sales_history, skipping _ids already there.
    Every part is downloaded and checked before the first insert."""
    month_start(month)  # a well-formed month
    parts = [p for p in list_parts(store, prefix, month) if p.complete]
    if not parts:
        raise ArchiveRefused(f"no complete archived part of {month} under {month_prefix(prefix, month)}.")
    checked: list[tuple[dict[str, Any], Path]] = []
    for part in parts:
        m_path = work_dir / f"{part.part_id}{MANIFEST_SUFFIX}"
        store.get_file(manifest_key(prefix, month, part.part_id), m_path)
        manifest = load_manifest(m_path.read_text(encoding="utf-8"))
        if manifest.get("format") not in SUPPORTED_FORMATS or manifest.get("month") != month:
            raise ArchiveRefused(f"part {part.part_id} of {month} has a manifest this code does not read.")
        if manifest.get("environment") == "production" and environment != "production":
            raise ArchiveRefused(f"{month} was archived in production; it restores only with "
                                 f"NEARSHOP_ENVIRONMENT=production (this is {environment!r}).")
        path = work_dir / f"{part.part_id}{DATA_SUFFIX}"
        store.get_file(data_key(prefix, month, part.part_id), path)
        if problem := check_data_file(path, size=manifest["bytes"], sha256=manifest["sha256"],
                                      ids_sha256=manifest["ids_sha256"], documents=manifest["documents"]):
            raise ArchiveRefused(f"part {part.part_id} of {month} does not match its manifest: {problem}. "
                                 f"Nothing was written.")
        checked.append((manifest, path))

    coll = target[COLLECTION]
    inserted = skipped = 0
    for _, path in checked:
        batch: list[RawBSONDocument] = []
        for doc in read_documents(path):
            batch.append(doc)
            if len(batch) >= batch_size:
                n = _insert_new(coll, batch)
                inserted, skipped, batch = inserted + n, skipped + len(batch) - n, []
        if batch:
            n = _insert_new(coll, batch)
            inserted, skipped = inserted + n, skipped + len(batch) - n
    start, end = month_range(month)
    present = coll.count_documents({"sold_at": {"$gte": start, "$lt": end}})
    return {"month": month, "parts": len(checked), "documents": sum(m["documents"] for m, _ in checked),
            "inserted": inserted, "skipped_duplicates": skipped, "rows_in_target_month": present,
            "target": target.name}


def _insert_new(coll, batch: list[RawBSONDocument]) -> int:
    """Insert the batch, skipping documents whose _id is already there. Returns how many went in."""
    try:
        coll.insert_many(batch, ordered=False, bypass_document_validation=True)
        return len(batch)  # inserted_ids is empty for raw documents
    except BulkWriteError as exc:
        errors = exc.details.get("writeErrors", [])
        if any(e.get("code") != 11000 for e in errors):
            raise
        return exc.details.get("nInserted", 0)


# ---------------------------------------------------------------- command line


def print_parts(store: BackupStore, prefix: str) -> None:
    parts = list_parts(store, prefix)
    if not parts:
        print(f"no archived months under {prefix}/{ARCHIVE_DIR}/")
    for p in parts:
        print(f"{p.month}  part {p.part_id}  {'complete' if p.complete else 'INCOMPLETE'}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m database.archive", description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--plan", action="store_true", help="only list the eligible months and their rows")
    action.add_argument("--list", action="store_true", help="only list the archived months in the store")
    action.add_argument("--restore", metavar="YYYY-MM", help="bring one archived month back into --target-db")
    parser.add_argument("--target-db", default="", help="the scratch database --restore writes into")
    parser.add_argument("--skip-backup-check", action="store_true",
                        help="archive without a recent backup (a laptop's local database only)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from app.core.config import settings

    if problem := local_only_problem(settings.environment, settings.mongodb_uri):
        print(f"Archive refused; nothing was written. {problem}", file=sys.stderr)
        return 2
    if args.skip_backup_check and settings.environment not in ("development", "test"):
        print("Archive refused; nothing was written. --skip-backup-check is for a laptop's local database only.",
              file=sys.stderr)
        return 2
    if args.restore is not None:
        try:
            month_start(args.restore)
        except ValueError as exc:
            print(f"Archive restore refused; nothing was written. {exc}", file=sys.stderr)
            return 2
        if problem := target_problem(args.target_db, live_db=settings.mongodb_db,
                                     production_db=settings.production_mongodb_db, environment=settings.environment):
            print(f"Archive restore refused; nothing was written. {problem}", file=sys.stderr)
            return 2

    from app.core.database import client

    mongo = client[settings.mongodb_db]
    cutoff = cutoff_for(now_utc(), settings.archive_keep_months)
    if args.plan:
        months = eligible_months(mongo, cutoff)
        print(json.dumps({"database": mongo.name, "cutoff": f"{cutoff:%Y-%m-%d}",
                          "months": [{"month": m, "rows": n} for m, n in months],
                          "rows": sum(n for _, n in months)}))
        return 0
    try:
        store = build_store(settings)
    except BackupNotConfigured as exc:
        print(f"Archive refused; nothing was written. {exc}", file=sys.stderr)
        return 2
    prefix = settings.backup_set_prefix
    if args.list:
        print_parts(store, prefix)
        return 0
    if args.restore is not None:
        try:
            with tempfile.TemporaryDirectory(prefix="nearshop-archive-restore-") as tmp:
                summary = restore_month(client[args.target_db], store, prefix=prefix, month=args.restore,
                                        environment=settings.environment, work_dir=Path(tmp))
        except ArchiveRefused as exc:
            print(f"Archive restore refused; nothing was written. {exc}", file=sys.stderr)
            return 2
        print(json.dumps(summary))
        return 0

    max_age = None if args.skip_backup_check else timedelta(hours=settings.archive_backup_max_age_hours)
    try:
        row = run_archive(mongo, store, prefix=prefix, environment=settings.environment,
                          keep_months=settings.archive_keep_months, backup_max_age=max_age)
    except ArchiveRefused as exc:
        print(f"Archive refused; nothing was written or deleted. {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        log.exception("archive failed")
        print(f"Archive failed ({type(exc).__name__}); recorded in {RUNS_COLLECTION}. Rows of the month that failed "
              f"were not deleted.", file=sys.stderr)
        return 1
    print(json.dumps({"run": row["_id"], "database": mongo.name, "outcome": row["outcome"],
                      "cutoff": f"{row['cutoff']:%Y-%m-%d}", "months": [e["month"] for e in row["months"]],
                      "documents": row["documents"], "deleted": row["deleted"], "bytes": row["bytes"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
