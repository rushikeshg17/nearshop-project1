"""Recorded consent at registration (P3.1): the required purpose and the 18-or-older declaration are needed,
every purpose is written as an append-only consents row against the current notice version, and the
admin_tasks consent report counts users without rows and prints no email address."""
from datetime import date, datetime

import pytest

from app.api.routes.auth import record_consents
from app.core.config import settings
from app.models import Role, User
from app.schemas.requests import AGE_PURPOSE, CONSENT_PURPOSES, OPTIONAL_PURPOSES, ConsentIn
from database import admin_tasks
from tests.conftest import HEADERS

FULL = {"account_and_orders": True, "age_18_plus": True}
SHOP = {"name": "Consent Hardware", "category_slugs": ["plumbing"], "phone": "9876500002",
        "address_line": "4 Station Road", "locality": "Agrahara", "lat": 12.31, "lng": 76.65}
EMAILS = ["consent-a@example.com", "consent-b@example.com", "consent-owner@example.com", "consent-none@example.com"]


def person(email: str, consent: dict | None = None) -> dict:
    body = {"name": "Consent Person", "email": email, "password": "longenough1"}
    if consent is not None:
        body["consent"] = consent
    return body


@pytest.fixture(autouse=True)
def cleanup(db, monkeypatch):
    monkeypatch.setattr(settings, "signups_open", True)
    yield
    ids = [u["_id"] for u in db.users.find_raw({"email": {"$in": EMAILS}}, {"_id": 1})]
    ids += [u["_id"] for u in db.users.find_raw({"email": {"$regex": "^report-"}}, {"_id": 1})]
    db.shops.delete_many({"owner_id": {"$in": ids}})
    db.users.delete_many({"_id": {"$in": ids}})
    db.mongo["consents"].delete_many({"user_id": {"$in": ids}})


def rows(db, user_id: int) -> list[dict]:
    return list(db.mongo["consents"].find({"user_id": user_id}, sort=[("_id", 1)]))


@pytest.mark.parametrize("consent", [None, {}, {"age_18_plus": True}, {"account_and_orders": False, "age_18_plus": True,
                                                                       "whatsapp": True}])
def test_registration_without_consent_is_refused(client, db, consent):
    res = client.post("/api/auth/register", json=person("consent-none@example.com", consent), headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "consent_required"
    assert not db.users.exists({"email": "consent-none@example.com"})


@pytest.mark.parametrize("path", ["/api/auth/register", "/api/auth/register-shop"])
def test_registration_without_age_declaration_is_refused(client, db, path):
    owner = person("consent-none@example.com", {"account_and_orders": True})
    body = owner if path.endswith("/register") else {"owner": owner, "shop": SHOP}
    res = client.post(path, json=body, headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "age_confirmation_required"
    assert not db.users.exists({"email": "consent-none@example.com"})


def test_shop_registration_without_consent_is_refused(client, db):
    res = client.post("/api/auth/register-shop", json={"owner": person("consent-owner@example.com"), "shop": SHOP},
                      headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "consent_required"
    assert not db.users.exists({"email": "consent-owner@example.com"})
    assert not db.shops.exists({"name": "Consent Hardware"})


def test_unknown_consent_field_is_rejected(client):
    res = client.post("/api/auth/register", json=person("consent-none@example.com", {**FULL, "marketing": True}),
                      headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"


def test_customer_consent_rows_for_every_purpose(client, db, monkeypatch):
    monkeypatch.setattr(settings, "notice_version", "v7")
    res = client.post("/api/auth/register", json=person("consent-a@example.com", {**FULL, "whatsapp": True}),
                      headers=HEADERS)
    assert res.status_code == 200, res.text
    written = rows(db, res.json()["id"])
    assert sorted(r["purpose"] for r in written) == sorted(CONSENT_PURPOSES)
    assert {r["notice_version"] for r in written} == {"v7"}
    assert {r["source"] for r in written} == {"web"}
    assert len({r["at"] for r in written}) == 1
    granted = {r["purpose"]: r["granted"] for r in written}
    assert granted == {"account_and_orders": True, AGE_PURPOSE: True, "whatsapp": True,
                       "personalised_suggestions": False, "search_measurement": False}


def test_shop_consent_rows_and_optional_purposes_default_false(client, db):
    res = client.post("/api/auth/register-shop", json={"owner": person("consent-owner@example.com", FULL),
                                                        "shop": SHOP}, headers=HEADERS)
    assert res.status_code == 200, res.text
    written = rows(db, res.json()["id"])
    assert sorted(r["purpose"] for r in written) == sorted(CONSENT_PURPOSES)
    assert {r["notice_version"] for r in written} == {settings.notice_version}
    granted = {r["purpose"]: r["granted"] for r in written}
    assert all(granted[p] is False for p in OPTIONAL_PURPOSES)
    assert granted["account_and_orders"] is True and granted[AGE_PURPOSE] is True


def test_optional_purposes_default_to_false_in_the_model():
    consent = ConsentIn()
    assert all(getattr(consent, p) is False for p in CONSENT_PURPOSES)


def test_consent_rows_are_appended_never_updated(client, db):
    res = client.post("/api/auth/register", json=person("consent-b@example.com", FULL), headers=HEADERS)
    assert res.status_code == 200, res.text
    user_id = res.json()["id"]
    first = rows(db, user_id)
    record_consents(db, user_id, ConsentIn(**FULL, whatsapp=True))
    after = rows(db, user_id)
    assert len(after) == 2 * len(CONSENT_PURPOSES)
    assert after[:len(first)] == first  # the original rows are untouched
    latest = {r["purpose"]: r for r in after}  # sorted by _id: the newest row per purpose wins
    assert latest["whatsapp"]["granted"] is True
    assert [r["granted"] for r in first if r["purpose"] == "whatsapp"] == [False]


def test_consent_report_counts_missing_rows_and_prints_no_email(db):
    created = datetime(2099, 1, 2)
    complete, no_age, none, _admin = db.users.insert_many([
        User(name="Report One", email="report-1@example.com", password_hash="x", role=Role.CUSTOMER,
             created_at=created),
        User(name="Report Two", email="report-2@example.com", password_hash="x", role=Role.CUSTOMER,
             created_at=created),
        User(name="Report Three", email="report-3@example.com", password_hash="x", role=Role.OWNER,
             created_at=created),
        # Made by create-admin, never by sign-up: counted apart, not reported as missing consent.
        User(name="Report Admin", email="report-admin@example.com", password_hash="x", role=Role.ADMIN,
             created_at=created),
    ])
    record_consents(db, complete.id, ConsentIn(**FULL))
    record_consents(db, no_age.id, ConsentIn(account_and_orders=True))
    lines: list[str] = []
    assert admin_tasks.consent_report(db, date(2099, 1, 1), lines.append) == 1
    text = "\n".join(lines)
    assert "@" not in text
    assert "users created by sign-up:             3" in text
    assert "admins created (not checked):         1" in text
    assert "missing a required-purpose consent:   1" in text
    assert "missing the age declaration:          2" in text
    assert "missing either:                       2" in text
    assert f"user ids missing a row: {no_age.id}, {none.id}" in text

    lines.clear()
    assert admin_tasks.consent_report(db, date(2099, 1, 3), lines.append) == 0
    assert "users created by sign-up:             0" in "\n".join(lines)


def test_consent_report_command_line(db, monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(admin_tasks, "Database", lambda: db)
    assert admin_tasks.main(["consent-report", "--since", "2099-06-01"], out=lines.append) == 0
    assert "@" not in "\n".join(lines)
