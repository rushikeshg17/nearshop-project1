"""The environment guard: reset, the seed and the test suite refuse staging and production.

Unit tests cover the pure checks. Subprocess tests run the real seed and the real pytest start-up with
a hostile environment and show that they exit non-zero without writing (the seed) or without opening
any network connection (pytest). The only server any of these tests reaches is the local test one.
"""
import os
import secrets
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import ENVIRONMENTS, Settings, settings
from app.core.database import client as mongo_client
from database import schema
from database.seed import seed
from tests.conftest import check_test_database

BACKEND_DIR = Path(__file__).resolve().parents[1]
LOCAL_URI = "mongodb://localhost:27017/?replicaSet=rs0"
ATLAS_URI = "mongodb+srv://nearshop:not-a-real-password@cluster0.abcd1.mongodb.net/?retryWrites=true&w=majority"

ALLOWED = dict(environment="development", mongodb_uri=LOCAL_URI, db_name="nearshop",
               production_db="nearshop_prod", protected_hosts=[], confirm="nearshop")


def guard(**changes) -> str | None:
    return schema.check_reset_allowed(**{**ALLOWED, **changes})


# ---------------------------------------------------------------- the reset guard (pure)

def test_allows_development_with_a_local_uri():
    assert guard() is None
    assert guard(environment="test") is None
    assert guard(environment="demo") is None


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_refuses_production_and_staging(environment):
    problem = guard(environment=environment)
    assert problem and environment in problem


def test_refuses_a_protected_host():
    hosts = ["cluster0.abcd1.mongodb.net"]
    srv = "mongodb+srv://u:p@cluster0.abcd1.mongodb.net/nearshop?retryWrites=true"
    assert "NEARSHOP_PROTECTED_HOSTS" in guard(mongodb_uri=srv, protected_hosts=hosts)
    # a listed domain covers its hosts, and every host of a seed list is checked
    shards = "mongodb://u:p@a.example.com:27017,shard-01.abcd1.mongodb.net:27017/?replicaSet=x"
    assert guard(mongodb_uri=shards, protected_hosts=["abcd1.mongodb.net"])
    assert guard(protected_hosts=["localhost"])
    # an unrelated host whose name merely ends the same way is not protected
    assert guard(mongodb_uri="mongodb://notabcd1.mongodb.net/", protected_hosts=["abcd1.mongodb.net"]) is None


def test_refuses_the_production_database_name():
    problem = guard(db_name="nearshop_prod", confirm="nearshop_prod")
    assert problem and "production database name" in problem


@pytest.mark.parametrize("confirm", [None, ""])
def test_refuses_a_missing_confirmation(confirm):
    problem = guard(confirm=confirm)
    assert problem and "--confirm-db nearshop" in problem


def test_refuses_a_mismatched_confirmation():
    problem = guard(confirm="nearshop_dev")
    assert problem and "does not match" in problem


def test_uri_hosts():
    assert schema.uri_hosts(LOCAL_URI) == ["localhost"]
    assert schema.uri_hosts(ATLAS_URI) == ["cluster0.abcd1.mongodb.net"]
    assert schema.uri_hosts("mongodb://u:p%40ss@H1:27017,h2/db?x=1") == ["h1", "h2"]
    assert schema.uri_hosts("mongodb://[::1]:27017/") == ["::1"]


# ---------------------------------------------------------------- settings

def test_environment_must_be_a_known_value():
    for environment in ENVIRONMENTS:
        assert Settings(_env_file=None, environment=environment).environment == environment
    assert Settings(_env_file=None, environment=" Production ").environment == "production"
    with pytest.raises(ValidationError, match="NEARSHOP_ENVIRONMENT must be one of"):
        Settings(_env_file=None, environment="prod")


def test_protected_host_list():
    s = Settings(_env_file=None, protected_hosts=" Cluster0.ABCD1.mongodb.net:27017, ,staging.example.com")
    assert s.protected_host_list == ["cluster0.abcd1.mongodb.net", "staging.example.com"]


