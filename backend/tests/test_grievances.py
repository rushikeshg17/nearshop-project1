"""Grievance intake and tracking (P3.8, plan check 6.4): the officer's details from settings, submitting signed in
or with a contact, the G ticket, the lookup by ticket plus contact, the admin queue with its 30-day due date and
25-day highlight, resolving, the 5-per-contact daily limit (plan section 8.3) and the admin-only queue."""
import re
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.routes import grievances as g
from app.core import ratelimit
from app.core.config import settings
from app.core.database import utcnow
from app.main import app
from tests.conftest import HEADERS

TICKET = re.compile(r"^G-[A-HJ-NP-Z2-9]{6}$")


def phone() -> str:
    return "9" + "".join(secrets.choice("0123456789") for _ in range(9))


def email() -> str:
    return f"grievance-{secrets.token_hex(4)}@example.com"


@pytest.fixture
def made(db):
    """Contacts the test used: their grievances (and the notifications of the test's users) go afterwards."""
    contacts: list[str] = []
    yield contacts
    db.mongo[g.COLLECTION].delete_many({"contact": {"$in": contacts}})
    db.notifications.delete_many({"kind": "grievance"})


def file(client: TestClient, contact: str, made: list[str], **fields):
    try:
        made.append(g.check_contact(contact))
    except ValueError:
        pass  # refused: nothing is stored
    body = {"category": "order", "contact": contact, "description": "The shop did not hand over my order."} | fields
    return client.post("/api/grievances", json=body, headers=HEADERS)


def test_the_officer_comes_from_settings_with_marked_placeholders_until_set(client, monkeypatch):
    for field in ("name", "designation", "email", "phone", "address"):
        monkeypatch.setattr(settings, f"grievance_officer_{field}", "")
    res = client.get("/api/grievances/officer")
    assert res.status_code == 200
    data = res.json()
    assert data["placeholder"] is True and data["resolve_days"] == 30
    assert all("placeholder" in data[k].lower() for k in ("name", "designation", "email", "phone", "address"))

    details = {"name": "A. Officer", "designation": "Grievance Officer", "email": "grievance@nearshop.example",
               "phone": "+91 80 0000 0000", "address": "1 Example Road, Ballari 583101"}
    for field, value in details.items():
        monkeypatch.setattr(settings, f"grievance_officer_{field}", value)
    data = client.get("/api/grievances/officer").json()
    assert data["placeholder"] is False
    assert {k: data[k] for k in details} == details


def test_submit_look_up_and_resolve(client, admin, db, made):
    contact = phone()
    res = file(client, f"+91 {contact[:5]} {contact[5:]}", made, category="shop_conduct", order_code=" d-7kq2mx ")
    assert res.status_code == 201, res.text
    filed = res.json()
    assert TICKET.match(filed["ticket"])
    assert filed["status"] == "acknowledged" and filed["acknowledged_at"] == filed["received_at"]
    assert "acknowledged" in filed["message"] and "30 days" in filed["message"]
    assert filed["order_code"] == "D-7KQ2MX" and filed["category"] == "shop_conduct"
    assert "contact" not in filed and "id" not in filed
    stored = db.mongo[g.COLLECTION].find_one({"ticket": filed["ticket"]})
    assert stored["contact"] == contact and stored["contact_kind"] == "phone" and stored["user_id"] is None
    assert stored["due_at"] - stored["created_at"] == timedelta(days=30)

    # The lookup needs the ticket and the same contact, in any spelling of it.
    def lookup(ticket: str, who: str):
        return client.post("/api/grievances/lookup", json={"ticket": ticket, "contact": who}, headers=HEADERS)

    seen = lookup(filed["ticket"].lower(), f"0{contact}")
    assert seen.status_code == 200 and seen.json()["status"] == "acknowledged"
    assert lookup(filed["ticket"], phone()).status_code == 404
    assert lookup("G-ZZZZZZ", contact).status_code == 404
    assert lookup("X-123", contact).status_code == 422

    # The admin works it, then resolves it with an answer.
    queue = admin.get("/api/admin/grievances").json()
    row = next(i for i in queue["items"] if i["ticket"] == filed["ticket"])
    assert row["contact"] == contact and row["due_at"] == filed["due_at"] and row["needs_attention"] is False
    assert queue["resolve_days"] == 30 and queue["warn_days"] == 25

    def act(**body):
        return admin.post(f"/api/admin/grievances/{row['id']}/status", json=body, headers=HEADERS)

    started = act(status="in_progress")
    assert started.status_code == 200 and started.json()["status"] == "in_progress"
    assert act(status="in_progress").status_code == 409
    missing = act(status="resolved")
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "resolution_required"
    done = act(status="resolved", resolution="We spoke to the shop and it has refunded you in full.")
    assert done.status_code == 200 and done.json()["status"] == "resolved"
    assert act(status="resolved", resolution="Second answer, which must not land.").status_code == 409

    seen = lookup(filed["ticket"], contact).json()
    assert seen["status"] == "resolved" and seen["resolution"].startswith("We spoke to the shop")
    assert seen["in_progress_at"] and seen["resolved_at"]
    assert filed["ticket"] not in {i["ticket"] for i in admin.get("/api/admin/grievances").json()["items"]}
    resolved = admin.get("/api/admin/grievances", params={"state": "resolved"}).json()["items"]
    assert filed["ticket"] in {i["ticket"] for i in resolved}


