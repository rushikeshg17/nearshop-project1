"""Restore an off-site backup set (database/backup.py) into a named, empty database.

    uv run python -m database.restore --target-db nearshop_drill                  # the latest complete set
    uv run python -m database.restore --target-db nearshop_drill --set 2026-10-21T213000Z
    uv run python -m database.restore --list                                      # read-only: the sets
    uv run python -m database.restore --target-db nearshop_prod --set <id> --emergency   # see below

The target is a database on the cluster in NEARSHOP_MONGODB_URI, named by --target-db; there is no
default. The sets come from the store in the NEARSHOP_BACKUP_* settings, under NEARSHOP_BACKUP_PREFIX
(default: the environment name). The step-by-step procedure and the drill log: docs/runbooks/restore.md.

Safeguards, all checked before anything is written:
  * the target must be empty: it may hold no collection at all, not even an empty one (one made by the
    code's schema would carry this code's indexes and validators), so name a database that does not
    exist yet; MongoDB creates it on the first write;
  * the production database name is refused (NEARSHOP_PRODUCTION_MONGODB_DB, and with
    NEARSHOP_ENVIRONMENT=production also NEARSHOP_MONGODB_DB, the database the API serves) unless
    --emergency is given and the operator types the database name when asked;
  * a set taken in production restores only with NEARSHOP_ENVIRONMENT=production, so production's
    customers never land in a staging, demo or laptop database;
  * in development and test, only a local MongoDB (as database/migrate.py);
  * every file must match the size and SHA-256 in the manifest, and the set's migration number must be
    one this code knows.

Then, in order, so the documents meet the schema they were written under and never this code's newer one:
  1. every collection of the set is created with the options in the manifest (its validator among
     them): the schema at the set's migration;
  2. every collection's documents, schema_migrations and counters among them, are inserted in batches,
     byte for byte, skipping validation (they passed the validators when they were written; a restore
     must bring all of them back);
  3. each collection's document count is checked against the manifest, and any difference fails the
     restore (exit 1);
  4. the set's indexes are built from the manifest, after the documents as mongorestore does (faster,
     and a TTL index cannot remove a document before it is counted);
  5. any counter below the largest id of its collection is raised to it (none should be: backup.py
     reads counters last);
  6. the migration runner applies the migrations newer than the set, if the code has any, and then
     ensure_schema brings validators, indexes and new collections to this code's schema, exactly as a
     deploy of this code would on the database the set was taken from.
It prints a JSON summary with the time taken, the figure the drill log records.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import bson
from bson.raw_bson import RawBSONDocument
from pymongo.database import Database as MongoDatabase

from database import migrate
from database.backup import (
    COUNTERS_COLLECTION,
    MANIFEST,
    RAW,
    BackupNotConfigured,
    BackupStore,
    build_store,
    latest_complete_set,
    load_manifest,
    local_only_problem,
    print_sets,
    set_key,
    sha256_file,
)
from database.migrations import Migration, discover
from database.schema import VALIDATORS

log = logging.getLogger("nearshop.restore")

BATCH_SIZE = 1000
SUPPORTED_FORMATS = (1,)

Ask = Callable[[str], str]


class RestoreRefused(RuntimeError):
    """A safeguard stopped the restore before anything was written."""


class RestoreFailed(RuntimeError):
    """The restore wrote to the target but did not finish, or its counts do not match the manifest."""


# ---------------------------------------------------------------- safeguards


def protected_names(*, environment: str, production_db: str, app_db: str) -> set[str]:
    """Database names that are production: NEARSHOP_PRODUCTION_MONGODB_DB always, and in production the
    database the API serves (NEARSHOP_MONGODB_DB)."""
    names = {production_db}
    if environment == "production":
        names.add(app_db)
    return names


def name_problem(target_db: str, protected: Iterable[str], *, emergency: bool, ask: Ask) -> str | None:
    """Why target_db may not be restored into, or None. A production name needs --emergency and the
    operator typing the name; ask is only called then."""
    if not target_db or not target_db.strip():
        return "name the target database with --target-db."
    if target_db not in set(protected):
        return None
    if not emergency:
        return (f"{target_db!r} is the production database. Restore into a scratch database and switch "
                f"NEARSHOP_MONGODB_DB to it (docs/runbooks/restore.md), or, in an emergency only, add "
                f"--emergency and type the database name when asked.")
    try:
        typed = ask(f"EMERGENCY restore into the production database {target_db!r}. Type the database name "
                    f"to continue: ").strip()
    except EOFError:  # no terminal (a Render job): nobody typed it
        return f"--emergency needs the operator to type {target_db!r} at a terminal; nothing was typed."
    if typed != target_db:
        return f"the typed name {typed!r} does not match {target_db!r}."
    return None


def existing_collections(target: MongoDatabase) -> list[str]:
    """Collections of the target (system collections aside), empty or not. A restore needs none."""
    return sorted(name for name in target.list_collection_names() if not name.startswith("system."))


def not_empty_problem(target: MongoDatabase) -> str | None:
    if not (names := existing_collections(target)):
        return None
    held = [n for n in names if target[n].find_one({}, {"_id": 1}) is not None]
    listed = ", ".join(names[:5]) + ("..." if len(names) > 5 else "")
    what = (f"{len(held)} of them {'holds' if len(held) == 1 else 'hold'} documents" if held else "all empty")
    return (f"Database {target.name!r} is not empty: it has collections ({listed}; {what}). Restore into a "
            f"database name that does not exist yet.")


def environment_problem(manifest: dict[str, Any], environment: str) -> str | None:
    if manifest.get("environment") == "production" and environment != "production":
        return (f"set {manifest.get('set_id')} was taken in production; it restores only with "
                f"NEARSHOP_ENVIRONMENT=production (this is {environment!r}). Production data never goes to "
                f"another environment.")
    return None


def manifest_problem(manifest: dict[str, Any], migrations: Sequence[Migration]) -> str | None:
    if manifest.get("format") not in SUPPORTED_FORMATS:
        return f"the manifest's format {manifest.get('format')!r} is not one this code reads {SUPPORTED_FORMATS}."
    expected = max((m.number for m in migrations), default=0)
    if int(manifest.get("migration", 0)) > expected:
        return (f"set {manifest.get('set_id')} is at migration {manifest['migration']} but this code knows "
                f"migrations up to {expected}. Restore with the code of commit {manifest.get('commit')}.")
    return None


# ---------------------------------------------------------------- fetching


def fetch_manifest(store: BackupStore, prefix: str, set_id: str, work_dir: Path) -> dict[str, Any]:
    key = set_key(prefix, set_id, MANIFEST)
    if key not in store.list_keys(set_key(prefix, set_id, "")):
        raise RestoreRefused(f"set {prefix}/{set_id} has no manifest (it does not exist or is incomplete).")
    path = work_dir / MANIFEST
    store.get_file(key, path)
    return load_manifest(path.read_text(encoding="utf-8"))


def fetch_files(store: BackupStore, prefix: str, manifest: dict[str, Any], work_dir: Path) -> None:
    """Download every collection file and check its size and SHA-256 against the manifest."""
    for name, entry in manifest["collections"].items():
        path = work_dir / entry["file"]
        store.get_file(set_key(prefix, manifest["set_id"], entry["file"]), path)
        if path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise RestoreRefused(f"the file of collection {name!r} ({entry['file']}) does not match the "
                                 f"manifest's size and SHA-256; the set is damaged. Nothing was written.")


# ---------------------------------------------------------------- restoring


def read_documents(path: Path) -> Iterable[RawBSONDocument]:
    with gzip.open(path, "rb") as f:
        yield from bson.decode_file_iter(f, codec_options=RAW)


def insert_file(target: MongoDatabase, name: str, path: Path, batch_size: int = BATCH_SIZE) -> int:
    """Insert a collection file's documents in batches. An ordered insert stops at the first error and
    raises it, so the return value is the number of documents inserted."""
    coll = target[name]
    inserted, batch = 0, []
    for doc in read_documents(path):
        batch.append(doc)
        if len(batch) >= batch_size:
            coll.insert_many(batch, ordered=True, bypass_document_validation=True)
            inserted, batch = inserted + len(batch), []
    if batch:
        coll.insert_many(batch, ordered=True, bypass_document_validation=True)
        inserted += len(batch)
    return inserted


def create_collections(target: MongoDatabase, manifest: dict[str, Any]) -> None:
    """Create every collection of the set with its options from the manifest, validators among them."""
    for name, entry in manifest["collections"].items():
        target.create_collection(name, **entry.get("options", {}))


def build_indexes(target: MongoDatabase, manifest: dict[str, Any]) -> int:
    """Build the set's indexes from the manifest, one command per collection. Returns how many were built."""
    built = 0
    for name, entry in manifest["collections"].items():
        present = set(target[name].index_information())  # the _id index, made with the collection
        if specs := [spec for spec in entry.get("indexes", []) if spec["name"] not in present]:
            target.command("createIndexes", name, indexes=specs)
            built += len(specs)
    return built