def clean_env(**values: str) -> dict[str, str]:
    """This process's environment without any NEARSHOP_ setting (conftest set some), plus `values`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEARSHOP_")}
    return {**env, **values}


def run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
                          timeout=120)


def test_an_unknown_environment_stops_the_api_at_startup():
    result = run(["-c", "import app.main"], clean_env(NEARSHOP_ENVIRONMENT="prod",
                                                       NEARSHOP_MONGODB_URI=LOCAL_URI))
    assert result.returncode != 0
    assert "NEARSHOP_ENVIRONMENT must be one of development, test, staging, production, demo" in result.stderr


# ---------------------------------------------------------------- reset and the seed

@pytest.mark.parametrize("setting, value", [
    ("environment", "production"),
    ("environment", "staging"),
    ("protected_hosts", "localhost,127.0.0.1,mongo"),
])
def test_reset_refuses_and_writes_nothing(db, monkeypatch, setting, value):
    before = {name: db.mongo[name].count_documents({}) for name in ("products", "shops", "users")}
    monkeypatch.setattr(settings, setting, value)
    with pytest.raises(schema.ResetRefused, match="nothing was written"):
        schema.reset(db.mongo, confirm=db.mongo.name)
    assert {name: db.mongo[name].count_documents({}) for name in before} == before
    assert all(before.values())


def test_reset_refuses_without_a_matching_confirmation(db):
    for confirm in (None, "nearshop"):
        with pytest.raises(schema.ResetRefused):
            schema.reset(db.mongo, confirm=confirm)
    assert db.mongo["products"].count_documents({}) > 0


@pytest.mark.parametrize("setting, value, argv", [
    ("environment", "production", ["--confirm-db", "x"]),
    ("environment", "staging", ["--confirm-db", "x"]),
    ("protected_hosts", "localhost", ["--confirm-db", "x"]),
    ("production_mongodb_db", "x", ["--confirm-db", "x"]),
    ("environment", "development", []),
    ("environment", "development", ["--confirm-db", "y"]),
])
def test_seed_stops_before_creating_a_database_handle(monkeypatch, setting, value, argv):
    def no_database(*args, **kwargs):
        pytest.fail("the seed reached the database although the environment guard refuses")

    monkeypatch.setattr(seed.settings, "demo_password", SecretStr(secrets.token_urlsafe(24)))
    monkeypatch.setattr(seed.settings, "environment", "development")
    monkeypatch.setattr(seed.settings, "mongodb_uri", LOCAL_URI)
    monkeypatch.setattr(seed.settings, "mongodb_db", "x")
    monkeypatch.setattr(seed.settings, setting, value)
    monkeypatch.setattr(seed, "Database", no_database)
    monkeypatch.setattr(seed.schema, "reset", no_database)
    with pytest.raises(SystemExit) as exc:
        seed.main(argv)
    assert exc.value.code not in (0, None)
    assert "no database was touched" in str(exc.value.code)


@pytest.mark.parametrize("extra", [
    {"NEARSHOP_ENVIRONMENT": "production"},
    {"NEARSHOP_ENVIRONMENT": "development", "NEARSHOP_PROTECTED_HOSTS": "127.0.0.1,localhost,mongo"},
])
def test_seed_command_exits_non_zero_and_writes_nothing(ids, extra):
    """The real seed command, pointed at the local test server, with a database name that does not exist."""
    name = f"nearshop_guard_{uuid.uuid4().hex[:10]}"
    env = clean_env(NEARSHOP_MONGODB_URI=os.environ["NEARSHOP_MONGODB_URI"], NEARSHOP_MONGODB_DB=name,
                    NEARSHOP_DEMO_PASSWORD=secrets.token_urlsafe(24), NEARSHOP_EMBEDDING_PROVIDER="tfidf",
                    **extra)
    result = run(["-m", "database.seed.seed", "--confirm-db", name], env)
    assert result.returncode != 0
    assert "Seed stopped; no database was touched" in result.stderr
    assert name not in mongo_client.list_database_names()


# ---------------------------------------------------------------- the test suite's own guard

def test_test_database_check():
    local = "mongodb://127.0.0.1:27017/?directConnection=true"
    assert check_test_database(local, LOCAL_URI) is None
    assert check_test_database("mongodb://mongo:27017/?replicaSet=rs0", None) is None
    assert "not set" in check_test_database(None, LOCAL_URI)
    assert "not set" in check_test_database("  ", LOCAL_URI)
    assert check_test_database(ATLAS_URI, LOCAL_URI)
    assert check_test_database("mongodb://u:p@cluster0-shard-00-00.abcd1.mongodb.net:27017/", LOCAL_URI)
    assert check_test_database("mongodb://localhost:27017,db.example.com:27017/", None)
    assert check_test_database("mongodb://localhost.example.com/", None)
    assert "equals NEARSHOP_MONGODB_URI" in check_test_database(LOCAL_URI, LOCAL_URI)


# Runs pytest with every socket call replaced by one that ends the process with code 99, so a
# connection attempt (or a DNS lookup) of any kind cannot go unnoticed.
NO_NETWORK_PYTEST = textwrap.dedent("""
    import os, socket, sys

    def no_network(*args, **kwargs):
        sys.stderr.write("NETWORK ATTEMPT\\n")
        sys.stderr.flush()
        os._exit(99)

    socket.socket.connect = socket.socket.connect_ex = no_network
    socket.getaddrinfo = socket.create_connection = no_network
    import pytest
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "tests/test_seed_safety.py"]))
""")


@pytest.mark.parametrize("test_uri, app_uri, message", [
    (None, None, "NEARSHOP_TEST_MONGODB_URI is not set"),
    (ATLAS_URI, None, "mongodb+srv is never local"),
    ("mongodb://u:p@cluster0-shard-00-00.abcd1.mongodb.net:27017/?ssl=true", None, "tests run only against"),
    ("mongodb://127.0.0.1:27017/?directConnection=true", "mongodb://127.0.0.1:27017/?directConnection=true",
     "equals NEARSHOP_MONGODB_URI"),
])
def test_pytest_stops_before_connecting(test_uri, app_uri, message):
    env = clean_env()
    if test_uri is not None:
        env["NEARSHOP_TEST_MONGODB_URI"] = test_uri
    if app_uri is not None:
        env["NEARSHOP_MONGODB_URI"] = app_uri
    result = run(["-c", NO_NETWORK_PYTEST], env)
    assert "NETWORK ATTEMPT" not in result.stderr
    assert result.returncode == 4, result.stdout + result.stderr
    assert "Tests stopped before connecting to any database" in result.stderr
    assert message in result.stderr
    assert "passed" not in result.stdout
