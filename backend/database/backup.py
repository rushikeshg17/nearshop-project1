"""Off-site backups: every collection as gzipped BSON, plus a manifest, in a private bucket.

    uv run python -m database.backup              # back up NEARSHOP_MONGODB_DB, then delete expired sets
    uv run python -m database.backup --no-prune   # back up and keep every older set
    uv run python -m database.backup --list       # read-only: the sets in the store

Render runs it every night at 21:30 UTC (03:00 IST) as a cron job for production and for staging
(render.yaml). Restoring a set: database/restore.py and docs/runbooks/restore.md.

A set lives under <prefix>/<set id>/ in the store, where the prefix is NEARSHOP_BACKUP_PREFIX (default:
the environment name) and the set id is the UTC start time, 2026-10-21T213000Z. It holds:

  * <collection>.bson.gz: every document of the collection, as concatenated BSON (the format of
    mongodump), gzipped. Documents are copied byte for byte, so every BSON type survives.
  * manifest.json, written last (a set without one is incomplete): the database, environment, git
    commit, latest applied migration number, and for each collection its document count, validator and
    options, index specs, file size and SHA-256. Extended JSON (bson.json_util, relaxed mode).

Every collection is copied except system collections, the migration lock and backup_runs. Collections
are read one after another, not at a single instant; counters is read last, so every id in the set was
issued before the counters in the set were read, and a restored database never hands out an id twice.
For a copy taken at one instant, use the Atlas snapshot (runbook).

Retention (plan section 8.4): the newest set of each of the last NEARSHOP_BACKUP_KEEP_DAILY days (30)
and the first set of each of the last NEARSHOP_BACKUP_KEEP_MONTHLY months (12), counting the current
day and month; every other complete set is deleted after a successful backup. An incomplete set (a run
that died before its manifest) is deleted once it is a day old. Keys the tool did not write are never
touched.

Each run is recorded in the backup_runs collection of the database it backs up: _id (the set id),
started_at, finished_at, outcome (running, succeeded or failed), size_bytes, documents, collections,
deleted_sets and error. The newest succeeded row is the heartbeat: alert when it is more than 26 hours
old (plan section 8.10).

In development and test it runs only against a local MongoDB (as database/migrate.py does), so a
backend/.env pointing at a hosted cluster is never copied to a laptop. Credentials come only from the
environment: NEARSHOP_MONGODB_URI and the NEARSHOP_BACKUP_* settings (app/core/config.py).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from bson import json_util
from bson.codec_options import CodecOptions
from bson.raw_bson import RawBSONDocument
from pymongo.database import Database as MongoDatabase
from pymongo.read_concern import ReadConcern

from app.core.config import BACKEND_DIR, Settings
from database.migrate import LOCK_COLLECTION, MIGRATIONS_COLLECTION

log = logging.getLogger("nearshop.backup")

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
FILE_SUFFIX = ".bson.gz"
RUNS_COLLECTION = "backup_runs"
COUNTERS_COLLECTION = "counters"
# Never copied: the migration lock is a runner's transient state, and backup_runs describes the sets
# themselves (a set would otherwise hold its own run as "running").
EXCLUDED_COLLECTIONS = frozenset({LOCK_COLLECTION, RUNS_COLLECTION})
SET_ID_FORMAT = "%Y-%m-%dT%H%M%SZ"
SET_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")
INCOMPLETE_GRACE = timedelta(days=1)  # an incomplete set younger than this may be a run still uploading
RAW = CodecOptions(document_class=RawBSONDocument)
CHUNK = 1024 * 1024


class BackupNotConfigured(RuntimeError):
    """NEARSHOP_BACKUP_BACKEND is off, or its required settings are missing. Nothing was written."""


# ---------------------------------------------------------------- the store


class BackupStore(Protocol):
    """Where sets go: a private S3-compatible bucket (Cloudflare R2), or a local directory."""

    def put_file(self, key: str, path: Path) -> None: ...

    def get_file(self, key: str, path: Path) -> None: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete_keys(self, keys: Sequence[str]) -> None: ...


class LocalBackupStore:
    """Sets in a directory: development, and drills against a local database. Keys are relative paths."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"backup key {key!r} escapes the backup directory")
        return path

    def put_file(self, key: str, path: Path) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    def get_file(self, key: str, path: Path) -> None:
        shutil.copyfile(self._path(key), path)

    def list_keys(self, prefix: str) -> list[str]:
        if not self.root.is_dir():
            return []
        keys = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix))

    def delete_keys(self, keys: Sequence[str]) -> None:
        for key in keys:
            path = self._path(key)
            if path.exists():
                path.unlink()
            for parent in path.parents:  # remove the set's directory once it is empty
                if parent == self.root.resolve() or any(parent.iterdir()):
                    break
                parent.rmdir()


