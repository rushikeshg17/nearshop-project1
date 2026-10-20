"""Schema migrations: the baseline, the runner's lock and records, safe index changes and the startup check.

Each test gets its own empty database on the local test server (dropped afterwards), so the runner
starts from nothing, as on a new environment, or from the pre-migration state that ensure_schema
alone used to leave.
"""
import logging
import os
import uuid
from datetime import datetime, timedelta

import pymongo.collection
import pytest
from pymongo import ASCENDING, IndexModel, MongoClient, monitoring
from pymongo.errors import DuplicateKeyError

from app import main
from app.core.database import client as mongo_client
from database import migrate, schema
from database.migrations import (
    Migration,
    add_enum_values,
    backfill,
    check_sequence,
    discover,
    m0001_baseline,
    replace_index,
)

BASELINE = Migration(m0001_baseline.NUMBER, m0001_baseline.NAME, m0001_baseline.apply)
ALL = [m.number for m in discover()]  # every migration this code has: a new database gets them all
CI_EMAIL = IndexModel([("email", ASCENDING)], unique=True, name="uq_email_ci",
                      collation={"locale": "en", "strength": 2})  # one account per email, ignoring case


@pytest.fixture
def scratch():
    name = f"{os.environ['NEARSHOP_MONGODB_DB']}_m{uuid.uuid4().hex[:8]}"
    yield mongo_client[name]
    mongo_client.drop_database(name)


class Commands(monitoring.CommandListener):
    def __init__(self):
        self.events = []

    def started(self, event):
        self.events.append(("started", event.command_name, event.command))

    def succeeded(self, event):
        self.events.append(("succeeded", event.command_name, event.reply))

    def failed(self, event):
        pass

    def names(self):
        return [name for kind, name, _ in self.events if kind == "started"]


@pytest.fixture
def listened(scratch):
    """The scratch database through a second client that records every command it sends."""
    commands = Commands()
    other = MongoClient(os.environ["NEARSHOP_MONGODB_URI"], event_listeners=[commands])
    yield other[scratch.name], commands
    other.close()


def index_names(mongo, collection):
    return set(mongo[collection].index_information())


def all_indexes(mongo):
    return {name: mongo[name].index_information() for name in schema.INDEXES}


def has_unique_email(mongo) -> bool:
    return any(info.get("unique") and info["key"] == [("email", 1)]
               for info in mongo["users"].index_information().values())


def user_doc(_id, email):
    now = datetime(2026, 10, 20)
    return {"_id": _id, "name": "U", "email": email, "password_hash": "x", "role": "customer", "is_active": True,
            "created_at": now, "updated_at": now}


# ---------------------------------------------------------------- baseline and records


def test_baseline_on_an_empty_database_creates_every_collection_and_index(scratch):
    assert migrate.run(scratch) == ALL

    collections = set(scratch.list_collection_names())
    assert set(schema.VALIDATORS) <= collections
    for name, models in schema.INDEXES.items():
        assert {m.document["name"] for m in models} <= index_names(scratch, name), name
    users = next(iter(scratch.list_collections(filter={"name": "users"})))
    assert users["options"]["validator"] == schema.VALIDATORS["users"]
    record = scratch[migrate.MIGRATIONS_COLLECTION].find_one({"_id": 1})
    assert record["number"] == 1 and record["name"] == "baseline"
    assert isinstance(record["applied_at"], datetime) and record["duration"] >= 0
    assert scratch[migrate.LOCK_COLLECTION].count_documents({}) == 0  # released


def test_running_twice_does_nothing_the_second_time(scratch, listened):
    migrate.run(scratch)
    records = list(scratch[migrate.MIGRATIONS_COLLECTION].find())
    indexes = all_indexes(scratch)

    mongo, commands = listened
    assert migrate.run(mongo) == []
    assert list(scratch[migrate.MIGRATIONS_COLLECTION].find()) == records
    assert all_indexes(scratch) == indexes
    assert "dropIndexes" not in commands.names() and "drop" not in commands.names()
    built = [reply for kind, name, reply in commands.events if kind == "succeeded" and name == "createIndexes"]
    assert built and all(r["numIndexesBefore"] == r["numIndexesAfter"] for r in built)  # every index existed


def test_baseline_on_todays_database_records_itself_without_changes(scratch, listened):
    schema.ensure_schema(scratch)  # what every API start did before migrations existed
    indexes = all_indexes(scratch)

    mongo, commands = listened
    m0001_baseline.apply(mongo)
    assert commands.names() == ["listCollections"]  # it only looked

    assert migrate.run(scratch) == ALL
    assert all_indexes(scratch) == indexes
    assert migrate.check_version(scratch).pending == []