def restore_counters(target: MongoDatabase) -> dict[str, int]:
    """Raise any counter below the largest integer id of its collection. Returns the counters changed.

    The collections are those whose ids come from counters: the ones database/schema.py declares, and
    any other collection a counter names (schema_migrations, numbered by migration, is not one)."""
    changed: dict[str, int] = {}
    counters = target[COUNTERS_COLLECTION]
    names = (set(VALIDATORS) | {c["_id"] for c in counters.find({}, {"_id": 1}) if isinstance(c["_id"], str)})
    names -= {COUNTERS_COLLECTION, migrate.MIGRATIONS_COLLECTION, migrate.LOCK_COLLECTION}
    for name in sorted(names & set(target.list_collection_names())):
        top = target[name].find_one({"_id": {"$type": "number"}}, {"_id": 1}, sort=[("_id", -1)])
        if not top or not isinstance(top["_id"], int):
            continue
        current = counters.find_one({"_id": name}) or {}
        if int(current.get("seq", 0)) < top["_id"]:
            counters.update_one({"_id": name}, {"$set": {"seq": top["_id"]}}, upsert=True)
            changed[name] = top["_id"]
    return changed


def count_mismatches(target: MongoDatabase, manifest: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """{collection: (manifest count, restored count)} for every collection whose counts differ."""
    out = {}
    for name, entry in manifest["collections"].items():
        restored = target[name].count_documents({})
        if restored != entry["documents"]:
            out[name] = (entry["documents"], restored)
    return out


def restore_set(target: MongoDatabase, manifest: dict[str, Any], files_dir: Path, *,
                migrations: Sequence[Migration] | None = None, batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """Restore a fetched and verified set into target, which must be empty (checked again here).
    Returns a summary; raises RestoreRefused before writing, or RestoreFailed after."""
    ordered = list(migrations) if migrations is not None else discover()
    if problem := manifest_problem(manifest, ordered):
        raise RestoreRefused(f"Restore refused; nothing was written. {problem}")
    if problem := not_empty_problem(target):
        raise RestoreRefused(f"Restore refused; nothing was written. {problem}")
    started = time.perf_counter()
    collections = manifest["collections"]
    set_migration = int(manifest.get("migration", 0))
    try:
        # 1. The collections at the set's migration: the set's own options, never this code's schema.
        create_collections(target, manifest)
        # 2. The documents, schema_migrations and counters among them (counters last, as in the set).
        restored = {}
        for name, entry in collections.items():
            restored[name] = insert_file(target, name, files_dir / entry["file"], batch_size)
            log.info("restored %s: %d documents", name, restored[name])
        # 3. Counts against the manifest, before anything else writes or removes a document.
        if mismatches := count_mismatches(target, manifest):
            detail = ", ".join(f"{n} {want} expected, {got} restored" for n, (want, got) in mismatches.items())
            raise RestoreFailed(f"counts do not match the manifest: {detail}. Drop {target.name!r} before "
                                f"trying again.")
        # 4. The set's indexes; 5. any counter behind its collection.
        indexes = build_indexes(target, manifest)
        raised = restore_counters(target)
        # 6. Migrations newer than the set, then this code's ensure_schema, as a deploy would run them.
        later = migrate.run(target, migrations=ordered)
    except RestoreFailed:
        raise
    except Exception as exc:
        raise RestoreFailed(f"the restore into {target.name!r} stopped part-way ({type(exc).__name__}: {exc}). "
                            f"Drop that database before trying again.") from exc
    return {
        "set_id": manifest["set_id"], "source_database": manifest.get("database"), "target_database": target.name,
        "documents": sum(c["documents"] for c in collections.values()), "collections": len(collections),
        "migration": set_migration, "migrations_applied_after": later, "indexes_built": indexes,
        "counters_raised": raised, "seconds": round(time.perf_counter() - started, 1),
    }


# ---------------------------------------------------------------- command line


def main(argv: Sequence[str] | None = None, ask: Ask = input) -> int:
    parser = argparse.ArgumentParser(prog="python -m database.restore", description=__doc__.split("\n")[0])
    parser.add_argument("--target-db", help="the database to restore into; it must be empty")
    parser.add_argument("--set", default="latest", help="the set id (2026-10-21T213000Z), or latest (default)")
    parser.add_argument("--emergency", action="store_true",
                        help="allow the production database name, after typing it when asked")
    parser.add_argument("--list", action="store_true", help="only list the sets in the store")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from app.core.config import settings

    def refuse(problem: str) -> int:
        print(f"Restore refused; nothing was written. {problem}", file=sys.stderr)
        return 2

    try:
        store = build_store(settings)
    except BackupNotConfigured as exc:
        return refuse(str(exc))
    prefix = settings.backup_set_prefix
    if args.list:
        print_sets(store, prefix)
        return 0
    if problem := local_only_problem(settings.environment, settings.mongodb_uri):
        return refuse(problem)
    protected = protected_names(environment=settings.environment, production_db=settings.production_mongodb_db,
                                app_db=settings.mongodb_db)
    if problem := name_problem(args.target_db, protected, emergency=args.emergency, ask=ask):
        return refuse(problem)

    from app.core.database import client

    started = time.perf_counter()
    target = client[args.target_db]
    if problem := not_empty_problem(target):
        return refuse(problem)
    set_id = latest_complete_set(store, prefix) if args.set == "latest" else args.set
    if not set_id:
        return refuse(f"no complete backup set under {prefix}/.")
    with tempfile.TemporaryDirectory(prefix="nearshop-restore-") as tmp:
        try:
            manifest = fetch_manifest(store, prefix, set_id, Path(tmp))
            if problem := environment_problem(manifest, settings.environment):
                return refuse(problem)
            fetch_files(store, prefix, manifest, Path(tmp))
            summary = restore_set(target, manifest, Path(tmp))
        except RestoreRefused as exc:
            return refuse(str(exc).removeprefix("Restore refused; nothing was written. "))
        except RestoreFailed as exc:
            print(f"Restore FAILED: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # reading the set from the store; restore_set wraps everything after writing
            log.exception("could not read the set")
            print(f"Restore stopped before writing anything: could not read set {prefix}/{set_id} "
                  f"({type(exc).__name__}).", file=sys.stderr)
            return 1
    summary["total_seconds"] = round(time.perf_counter() - started, 1)  # download included: the drill's figure
    print(json.dumps({"set": f"{prefix}/{set_id}", **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
