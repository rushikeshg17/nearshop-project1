"""The seed must refuse a missing, short or previously published demo password, before any database use.

None of these tests uses a database fixture: they exercise the pure password check and the very
start of the seed's main(), with the database handle replaced by a stub that fails the test.
"""
import secrets

import pytest
from pydantic import SecretStr

from database.seed import seed


def test_rejects_missing_password():
    assert seed.check_demo_password(None)
    assert seed.check_demo_password("")


def test_rejects_15_character_password():
    assert seed.check_demo_password("a" * 15)
    assert seed.check_demo_password(secrets.token_hex(8)[:15])


def test_rejects_old_public_default():
    problem = seed.check_demo_password("nearshop123")
    assert problem and "old public default" in problem


def test_accepts_random_24_character_password():
    password = secrets.token_urlsafe(18)
    assert len(password) == 24
    assert seed.check_demo_password(password) is None


@pytest.mark.parametrize("password", ["", "short-password", "nearshop123"])
def test_seed_exits_before_creating_a_database_handle(monkeypatch, password):
    def no_database(*args, **kwargs):
        pytest.fail("the seed reached the database before checking the demo password")

    monkeypatch.setattr(seed.settings, "demo_password", SecretStr(password))
    monkeypatch.setattr(seed, "Database", no_database)
    monkeypatch.setattr(seed.schema, "reset", no_database)
    with pytest.raises(SystemExit) as exc:
        seed.main()
    assert exc.value.code not in (0, None)