# ---------------------------------------------------------------- index changes never drop a rule


def test_index_option_change_builds_the_new_index_before_dropping_the_old(scratch, monkeypatch):
    migrate.run(scratch)
    scratch["users"].insert_one(user_doc(1, "a@example.com"))
    steps = []

    def watch(method):
        original = getattr(pymongo.collection.Collection, method)

        def wrapper(self, *args, **kwargs):
            assert has_unique_email(scratch), f"no unique email index before {method}"
            result = original(self, *args, **kwargs)
            assert has_unique_email(scratch), f"no unique email index after {method}"
            if self.name == "users":
                steps.append((method, args[0] if method == "drop_index" else [m.document["name"] for m in args[0]]))
            return result

        monkeypatch.setattr(pymongo.collection.Collection, method, wrapper)

    watch("create_indexes")
    watch("drop_index")
    # The code now declares the case-insensitive index in place of uq_email, and ships this migration.
    users = [m for m in schema.INDEXES["users"] if m.document["name"] != "uq_email"]
    monkeypatch.setitem(schema.INDEXES, "users", [CI_EMAIL, *users])
    second = Migration(ALL[-1] + 1, "case_insensitive_email",
                       lambda mongo: replace_index(mongo, "users", "uq_email", CI_EMAIL))

    assert migrate.run(scratch, migrations=[*discover(), second]) == [second.number]

    created = steps.index(("create_indexes", ["uq_email_ci"]))
    assert steps.index(("drop_index", "uq_email")) > created
    assert {"uq_email_ci"} <= index_names(scratch, "users") and "uq_email" not in index_names(scratch, "users")
    with pytest.raises(DuplicateKeyError):
        scratch["users"].insert_one(user_doc(2, "A@Example.COM"))

    steps.clear()
    assert migrate.run(scratch, migrations=[*discover(), second]) == []
    assert not [s for s in steps if s[0] == "drop_index"]


def test_ensure_schema_conflict_names_the_index_and_drops_nothing(scratch, monkeypatch):
    migrate.run(scratch)
    before = all_indexes(scratch)
    changed = IndexModel([("email", ASCENDING)], unique=True, name="uq_email", collation={"locale": "en", "strength": 2})
    users = [changed if m.document["name"] == "uq_email" else m for m in schema.INDEXES["users"]]
    monkeypatch.setitem(schema.INDEXES, "users", users)

    with pytest.raises(schema.SchemaConflict, match=r"'uq_email' on collection 'users'"):
        schema.ensure_schema(scratch)
    assert all_indexes(scratch) == before
    assert has_unique_email(scratch)


# ---------------------------------------------------------------- the lock


def test_a_second_runner_is_refused_while_the_lock_is_held(scratch):
    migrate.acquire_lock(scratch, "first-runner")
    with pytest.raises(migrate.MigrationLocked, match="first-runner"):
        migrate.run(scratch)
    assert scratch[migrate.MIGRATIONS_COLLECTION].count_documents({}) == 0
    assert set(scratch.list_collection_names()) == {migrate.LOCK_COLLECTION}  # nothing else was created
    assert scratch[migrate.LOCK_COLLECTION].find_one()["owner"] == "first-runner"

    migrate.release_lock(scratch, "first-runner")
    assert migrate.run(scratch) == ALL


def test_an_expired_lock_is_taken_over(scratch):
    past = datetime(2026, 1, 1)
    scratch[migrate.LOCK_COLLECTION].insert_one({"_id": migrate.LOCK_ID, "owner": "crashed", "acquired_at": past,
                                                 "expires_at": past + timedelta(hours=1)})
    assert migrate.run(scratch) == ALL
    assert scratch[migrate.LOCK_COLLECTION].count_documents({}) == 0


def test_release_lock_clears_a_killed_runners_lock_from_the_command(scratch, monkeypatch, capsys):
    from app.core.config import settings

    monkeypatch.setattr(settings, "mongodb_db", scratch.name)
    migrate.acquire_lock(scratch, "killed-runner")  # unexpired: run() would be refused for an hour
    assert migrate.main(["--release-lock"]) == 0
    assert "killed-runner" in capsys.readouterr().out
    assert scratch[migrate.LOCK_COLLECTION].count_documents({}) == 0
    assert migrate.applied_numbers(scratch) == set()  # it applied nothing
    assert migrate.main(["--release-lock"]) == 0
    assert "no migration lock" in capsys.readouterr().out
    assert migrate.main([]) == 0 and migrate.applied_numbers(scratch) == set(ALL)


