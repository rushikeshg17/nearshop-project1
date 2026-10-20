"""Numbered schema migrations, applied once each, in order, before new code starts.

    uv run python -m database.migrate            # apply pending migrations, then ensure_schema
    uv run python -m database.migrate --status   # read-only: applied, expected, pending

On Render the runner is each API service's preDeployCommand (render.yaml), so a release's migrations
finish before its code serves traffic; a failed migration fails the deploy and the previous version
keeps serving. Locally, make migrate runs it against the local database.

Writing a migration
-------------------
Add a module mNNNN_<name>.py here (four digits, the next number; numbers are contiguous from 1)
that defines:

  * NUMBER: the same number as the file name;
  * NAME: a short snake_case name;
  * apply(mongo): the change, given the pymongo Database. It must be idempotent: a deploy that dies
    after apply but before the record is written runs it again, and a fresh database (reset, a new
    environment) runs every migration on top of the current schema.

Applied migrations are recorded in the schema_migrations collection (_id and number, name,
applied_at, duration in seconds). After the pending migrations, the runner calls
database.schema.ensure_schema, which creates the collections, validators and indexes the code
declares. ensure_schema only adds: changing or removing anything already in the database is a
migration's job. Declare the end state in database/schema.py in the same change as the migration.

Expand, then contract
---------------------
The pre-deploy step runs while the previous release still serves traffic, and a rollback puts the
previous release back on the migrated database. So a release may only add: optional fields, indexes
(under new names) and enum values. Anything that removes or narrows (dropping a field, an index or an
enum value, or making a field required) ships at least one release later, once no running version
needs the old shape. A field the code starts to require is first added as optional, backfilled, and
made required in a later release.

Helpers
-------
  * replace_index(mongo, collection, old_name, new_index): change an index's keys or options. It
    builds the replacement under a new name, and drops the old named index only once the new one is
    built, so a unique rule (such as uq_email on users) is enforced at every moment. MongoDB lets two
    indexes on the same keys coexist only when they differ in collation, partial filter or (on
    current servers) uniqueness; otherwise give the replacement different keys.
  * add_enum_values(mongo, collection, path, values): add values to an enum in the collection's
    validator, in place, before the code that writes them starts. path is the dotted field path
    ("status", or "items.[].kind" through an array's items).
  * backfill(mongo, collection, filter, update, batch_size=...): update the documents matching filter
    in batches of _id order, each batch a separate update_many, so a large collection is never locked
    in one long write. filter must exclude documents already done, which keeps it idempotent.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pymongo import IndexModel
from pymongo.database import Database as MongoDatabase

log = logging.getLogger(__name__)

MODULE_NAME = re.compile(r"^m(\d{4})_([a-z0-9_]+)$")


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    apply: Callable[[MongoDatabase], None]


def discover() -> list[Migration]:
    """The migrations in this package, in order. Raises ValueError on a misnamed or misnumbered module."""
    migrations = []
    for info in pkgutil.iter_modules(__path__):
        match = MODULE_NAME.match(info.name)
        if not match:
            raise ValueError(f"database/migrations/{info.name}.py is not named mNNNN_<name>.py")
        module = importlib.import_module(f"{__name__}.{info.name}")
        number, name, apply = (getattr(module, attr, None) for attr in ("NUMBER", "NAME", "apply"))
        if number != int(match.group(1)) or not isinstance(name, str) or not callable(apply):
            raise ValueError(f"{info.name}.py must define NUMBER = {int(match.group(1))}, NAME and apply(mongo)")
        migrations.append(Migration(number, name, apply))
    return check_sequence(migrations)


def check_sequence(migrations: Sequence[Migration]) -> list[Migration]:
    """Sort migrations by number and check the numbers run 1, 2, 3, ... with no gap or repeat."""
    ordered = sorted(migrations, key=lambda m: m.number)
    numbers = [m.number for m in ordered]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"migration numbers must run from 1 with no gaps or repeats; found {numbers}")
    return ordered


def latest_number() -> int:
    """The migration number this code expects the database to have reached."""
    return discover()[-1].number


# ---------------------------------------------------------------- helpers for migrations


def replace_index(mongo: MongoDatabase, collection: str, old_name: str, new_index: IndexModel) -> None:
    """Build new_index (which must have a new name), then drop the index named old_name.

    Idempotent: an existing identical replacement is not rebuilt, and an old index already gone is
    not an error. If the replacement cannot be built, the old index stays and the error propagates."""
    new_name = new_index.document["name"]
    if new_name == old_name:
        raise ValueError(f"the replacement for index {old_name!r} on {collection!r} needs a new name")
    coll = mongo[collection]
    coll.create_indexes([new_index])  # returns once the build has finished
    if new_name not in coll.index_information():
        raise RuntimeError(f"index {new_name!r} on {collection!r} was not built; {old_name!r} is kept")
    if old_name in coll.index_information():
        coll.drop_index(old_name)
        log.info("replaced index %s.%s with %s", collection, old_name, new_name)


def add_enum_values(mongo: MongoDatabase, collection: str, path: str, values: Sequence[Any]) -> list[Any]:
    """Add values to the enum at path in the collection's $jsonSchema validator. Returns the values
    added (none when all were present already)."""
    info = next(iter(mongo.list_collections(filter={"name": collection})), None)
    validator = (info or {}).get("options", {}).get("validator")
    if not validator or "$jsonSchema" not in validator:
        raise ValueError(f"collection {collection!r} has no $jsonSchema validator")
    node: Mapping[str, Any] = validator["$jsonSchema"]
    for part in path.split("."):
        try:
            node = node["items"] if part == "[]" else node["properties"][part]
        except KeyError:
            raise ValueError(f"the validator of {collection!r} has no field {path!r}") from None
    if "enum" not in node:
        raise ValueError(f"field {path!r} of {collection!r} is not an enum in its validator")
    missing = [v for v in values if v not in node["enum"]]
    if missing:
        node["enum"].extend(missing)  # node is part of validator, which is written back whole
        mongo.command("collMod", collection, validator=validator)
        log.info("added %s to %s.%s", missing, collection, path)
    return missing


def backfill(mongo: MongoDatabase, collection: str, filter: Mapping[str, Any], update: Mapping[str, Any] | list,
             *, batch_size: int = 500) -> int:
    """Apply update to every document matching filter, batch_size documents at a time in _id order.
    Returns the number of documents modified. Each document is visited at most once, so it always
    ends; filter should exclude documents already updated, so a rerun does no work."""
    coll = mongo[collection]
    modified, last_id = 0, None
    while True:
        page = filter if last_id is None else {"$and": [filter, {"_id": {"$gt": last_id}}]}
        ids = [d["_id"] for d in coll.find(page, {"_id": 1}).sort("_id", 1).limit(batch_size)]
        if not ids:
            return modified
        modified += coll.update_many({"$and": [filter, {"_id": {"$in": ids}}]}, update).modified_count
        last_id = ids[-1]
        log.info("backfill %s: %d documents updated so far", collection, modified)
