"""Staff logins (P9.13, app/services/staff.py; plan section 8.18): the invitation and the first phone sign-in, each
route group as the owner, as staff and as another shop's staff, the login limit, suspension on a lapse and
reactivation on renewal, removal, the refusal of a phone that already has an account, and actor_role staff in status
events and the validators.

Codes go through the console provider and the invitation through the fake WhatsApp client: nothing leaves the
machine."""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import WriteError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.main import app
from app.models import Reservation, ReservationStatus, Role, Shop, User
from app.models.enums import StaffStatus
from app.services import plans, staff, whatsapp
from app.services.messaging import ConsoleProvider
from app.utils.text import short_code
from tests.conftest import HEADERS, PASSWORD, login
from tests.test_plans import ADMIN, REASON, World, grant

pytestmark = pytest.mark.usefixtures("clean")


def new_phone() -> str:
    return f"6{uuid.uuid4().int % 10 ** 9:09d}"


@pytest.fixture
def clean(ids, db, monkeypatch):
    """WhatsApp on (the fake client), sign-ups closed (staff sign in by invitation, not sign-up), and the staff rows,
    staff accounts and subscriptions a test made removed afterwards."""
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    monkeypatch.setattr(settings, "signups_open", False)
    whatsapp.FAKE.reset()
    ConsoleProvider.outbox.clear()
    before = {d["_id"] for d in db.users.find_raw({}, {"_id": 1})}
    yield
    made = [d["_id"] for d in db.users.find_raw({}, {"_id": 1}) if d["_id"] not in before]
    shops = [d["_id"] for d in db.shops.find_raw({"owner_id": {"$in": made}}, {"_id": 1})]
    db.mongo[staff.STAFF].delete_many({"$or": [{"user_id": {"$in": made}}, {"shop_id": {"$in": shops}},
                                               {"name": {"$regex": "^Staff "}}]})
    db.mongo[plans.SUBSCRIPTIONS].delete_many({"shop_ids": {"$in": shops}})
    db.users.delete_many({"_id": {"$in": made}, "role": Role.STAFF.value})
    whatsapp.FAKE.reset()


@pytest.fixture
def world(db):
    w = World(db)
    yield w
    db.mongo[plans.SUBSCRIPTIONS].delete_many({"shop_ids": {"$in": w.shops}})
    db.mongo[staff.STAFF].delete_many({"shop_id": {"$in": w.shops}})
    w.cleanup()


def new_client() -> TestClient:
    return TestClient(app)


def as_owner(world: World, shop: Shop) -> TestClient:
    owner = world.db.users.get(shop.owner_id)
    return login(new_client(), owner.email)


def paid(db: Database, shop: Shop):
    """Growth entitlements (3 logins: the owner and two staff), from 10 days ago."""
    return grant(db, shop, utcnow() - timedelta(days=10), source="sponsored", plan_code="partner_seat")


def invite(owner: TestClient, phone: str, name: str = "Staff Ravi", agreed: bool = True):
    return owner.post("/api/owner/staff", json={"name": name, "phone": phone, "agreed": agreed}, headers=HEADERS)


def sign_in(phone: str) -> TestClient:
    """The invitee signs in with a code sent to their phone, as any phone sign-in."""
    c = new_client()
    res = c.post("/api/auth/otp/request", json={"phone": phone, "purpose": "login"}, headers=HEADERS)
    assert res.status_code == 200, res.text
    res = c.post("/api/auth/otp/verify", json={"phone": phone, "purpose": "login",
                                               "code": ConsoleProvider.last_code(phone)}, headers=HEADERS)
    assert res.status_code == 200, res.text
    return c


def staff_member(world: World, owner: TestClient, name: str = "Staff Ravi") -> tuple[TestClient, str]:
    phone = new_phone()
    assert invite(owner, phone, name).status_code == 200
    return sign_in(phone), phone


def requested(db: Database, shop: Shop, product_id: int) -> Reservation:
    cust = db.users.find_one({"email": "cust@example.com"})
    now = utcnow()
    return db.reservations.insert(Reservation(
        code=short_code("R"), customer_id=cust.id, shop_id=shop.id, product_id=product_id, quantity=1, unit_price=100,
        status=ReservationStatus.REQUESTED, expires_at=now + timedelta(minutes=30), created_at=now, updated_at=now))


# ---------------------------------------------------------------- the validators