class S3BackupStore:
    """A private S3-compatible bucket: the Cloudflare R2 backup bucket of staging or production."""

    def __init__(self, bucket: str, client):
        self.bucket = bucket
        self.client = client

    @classmethod
    def from_settings(cls, cfg: Settings) -> S3BackupStore:
        import boto3  # imported only when this backend is selected
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=cfg.backup_s3_endpoint,
            aws_access_key_id=cfg.backup_s3_access_key_id,
            aws_secret_access_key=cfg.backup_s3_secret_access_key.get_secret_value(),
            region_name=cfg.backup_s3_region,
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"},
                          connect_timeout=10, read_timeout=120,
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )
        return cls(cfg.backup_s3_bucket, client)

    def put_file(self, key: str, path: Path) -> None:
        # upload_file switches to a multipart upload for large files.
        self.client.upload_file(str(path), self.bucket, key)

    def get_file(self, key: str, path: Path) -> None:
        self.client.download_file(self.bucket, key, str(path))

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)

    def delete_keys(self, keys: Sequence[str]) -> None:
        keys = list(keys)
        for start in range(0, len(keys), 1000):  # DeleteObjects takes at most 1,000 keys
            batch = keys[start:start + 1000]
            res = self.client.delete_objects(Bucket=self.bucket, Delete={
                "Objects": [{"Key": k} for k in batch], "Quiet": True})
            if errors := res.get("Errors"):
                raise RuntimeError(f"the bucket refused to delete {len(errors)} backup objects, "
                                   f"for example {errors[0].get('Key')!r}: {errors[0].get('Code')}")


def store_problem(cfg: Settings) -> str | None:
    """Why the backup store cannot be used with these settings, or None."""
    if cfg.backup_backend == "off":
        return ("NEARSHOP_BACKUP_BACKEND is off. Set it to s3 (with the NEARSHOP_BACKUP_S3_* settings of the "
                "private backup bucket) or, on a laptop, to local with NEARSHOP_BACKUP_DIR.")
    if cfg.backup_backend == "local":
        return None if cfg.backup_dir else "NEARSHOP_BACKUP_BACKEND=local needs NEARSHOP_BACKUP_DIR."
    required = {
        "NEARSHOP_BACKUP_S3_ENDPOINT": cfg.backup_s3_endpoint,
        "NEARSHOP_BACKUP_S3_BUCKET": cfg.backup_s3_bucket,
        "NEARSHOP_BACKUP_S3_ACCESS_KEY_ID": cfg.backup_s3_access_key_id,
        "NEARSHOP_BACKUP_S3_SECRET_ACCESS_KEY": cfg.backup_s3_secret_access_key.get_secret_value(),
    }
    if missing := [name for name, value in required.items() if not value.strip()]:
        return f"NEARSHOP_BACKUP_BACKEND=s3 needs {', '.join(missing)}."
    if cfg.backup_s3_bucket.strip() == cfg.media_s3_bucket.strip():
        return "NEARSHOP_BACKUP_S3_BUCKET is the photo bucket; backups go only to the private backup bucket."
    return None


def build_store(cfg: Settings) -> BackupStore:
    if problem := store_problem(cfg):
        raise BackupNotConfigured(problem)
    if cfg.backup_backend == "local":
        assert cfg.backup_dir is not None
        return LocalBackupStore(cfg.backup_dir)
    return S3BackupStore.from_settings(cfg)