def test_a_signed_in_complainant_gets_the_acknowledgement_in_the_app(customer, db, made):
    contact = email()
    res = file(customer, contact.upper(), made, category="account")
    assert res.status_code == 201, res.text
    ticket = res.json()["ticket"]
    me = customer.get("/api/auth/me").json()
    stored = db.mongo[g.COLLECTION].find_one({"ticket": ticket})
    assert stored["user_id"] == me["id"] and stored["contact"] == contact and stored["contact_kind"] == "email"
    note = db.notifications.find_one({"user_id": me["id"], "kind": "grievance"}, sort=[("created_at", -1)])
    assert note is not None and ticket in note.title and note.link == f"/grievance?ticket={ticket}"
    mine = customer.get("/api/grievances/mine")
    assert mine.status_code == 200 and ticket in {x["ticket"] for x in mine.json()}
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/grievances/mine").status_code == 401


def test_the_form_checks_its_fields(client, made):
    assert file(client, "not a contact", made).status_code == 422
    assert file(client, "12345", made).status_code == 422  # not a mobile number
    assert file(client, "5876543210", made).status_code == 422  # Indian mobiles start 6 to 9
    assert file(client, phone(), made, category="pricing").status_code == 422
    assert file(client, phone(), made, description="short").status_code == 422
    assert file(client, phone(), made, order_code="not a code!").status_code == 422
    assert file(client, phone(), made, order_code="").status_code == 201  # the code is optional


def test_the_ticket_format():
    tickets = {g.grievance_doc(1, category="other", contact="9876500000", description="x" * 10)["ticket"]
               for _ in range(200)}
    assert len(tickets) > 190  # drawn at random
    assert all(TICKET.match(t) for t in tickets)
    assert all(not set(t[2:]) & set("01IO") for t in tickets)  # nothing misread at a counter
    assert re.fullmatch(g.TICKET_PATTERN, "G-7KQ2MX") and not re.fullmatch(g.TICKET_PATTERN, "G-7KQ2M0")


def test_the_due_date_and_the_25_day_highlight(admin, db, made):
    received = utcnow()
    assert g.due_date(received) == received + timedelta(days=30)
    doc = {"status": "acknowledged", "created_at": received - timedelta(days=25)}
    assert not g.needs_attention(doc, received)  # 25 days exactly: not yet
    assert g.needs_attention(doc, received + timedelta(seconds=1))
    assert not g.needs_attention({**doc, "status": "resolved"}, received + timedelta(days=1))

    # In the queue: one received 26 days ago is highlighted, one 31 days ago is also past due.
    contacts = [phone(), phone()]
    made.extend(contacts)
    coll = db.mongo[g.COLLECTION]
    for days, contact in zip((26, 31), contacts, strict=True):
        coll.insert_one(g.grievance_doc(db.next_id(g.COLLECTION), category="other", contact=contact,
                                        description="An old complaint", now=received - timedelta(days=days)))
    rows = {i["contact"]: i for i in admin.get("/api/admin/grievances").json()["items"]}
    late, overdue = rows[contacts[0]], rows[contacts[1]]
    assert late["needs_attention"] and not late["past_due"] and late["days_open"] == 26
    assert overdue["needs_attention"] and overdue["past_due"] and overdue["days_open"] == 31
    assert late["due_at"][:10] == (received + timedelta(days=4)).isoformat()[:10]


def test_the_sixth_submission_from_one_contact_in_a_day_gets_429(client, db, made, monkeypatch):
    rule = ratelimit.GRIEVANCE_CONTACT
    assert (rule.limit, rule.per, rule.key) == (5, ratelimit.DAY, ("body:contact",))
    assert ratelimit.match("POST", "/api/grievances").rules == (rule,)
    monkeypatch.setattr(settings, "rate_limit_mode", "enforce")
    db.mongo[ratelimit.COLLECTION].delete_many({})
    try:
        contact = phone()
        spellings = [contact, f"+91{contact}", f"0{contact}", f"{contact[:5]} {contact[5:]}", f"91-{contact}"]
        for spelling in spellings:  # one contact, however it is written
            assert file(client, spelling, made).status_code == 201
        limited = file(client, f"+91 {contact}", made)
        assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) > 0
        assert file(client, phone(), made).status_code == 201  # another contact is not held back
        assert db.mongo[g.COLLECTION].count_documents({"contact": contact}) == 5
    finally:
        db.mongo[ratelimit.COLLECTION].delete_many({})


def test_the_queue_is_for_admins_only(client, customer, owner_a, admin):
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/admin/grievances").status_code == 401
        assert anonymous.post("/api/admin/grievances/1/status", json={"status": "in_progress"},
                              headers=HEADERS).status_code == 401
    for who in (customer, owner_a):
        assert who.get("/api/admin/grievances").status_code == 403
        assert who.post("/api/admin/grievances/1/status", json={"status": "in_progress"},
                        headers=HEADERS).status_code == 403
    assert admin.get("/api/admin/grievances").status_code == 200
    assert admin.post("/api/admin/grievances/999999/status", json={"status": "in_progress"},
                      headers=HEADERS).status_code == 404