def test_the_users_and_status_events_validators_accept_staff(db):
    user = db.users.insert(User(name="Staff Validator", phone=new_phone(), phone_verified_at=utcnow(),
                                password_hash="", role=Role.STAFF))
    try:
        db.mongo["status_events"].insert_one({"_id": db.next_id("status_events"), "entity": "reservation",
                                              "entity_id": 1, "shop_id": 1, "from_status": "REQUESTED",
                                              "to_status": "CONFIRMED", "actor_role": "staff", "actor_id": user.id,
                                              "note": None, "created_at": utcnow()})
        with pytest.raises(WriteError):
            db.mongo["status_events"].insert_one({"_id": db.next_id("status_events"), "entity": "reservation",
                                                  "entity_id": 1, "shop_id": 1, "to_status": "CONFIRMED",
                                                  "actor_role": "cashier", "created_at": utcnow()})
        with pytest.raises(WriteError):
            db.mongo["users"].insert_one({"_id": db.next_id("users"), "name": "Nobody", "password_hash": "",
                                          "role": "cashier", "is_active": True, "created_at": utcnow(),
                                          "updated_at": utcnow()})
    finally:
        db.mongo["status_events"].delete_many({"actor_id": user.id})
        db.users.delete_many({"_id": user.id})


# ---------------------------------------------------------------- invitation, sign-in and the route groups