def test_release_lock_is_refused_for_a_hosted_database_in_development(monkeypatch, capsys):
    from app.core.config import settings

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "mongodb_uri", "mongodb+srv://cluster0.abcd1.mongodb.net/")
    monkeypatch.setattr(migrate, "force_release_lock", lambda _m: pytest.fail("lock released on a hosted database"))
    assert migrate.main(["--release-lock"]) == 2
    assert "Lock release refused" in capsys.readouterr().err


def test_the_schema_command_is_the_migration_runner():
    source = open(schema.__file__).read().split('if __name__ == "__main__":', 1)[1]
    assert "migrate.main()" in source and "ensure_schema(" not in source


def test_a_failed_migration_is_not_recorded_and_the_lock_is_released(scratch):
    def broken(_mongo):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        migrate.run(scratch, migrations=[BASELINE, Migration(2, "broken", broken)])
    assert migrate.applied_numbers(scratch) == {1}
    assert scratch[migrate.LOCK_COLLECTION].count_documents({}) == 0


def test_a_database_ahead_of_the_code_is_left_alone(scratch, monkeypatch):
    migrate.run(scratch)
    newer = ALL[-1] + 1
    scratch[migrate.MIGRATIONS_COLLECTION].insert_one({"_id": newer, "number": newer, "name": "newer",
                                                       "applied_at": datetime(2026, 10, 20), "duration": 0.1})
    monkeypatch.setattr(migrate, "ensure_schema", lambda _m: pytest.fail("validators of a newer release rewritten"))
    assert migrate.run(scratch, migrations=[BASELINE]) == []
    assert migrate.check_version(scratch, [BASELINE]).ahead


# ---------------------------------------------------------------- the startup check


def test_the_startup_check_reports_a_database_behind_the_code(scratch, caplog):
    with caplog.at_level(logging.INFO, logger="nearshop"):
        main.check_schema(scratch)  # nothing applied yet
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "behind" in errors[0].getMessage()
    assert main.schema_status["behind"] is True and main.schema_status["expected"] == discover()[-1].number

    migrate.run(scratch)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="nearshop"):
        main.check_schema(scratch)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert main.schema_status["behind"] is False


def test_the_api_no_longer_runs_ensure_schema_at_startup():
    source = open(main.__file__).read()
    assert "ensure_schema" not in source


# ---------------------------------------------------------------- helpers and discovery


def test_add_enum_values_extends_the_validator_once(scratch):
    migrate.run(scratch)
    assert add_enum_values(scratch, "orders", "payment_status", ["partially_refunded"]) == ["partially_refunded"]
    assert add_enum_values(scratch, "orders", "payment_status", ["paid", "partially_refunded"]) == []
    options = next(iter(scratch.list_collections(filter={"name": "orders"})))["options"]
    enum = options["validator"]["$jsonSchema"]["properties"]["payment_status"]["enum"]
    assert enum == ["unpaid", "paid", "refunded", "partially_refunded"]
    assert options["validationLevel"] == "strict"
    with pytest.raises(ValueError, match="no field"):
        add_enum_values(scratch, "orders", "no_such_field", ["x"])


def test_backfill_updates_in_batches_and_reruns_as_a_no_op(scratch):
    scratch["plain"].insert_many([{"_id": i} for i in range(1, 1201)])
    todo = {"flag": {"$exists": False}}
    assert backfill(scratch, "plain", todo, {"$set": {"flag": True}}, batch_size=500) == 1200
    assert scratch["plain"].count_documents(todo) == 0
    assert backfill(scratch, "plain", todo, {"$set": {"flag": True}}, batch_size=500) == 0


def test_replace_index_refuses_the_same_name(scratch):
    migrate.run(scratch)
    with pytest.raises(ValueError, match="new name"):
        replace_index(scratch, "users", "uq_email", IndexModel([("email", ASCENDING)], unique=True, name="uq_email"))
    assert "uq_email" in index_names(scratch, "users")


def test_the_package_holds_a_contiguous_sequence_starting_with_the_baseline():
    migrations = discover()
    assert migrations[0].number == 1 and migrations[0].name == "baseline"
    assert [m.number for m in migrations] == list(range(1, len(migrations) + 1))
    with pytest.raises(ValueError, match="no gaps"):
        check_sequence([BASELINE, Migration(3, "skipped_two", lambda _m: None)])


def test_the_command_migrates_only_a_local_database_in_development():
    assert migrate.remote_problem("development", ["cluster0.abcd1.mongodb.net"])
    assert migrate.remote_problem("test", ["localhost", "db.example.com"])
    assert migrate.remote_problem("development", ["localhost"]) is None
    assert migrate.remote_problem("production", ["cluster0.abcd1.mongodb.net"]) is None