# ---------------------------------------------------------------- sets


def now_utc() -> datetime:
    """Naive UTC, truncated to milliseconds, like app.core.database.utcnow."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def set_id_for(moment: datetime) -> str:
    return moment.strftime(SET_ID_FORMAT)


def parse_set_id(set_id: str) -> datetime | None:
    if not SET_ID.match(set_id):
        return None
    try:
        return datetime.strptime(set_id, SET_ID_FORMAT)
    except ValueError:
        return None


def set_key(prefix: str, set_id: str, name: str) -> str:
    return f"{prefix}/{set_id}/{name}"


@dataclass(frozen=True)
class StoredSet:
    set_id: str
    taken_at: datetime
    complete: bool  # its manifest exists
    keys: tuple[str, ...]


def list_sets(store: BackupStore, prefix: str) -> list[StoredSet]:
    """The sets under prefix, oldest first. Keys outside <prefix>/<set id>/ are ignored."""
    found: dict[str, list[str]] = {}
    for key in store.list_keys(prefix + "/"):
        parts = key[len(prefix) + 1:].split("/")
        if len(parts) == 2 and parse_set_id(parts[0]):
            found.setdefault(parts[0], []).append(key)
    return [StoredSet(sid, parse_set_id(sid), set_key(prefix, sid, MANIFEST) in keys, tuple(sorted(keys)))
            for sid, keys in sorted(found.items())]


def latest_complete_set(store: BackupStore, prefix: str) -> str | None:
    complete = [s.set_id for s in list_sets(store, prefix) if s.complete]
    return complete[-1] if complete else None


def _month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def sets_to_keep(set_ids: Iterable[str], today: date, *, keep_daily: int, keep_monthly: int) -> set[str]:
    """The complete sets the retention rule keeps: the newest set of each of the last keep_daily days and
    the first set of each of the last keep_monthly months, both counting today. Pure."""
    by_day: dict[date, list[str]] = {}
    by_month: dict[int, list[str]] = {}
    for set_id in set_ids:
        taken = parse_set_id(set_id)
        if taken is None:
            continue
        by_day.setdefault(taken.date(), []).append(set_id)
        by_month.setdefault(_month_index(taken.date()), []).append(set_id)
    keep = {max(ids) for day, ids in by_day.items() if 0 <= (today - day).days < keep_daily}
    keep |= {min(ids) for month, ids in by_month.items() if 0 <= _month_index(today) - month < keep_monthly}
    # Sets dated after today (a clock moved back) are kept rather than guessed about.
    keep |= {sid for day, ids in by_day.items() if day > today for sid in ids}
    return keep


def plan_prune(sets: Sequence[StoredSet], now: datetime, *, keep_daily: int, keep_monthly: int,
               protect: Iterable[str] = ()) -> list[StoredSet]:
    """The sets to delete. The newest complete set and every set in protect are always kept. Pure."""
    complete = [s for s in sets if s.complete]
    keep = sets_to_keep((s.set_id for s in complete), now.date(), keep_daily=keep_daily, keep_monthly=keep_monthly)
    keep |= set(protect)
    if complete:
        keep.add(complete[-1].set_id)
    return [s for s in sets if s.set_id not in keep and (s.complete or s.taken_at < now - INCOMPLETE_GRACE)]


def prune(store: BackupStore, prefix: str, now: datetime, *, keep_daily: int, keep_monthly: int,
          protect: Iterable[str] = ()) -> list[str]:
    """Delete the sets the retention rule no longer keeps. Returns their set ids."""
    doomed = plan_prune(list_sets(store, prefix), now, keep_daily=keep_daily, keep_monthly=keep_monthly,
                        protect=protect)
    for stored in doomed:
        # The manifest goes first, so a set whose deletion is interrupted is incomplete, never half-complete.
        manifest = set_key(prefix, stored.set_id, MANIFEST)
        store.delete_keys([k for k in stored.keys if k == manifest])
        store.delete_keys([k for k in stored.keys if k != manifest])
        log.info("deleted backup set %s/%s", prefix, stored.set_id)
    return [s.set_id for s in doomed]


# ---------------------------------------------------------------- the backup


def git_commit() -> str:
    """The commit of the running code: RENDER_GIT_COMMIT on Render, else git, else unknown."""
    if commit := os.environ.get("RENDER_GIT_COMMIT", "").strip():
        return commit
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BACKEND_DIR, capture_output=True, text=True,
                             timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def latest_migration(mongo: MongoDatabase) -> int:
    doc = mongo[MIGRATIONS_COLLECTION].find_one({}, {"number": 1}, sort=[("number", -1)])
    return int(doc["number"]) if doc else 0


def collections_to_copy(mongo: MongoDatabase) -> list[str]:
    """Every collection except system and excluded ones, sorted, with counters last."""
    names = sorted(c["name"] for c in mongo.list_collections(filter={"type": "collection"})
                   if not c["name"].startswith("system.") and c["name"] not in EXCLUDED_COLLECTIONS)
    if COUNTERS_COLLECTION in names:
        names.remove(COUNTERS_COLLECTION)
        names.append(COUNTERS_COLLECTION)
    return names


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def dump_collection(mongo: MongoDatabase, name: str, path: Path) -> int:
    """Stream one collection into a gzipped BSON file, document by document. Returns the count."""
    coll = mongo.get_collection(name, codec_options=RAW, read_concern=ReadConcern("majority"))
    count = 0
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
        for doc in coll.find({}, batch_size=1000):
            out.write(doc.raw)
            count += 1
    return count


def collection_metadata(mongo: MongoDatabase, name: str) -> dict[str, Any]:
    info = next(iter(mongo.list_collections(filter={"name": name})), {}) or {}
    indexes = []
    for spec in mongo[name].list_indexes():
        spec = dict(spec)
        spec.pop("ns", None)
        indexes.append(spec)
    return {"options": dict(info.get("options", {})), "indexes": indexes}


def manifest_json(manifest: dict[str, Any]) -> str:
    return json_util.dumps(manifest, json_options=json_util.RELAXED_JSON_OPTIONS, indent=2)


def load_manifest(text: str) -> dict[str, Any]:
    return json_util.loads(text, json_options=json_util.RELAXED_JSON_OPTIONS)


def create_backup(mongo: MongoDatabase, store: BackupStore, *, prefix: str, set_id: str, environment: str,
                  commit: str | None = None, work_dir: Path | None = None) -> dict[str, Any]:
    """Copy every collection to the store under <prefix>/<set id>/, then its manifest. Returns the manifest."""
    started = now_utc()
    collections: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="nearshop-backup-", dir=work_dir) as tmp:
        migration = latest_migration(mongo)
        for name in collections_to_copy(mongo):
            meta = collection_metadata(mongo, name)
            file_name = name + FILE_SUFFIX
            path = Path(tmp) / file_name
            count = dump_collection(mongo, name, path)
            size = path.stat().st_size
            entry = {"file": file_name, "documents": count, "bytes": size, "sha256": sha256_file(path), **meta}
            store.put_file(set_key(prefix, set_id, file_name), path)
            path.unlink()
            collections[name] = entry
            log.info("backed up %s: %d documents, %d bytes", name, count, size)
        manifest = {
            "format": FORMAT_VERSION,
            "set_id": set_id,
            "database": mongo.name,
            "environment": environment,
            "commit": commit if commit is not None else git_commit(),
            "migration": migration,
            "started_at": started,
            "finished_at": now_utc(),
            "documents": sum(c["documents"] for c in collections.values()),
            "bytes": sum(c["bytes"] for c in collections.values()),
            "collections": collections,
        }
        path = Path(tmp) / MANIFEST
        path.write_text(manifest_json(manifest), encoding="utf-8")
        store.put_file(set_key(prefix, set_id, MANIFEST), path)  # last: the set is complete only now
    return manifest


def run_backup(mongo: MongoDatabase, store: BackupStore, *, prefix: str, environment: str,
               keep_daily: int, keep_monthly: int, do_prune: bool = True, now: datetime | None = None,
               commit: str | None = None, work_dir: Path | None = None) -> dict[str, Any]:
    """One recorded run: the backup, then pruning. Returns the backup_runs row. A failure is recorded
    as outcome failed and then raised."""
    started = now or now_utc()
    set_id = set_id_for(started)
    runs = mongo[RUNS_COLLECTION]
    runs.insert_one({"_id": set_id, "prefix": prefix, "environment": environment, "started_at": started,
                     "outcome": "running"})
    try:
        manifest = create_backup(mongo, store, prefix=prefix, set_id=set_id, environment=environment,
                                 commit=commit, work_dir=work_dir)
        deleted = prune(store, prefix, started, keep_daily=keep_daily, keep_monthly=keep_monthly,
                        protect=[set_id]) if do_prune else []
    except Exception as exc:
        runs.update_one({"_id": set_id}, {"$set": {
            "outcome": "failed", "finished_at": now_utc(), "error": f"{type(exc).__name__}: {exc}"[:500]}})
        raise
    row = {"outcome": "succeeded", "finished_at": now_utc(), "size_bytes": manifest["bytes"],
           "documents": manifest["documents"], "collections": len(manifest["collections"]),
           "migration": manifest["migration"], "commit": manifest["commit"], "deleted_sets": deleted}
    runs.update_one({"_id": set_id}, {"$set": row})
    return runs.find_one({"_id": set_id})


# ---------------------------------------------------------------- command line


def local_only_problem(environment: str, uri: str) -> str | None:
    """Why this command must not read or write the configured database from this environment, or None.
    In development and test only a local MongoDB is allowed (the reset guard's hosts)."""
    from database.schema import LOCAL_HOSTS, LOCAL_ONLY_ENVIRONMENTS, uri_hosts

    if environment in LOCAL_ONLY_ENVIRONMENTS:
        if remote := [h for h in uri_hosts(uri) if h not in LOCAL_HOSTS]:
            return (f"NEARSHOP_ENVIRONMENT is {environment!r} but MongoDB host {remote[0]!r} is not local. "
                    f"Staging and production back up and restore on Render (docs/runbooks/restore.md); no "
                    f"laptop copies a hosted database.")
    return None


def print_sets(store: BackupStore, prefix: str) -> None:
    sets = list_sets(store, prefix)
    if not sets:
        print(f"no backup sets under {prefix}/")
    for s in sets:
        print(f"{prefix}/{s.set_id}  {'complete' if s.complete else 'INCOMPLETE'}  {len(s.keys)} files")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m database.backup", description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="only list the sets in the store")
    action.add_argument("--no-prune", action="store_true", help="back up, and delete no older set")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from app.core.config import settings

    try:
        store = build_store(settings)
    except BackupNotConfigured as exc:
        print(f"Backup refused; nothing was written. {exc}", file=sys.stderr)
        return 2
    prefix = settings.backup_set_prefix
    if args.list:
        print_sets(store, prefix)
        return 0
    if problem := local_only_problem(settings.environment, settings.mongodb_uri):
        print(f"Backup refused; nothing was written. {problem}", file=sys.stderr)
        return 2

    from app.core.database import client

    mongo = client[settings.mongodb_db]
    try:
        row = run_backup(mongo, store, prefix=prefix, environment=settings.environment,
                         keep_daily=settings.backup_keep_daily, keep_monthly=settings.backup_keep_monthly,
                         do_prune=not args.no_prune)
    except Exception as exc:
        log.exception("backup failed")
        print(f"Backup failed ({type(exc).__name__}); recorded in {RUNS_COLLECTION}.", file=sys.stderr)
        return 1
    seconds = (row["finished_at"] - row["started_at"]).total_seconds()
    print(json.dumps({"set": f"{prefix}/{row['_id']}", "database": mongo.name, "outcome": row["outcome"],
                      "documents": row["documents"], "collections": row["collections"],
                      "size_bytes": row["size_bytes"], "seconds": round(seconds, 1),
                      "deleted_sets": row["deleted_sets"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