def test_an_invited_phone_signs_in_as_staff_and_works_the_queues_but_not_the_owner_areas(db, world):
    shop = world.shop(1)
    paid(db, shop)
    product = world.product(shop, "Stafftest Brass Tap")
    owner = as_owner(world, shop)

    phone = new_phone()
    res = invite(owner, phone, "Staff Ravi Kumar")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [s["status"] for s in body["staff"]] == ["invited"]
    assert body["logins"] == {"limit": 3, "used": 2, "available": 1}
    assert body["plan"]["code"] == "partner_seat"
    sent = [m for m in whatsapp.FAKE.sent if m["template"] == staff.TEMPLATE]
    assert len(sent) == 1 and sent[0]["phone"] == phone
    texts = [p["text"] for p in sent[0]["components"][0]["parameters"]]
    assert texts[0] == "Plan" and texts[1] == shop.name and texts[2].endswith("/login")
    assert body["staff"][0]["invitation"] == "sent"

    member = sign_in(phone)
    me = member.get("/api/auth/me").json()
    assert me["role"] == "staff" and me["name"] == "Staff Ravi Kumar"
    assert me["shop"] == {"id": shop.id, "slug": shop.slug, "name": shop.name, "staff_status": "active"}
    row = staff.rows(db).find_one({"phone": phone})
    assert row.status == StaffStatus.ACTIVE and row.user_id == me["id"]

    # Daily work: the queues and their actions, stock, the catalogue and imports.
    r = requested(db, shop, product.id)
    for path in ("/api/owner/overview", "/api/owner/reservations", "/api/owner/orders", "/api/owner/products",
                 "/api/owner/catalog", "/api/owner/import/template", f"/api/owner/reservations/{r.id}"):
        assert member.get(path).status_code == 200, path
    res = member.post(f"/api/owner/reservations/{r.id}/confirm", json={}, headers=HEADERS)
    assert res.status_code == 200, res.text
    event = res.json()["timeline"][-1]
    assert event["actor_role"] == "staff" and event["actor_name"] == "Staff Ravi Kumar"
    stored = db.status_events.find_one({"entity": "reservation", "entity_id": r.id, "to_status": "CONFIRMED"})
    assert stored.actor_role == "staff" and stored.actor_id == me["id"]
    res = member.put(f"/api/owner/products/{product.id}/stock", json={"quantity": 7}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["quantity"] == 7

    # The owner sees the staff member's name in the timeline.
    detail = owner.get(f"/api/owner/reservations/{r.id}").json()
    assert detail["timeline"][-1]["actor_name"] == "Staff Ravi Kumar"

    # Owner only: settings and the UPI ID, KYC, the plan and billing, staff, insights and reviews.
    for method, path, kw in (
        ("GET", "/api/owner/shop", {}), ("PATCH", "/api/owner/shop", {"json": {"tagline": "x"}}),
        ("GET", "/api/owner/verification", {}), ("POST", "/api/owner/verification", {}),
        ("GET", "/api/owner/plan", {}), ("GET", "/api/owner/billing", {}),
        ("POST", "/api/owner/billing/requests", {"json": {"plan_code": "growth", "period": "year"}}),
        ("PUT", "/api/owner/billing/account", {"json": {}}), ("GET", "/api/owner/staff", {}),
        ("POST", "/api/owner/staff", {"json": {"name": "Staff Two", "phone": new_phone(), "agreed": True}}),
        ("DELETE", f"/api/owner/staff/{row.id}", {}), ("GET", "/api/owner/insights", {}),
        ("GET", "/api/owner/reviews", {}),
    ):
        res = member.request(method, path, headers=HEADERS, **kw)
        assert res.status_code == 403, (method, path, res.status_code)

    # The owner keeps every route.
    for path in ("/api/owner/shop", "/api/owner/plan", "/api/owner/billing", "/api/owner/staff",
                 "/api/owner/reservations"):
        assert owner.get(path).status_code == 200, path


def test_another_shops_staff_cannot_reach_this_shop(db, world):
    mine, theirs = world.shop(1), world.shop(2)
    paid(db, mine)
    paid(db, theirs)
    product = world.product(mine, "Stafftest Gate Valve")
    r = requested(db, mine, product.id)
    other, _ = staff_member(world, as_owner(world, theirs), "Staff Other")

    assert other.get(f"/api/owner/reservations/{r.id}").status_code == 404
    assert other.post(f"/api/owner/reservations/{r.id}/confirm", json={}, headers=HEADERS).status_code == 404
    assert other.put(f"/api/owner/products/{product.id}/stock", json={"quantity": 1},
                     headers=HEADERS).status_code == 404
    assert r.id not in [x["id"] for x in other.get("/api/owner/reservations").json()]
    assert db.reservations.get(r.id).status == ReservationStatus.REQUESTED
    # Nor through the service directly: a staff member of another shop is no actor of this reservation.
    from app.core.errors import Forbidden
    from app.services import reservations as reservation_service
    outsider = db.users.find_one({"name": "Staff Other"})
    with pytest.raises(Forbidden):
        reservation_service.transition(db, r.id, "confirm", outsider)


def test_customers_and_signed_out_visitors_get_no_staff_access(world, customer):
    shop = world.shop(1)
    assert customer.get("/api/owner/reservations").status_code == 403
    assert new_client().get("/api/owner/reservations").status_code == 401
    assert customer.get("/api/owner/staff").status_code == 403
    assert shop  # the shop exists; customers still cannot work it


# ---------------------------------------------------------------- refusals


def test_a_phone_that_already_has_an_account_is_refused(world):
    shop = world.shop(1)
    paid(world.db, shop)
    owner = as_owner(world, shop)
    customer_phone = world.customer(9, new_phone()).phone  # a customer account's number
    res = invite(owner, customer_phone)
    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_registered"
    owner_phone = world.owner(8, phone=new_phone()).phone
    res = invite(owner, owner_phone)
    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_registered"
    assert invite(owner, new_phone(), agreed=False).status_code == 422
    assert invite(owner, "12345").status_code == 422  # not an Indian mobile number


def test_a_phone_on_a_shops_staff_cannot_be_invited_again(world):
    a, b = world.shop(1), world.shop(2)
    paid(world.db, a)
    paid(world.db, b)
    phone = new_phone()
    assert invite(as_owner(world, a), phone).status_code == 200
    res = invite(as_owner(world, b), phone)
    assert res.status_code == 409 and res.json()["error"]["code"] == "already_staff"


def test_without_an_invitation_a_phone_sign_in_finds_no_account(world):
    phone = new_phone()
    c = new_client()
    c.post("/api/auth/otp/request", json={"phone": phone, "purpose": "login"}, headers=HEADERS)
    res = c.post("/api/auth/otp/verify", json={"phone": phone, "purpose": "login",
                                               "code": ConsoleProvider.last_code(phone)}, headers=HEADERS)
    assert res.status_code == 404 and res.json()["error"]["code"] == "no_account"


# ---------------------------------------------------------------- the login limit, lapse and renewal


def test_the_login_limit_counts_the_owner(db, world):
    free = world.shop(1)
    res = invite(as_owner(world, free), new_phone())
    assert res.status_code == 409 and res.json()["error"]["code"] == "plan_limit"  # Free: the owner's one login

    growth = world.shop(2)
    paid(db, growth)
    owner = as_owner(world, growth)
    assert invite(owner, new_phone(), "Staff One").status_code == 200
    assert invite(owner, new_phone(), "Staff Two").status_code == 200
    res = invite(owner, new_phone(), "Staff Three")
    assert res.status_code == 409 and res.json()["error"]["code"] == "plan_limit"
    body = owner.get("/api/owner/staff").json()
    assert body["logins"] == {"limit": 3, "used": 3, "available": 0}

    # Withdrawing an invitation frees its seat.
    first = body["staff"][0]["id"]
    assert owner.delete(f"/api/owner/staff/{first}", headers=HEADERS).status_code == 200
    assert invite(owner, new_phone(), "Staff Three").status_code == 200


def test_a_lapse_suspends_staff_and_ends_their_sessions_and_renewal_reactivates_them(db, world):
    shop = world.shop(1)
    sub = paid(db, shop)
    owner = as_owner(world, shop)
    first, first_phone = staff_member(world, owner, "Staff First")
    second, second_phone = staff_member(world, owner, "Staff Second")
    assert first.get("/api/owner/reservations").status_code == 200
    assert second.get("/api/owner/reservations").status_code == 200

    plans.run_jobs(db, plans.subscriptions(db).get(sub.id).ends_at)  # the plan lapses: Free, the owner's one login
    assert plans.effective_plan(db, shop.id)["code"] == "free"
    # The first request after the lapse fits the shop to its plan: both are suspended and signed out.
    assert first.get("/api/owner/reservations").status_code == 403
    assert first.get("/api/owner/reservations").status_code == 401
    assert second.get("/api/owner/reservations").status_code == 401
    rows = {r.name: r for r in staff.rows(db).find({"shop_id": shop.id})}
    assert {r.status for r in rows.values()} == {StaffStatus.SUSPENDED}
    # Signing in again does not get a suspended member back in.
    again = sign_in(first_phone)
    assert again.get("/api/auth/me").json()["shop"]["staff_status"] == "suspended"
    res = again.get("/api/owner/reservations")
    assert res.status_code == 403
    assert invite(owner, new_phone(), "Staff Third").json()["error"]["code"] == "plan_limit"

    plans.extend(db, sub.id, actor=ADMIN, reason=REASON, months=12)  # renewed: Growth again
    assert plans.effective_plan(db, shop.id)["code"] == "partner_seat"
    assert again.get("/api/owner/reservations").status_code == 200
    assert {r.status for r in staff.rows(db).find({"shop_id": shop.id})} == {StaffStatus.ACTIVE}
    assert sign_in(second_phone).get("/api/owner/reservations").status_code == 200


def test_fewer_logins_suspend_the_newest_staff_first(db, world, monkeypatch):
    shop = world.shop(1)
    paid(db, shop)
    owner = as_owner(world, shop)
    oldest, _ = staff_member(world, owner, "Staff Oldest")
    newest, _ = staff_member(world, owner, "Staff Newest")

    monkeypatch.setattr(staff, "logins", lambda db, shop_id, at=None: 2)  # a plan with one staff seat
    assert staff.reconcile(db, shop.id)["suspended"] == [staff.rows(db).find_one({"name": "Staff Newest"}).id]
    assert oldest.get("/api/owner/reservations").status_code == 200
    assert newest.get("/api/owner/reservations").status_code == 401

    monkeypatch.setattr(staff, "logins", lambda db, shop_id, at=None: 3)
    assert staff.reconcile(db, shop.id)["reactivated"] == [staff.rows(db).find_one({"name": "Staff Newest"}).id]


# ---------------------------------------------------------------- removal


def test_removal_ends_the_staff_members_sessions_at_once(db, world):
    shop = world.shop(1)
    paid(db, shop)
    owner = as_owner(world, shop)
    member, phone = staff_member(world, owner, "Staff Removed")
    assert member.get("/api/owner/reservations").status_code == 200
    row = staff.rows(db).find_one({"phone": phone})
    version = db.users.get(row.user_id).token_version

    res = owner.delete(f"/api/owner/staff/{row.id}", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["staff"] == []
    assert member.get("/api/owner/reservations").status_code == 401
    assert member.get("/api/auth/me").json() is None
    assert db.users.get(row.user_id).token_version == version + 1
    assert staff.rows(db).get(row.id).status == StaffStatus.REMOVED
    # Signing in again reaches the account, but no shop.
    back = sign_in(phone)
    assert back.get("/api/owner/reservations").status_code == 403
    assert owner.delete(f"/api/owner/staff/{row.id}", headers=HEADERS).status_code == 404

    # A former staff member's number can be invited again, and the account is reused on sign-in.
    assert invite(owner, phone, "Staff Removed").status_code == 200
    assert sign_in(phone).get("/api/owner/reservations").status_code == 200
    assert staff.rows(db).find_one({"phone": phone, "status": "active"}).user_id == row.user_id


def test_another_owner_cannot_remove_this_shops_staff(db, world):
    a, b = world.shop(1), world.shop(2)
    paid(db, a)
    _, phone = staff_member(world, as_owner(world, a), "Staff Kept")
    row = staff.rows(db).find_one({"phone": phone})
    assert as_owner(world, b).delete(f"/api/owner/staff/{row.id}", headers=HEADERS).status_code == 404
    assert staff.rows(db).get(row.id).status == StaffStatus.ACTIVE


def test_password_sign_in_is_unchanged_for_owners(world):
    shop = world.shop(1)
    owner = world.db.users.get(shop.owner_id)
    c = new_client()
    res = c.post("/api/auth/login", json={"email": owner.email, "password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["role"] == "owner"
