"""Data export, consent withdrawal, nomination, account deletion and retention (P3.2; plan sections 8.4 and
8.5; docs/legal/retention.md)."""
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.api.routes.auth import record_consents
from app.api.routes.grievances import grievance_doc
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.database import client as mongo_client
from app.core.security import hash_password
from app.main import app
from app.models import (
    Notification,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    Reservation,
    ReservationStatus,
    Review,
    Role,
    SaleSource,
    SalesRecord,
    SearchEvent,
    Shop,
    User,
)
from app.schemas.requests import ConsentIn
from app.schemas.serializers import review_out
from app.services import privacy
from app.services.search import SearchParams, log_search
from database import schema
from database.migrations import m0004_retention_ttl
from tests.conftest import CENTER, HEADERS, PASSWORD

DAY = timedelta(days=1)


# ---------------------------------------------------------------- helpers


@pytest.fixture
def made(db):
    """Ids of the users and shops a test creates; everything that refers to them is removed afterwards."""
    track: dict[str, list[int]] = {"users": [], "shops": []}
    yield track
    users, shops = track["users"], track["shops"]
    products = [d["_id"] for d in db.products.find_raw({"shop_id": {"$in": shops}}, {"_id": 1})]
    for name, flt in (("reservations", {"$or": [{"customer_id": {"$in": users}}, {"shop_id": {"$in": shops}}]}),
                      ("orders", {"$or": [{"customer_id": {"$in": users}}, {"shop_id": {"$in": shops}}]}),
                      ("reviews", {"$or": [{"customer_id": {"$in": users}}, {"shop_id": {"$in": shops}}]}),
                      ("notifications", {"user_id": {"$in": users}}),
                      ("notification_outbox", {"user_id": {"$in": users}}),
                      ("search_history", {"user_id": {"$in": users}}),
                      ("consents", {"user_id": {"$in": users}}),
                      ("sales_history", {"shop_id": {"$in": shops}}),
                      ("status_events", {"$or": [{"shop_id": {"$in": shops}}, {"actor_id": {"$in": users}}]}),
                      ("inventory_events", {"$or": [{"shop_id": {"$in": shops}}, {"actor_id": {"$in": users}}]}),
                      ("products", {"_id": {"$in": products}}),
                      ("shops", {"_id": {"$in": shops}}),
                      ("users", {"_id": {"$in": users}})):
        db.mongo[name].delete_many(flt)


def make_user(db, made, role: Role = Role.CUSTOMER, **consent) -> User:
    user = db.users.insert(User(name="Priya Rao", email=f"privacy-{uuid.uuid4().hex[:10]}@example.com",
                                phone=f"98{uuid.uuid4().int % 10 ** 8:08d}",  # one account per phone (P4.1)
                                password_hash=hash_password(PASSWORD), role=role,
                                home_lat=12.30123, home_lng=76.65123, home_label="Home"))
    made["users"].append(user.id)
    record_consents(db, user.id, ConsentIn(account_and_orders=True, age_18_plus=True, **consent))
    return user


def signed_in(user: User) -> TestClient:
    c = TestClient(app)
    res = c.post("/api/auth/login", json={"email": user.email, "password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 200, res.text
    return c


def reservation(db, customer_id: int, product_id: int, status: ReservationStatus, *, hold: bool = False,
                quantity: int = 2) -> Reservation:
    product = db.products.get(product_id)
    res = db.reservations.insert(Reservation(
        code=f"R{uuid.uuid4().hex[:8].upper()}", customer_id=customer_id, shop_id=product.shop_id,
        product_id=product_id, quantity=quantity, unit_price=product.price, status=status,
        note="Ring me at 9876500011", expires_at=utcnow() + timedelta(hours=1), stock_held=hold))
    if hold:  # the stock the shop set aside when it confirmed
        db.products.update_one({"_id": product_id}, {"$inc": {"quantity": -quantity}})
    return res


def order(db, customer_id: int, product_id: int, status: OrderStatus, **fields) -> Order:
    product = db.products.get(product_id)
    return db.orders.insert(Order(
        code=f"O{uuid.uuid4().hex[:8].upper()}", customer_id=customer_id, shop_id=product.shop_id, status=status,
        delivery_address="12 Temple Street, Agrahara", delivery_lat=12.304567, delivery_lng=76.655678,
        contact_phone="9876500011", distance_km=1.2, subtotal=product.price, delivery_fee=30,
        total=product.price + 30, note="Leave with the neighbour",
        items=[OrderItem(product_id=product_id, name=product.name, unit_price=product.price, quantity=1)], **fields))


def owner_with_shop(db, made) -> tuple[User, Shop, Product]:
    owner = make_user(db, made, Role.OWNER)
    shop = db.shops.insert(Shop(owner_id=owner.id, name="Privacy Stores", slug=f"privacy-{owner.id}",
                                address_line="9 Far Road", city="Mysuru", lat=CENTER[0] + 1, lng=CENTER[1] + 1,
                                offers_delivery=True, delivery_radius_km=5))
    made["shops"].append(shop.id)
    product = db.products.insert(Product(shop_id=shop.id, category_id=1, name="Privacy Tape", price=40, quantity=20))
    return owner, shop, product


def consents(db, user_id: int) -> list[dict]:
    return list(db.mongo["consents"].find({"user_id": user_id}, sort=[("_id", 1)]))


# ---------------------------------------------------------------- deletion


def test_deletion_with_nothing_active_anonymises_at_once(db, made, ids):
    user = make_user(db, made, whatsapp=True, personalised_suggestions=True)
    other = make_user(db, made)
    c = signed_in(user)
    done = reservation(db, user.id, ids["tape"], ReservationStatus.COMPLETED)
    review = db.reviews.insert(Review(customer_id=user.id, shop_id=done.shop_id, product_id=ids["tape"],
                                      reservation_id=done.id, rating=4, comment="Priya here, call me"))
    delivered = order(db, user.id, ids["tape"], OrderStatus.DELIVERED, delivered_at=utcnow())
    db.notifications.insert(Notification(user_id=user.id, kind="test", title="Hello"))
    db.notifications.insert(Notification(user_id=other.id, kind="test", title="Not theirs"))
    db.search_history.insert(SearchEvent(user_id=user.id, query="tape", normalized_query="tape", lat=12.3, lng=76.6))
    sale = db.sales_history.insert(SalesRecord(shop_id=done.shop_id, product_id=ids["tape"], customer_id=user.id,
                                               basket_id=done.code, quantity=2, unit_price=40,
                                               source=SaleSource.RESERVATION))
    privacy.set_nominee(db, user, "Ravi Rao", "9876500012", None)

    plan = c.get("/api/auth/deletion").json()
    assert plan["outcome"] == "immediate" and plan["open"] == [] and plan["in_flight"] == []

    wrong = c.post("/api/auth/delete", json={"password": "not-the-password"}, headers=HEADERS)
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "wrong_password"

    res = c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 200 and res.json() == {"status": "deleted"}

    stored = db.users.raw.find_one({"_id": user.id})
    assert stored["name"] == "Deleted user" and stored["email"] == privacy.deleted_email(user.id)
    assert stored["phone"] is None and stored["password_hash"] == "" and stored["is_active"] is False
    assert stored["home_lat"] is None and stored["home_label"] is None and stored["sessions"] == []
    assert stored["token_version"] == user.token_version + 1 and "nominee" not in stored
    assert stored["deleted_at"] is not None
    # Signed out everywhere, and the old password no longer works.
    assert c.get("/api/auth/me").json() is None
    assert TestClient(app).post("/api/auth/login", json={"email": user.email, "password": PASSWORD},
                                headers=HEADERS).status_code == 401
    # Optional consents withdrawn by new rows; the old rows are untouched.
    state = privacy.consent_state(db, user.id)
    assert not state["whatsapp"] and not state["personalised_suggestions"]
    assert sum(1 for r in consents(db, user.id) if r["granted"] is False and r["purpose"] == "whatsapp") == 1
    # Notifications and searches gone; another user's kept.
    assert db.notifications.count({"user_id": user.id}) == 0 and db.notifications.count({"user_id": other.id}) == 1
    assert db.search_history.count({"user_id": user.id}) == 0
    # The review keeps its rating, loses its comment and shows "Former customer".
    kept = db.reviews.get(review.id)
    assert kept.rating == 4 and kept.comment is None
    kept.customer = db.users.get(user.id)
    assert review_out(kept)["customer_name"] == "Former customer"
    # Reservations and orders stay, without phone, address or note.
    assert db.reservations.get(done.id).note is None
    o = db.orders.raw.find_one({"_id": delivered.id})
    assert o["status"] == "DELIVERED" and o["contact_phone"] == "" and o["delivery_address"] == ""
    assert o["delivery_lat"] == 12.3 and o["total"] == delivered.total
    assert db.sales_history.get(sale.id).customer_id is None
    db.sales_history.delete_many({"_id": sale.id})


def test_an_open_reservation_blocks_deletion_until_cancel_all(db, made, ids):
    user = make_user(db, made)
    c = signed_in(user)
    stock = db.products.get(ids["tape"]).quantity
    held = reservation(db, user.id, ids["tape"], ReservationStatus.CONFIRMED, hold=True)
    assert db.products.get(ids["tape"]).quantity == stock - 2

    plan = c.get("/api/auth/deletion").json()
    assert plan["outcome"] == "cancel_first" and plan["then"] == "immediate"
    assert [(i["kind"], i["code"]) for i in plan["open"]] == [("reservation", held.code)]

    res = c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "deletion_blocked" and [i["code"] for i in err["open"]] == [held.code]
    assert db.users.get(user.id).is_active and db.reservations.get(held.id).status == ReservationStatus.CONFIRMED

    res = c.post("/api/auth/delete", json={"password": PASSWORD, "cancel_open": True}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["status"] == "deleted"
    assert db.reservations.get(held.id).status == ReservationStatus.CANCELLED
    assert db.products.get(ids["tape"]).quantity == stock  # the held stock was released
    assert db.users.get(user.id).name == "Deleted user"


def test_a_cancellable_order_blocks_deletion_until_cancel_all(db, made, ids):
    user = make_user(db, made)
    c = signed_in(user)
    pending = order(db, user.id, ids["tape"], OrderStatus.PENDING)
    confirmed = order(db, user.id, ids["tape"], OrderStatus.SHOP_CONFIRMED)

    res = c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 409
    assert sorted(i["code"] for i in res.json()["error"]["open"]) == sorted([pending.code, confirmed.code])

    res = c.post("/api/auth/delete", json={"password": PASSWORD, "cancel_open": True}, headers=HEADERS)
    assert res.json()["status"] == "deleted"
    assert {db.orders.get(o.id).status for o in (pending, confirmed)} == {OrderStatus.CANCELLED}
    assert db.orders.raw.find_one({"_id": pending.id})["contact_phone"] == ""


@pytest.mark.parametrize("status", [OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERY_FAILED])
def test_an_order_in_flight_schedules_the_deletion_until_it_closes(db, made, ids, status):
    user = make_user(db, made)
    c = signed_in(user)
    moving = order(db, user.id, ids["tape"], status)

    plan = c.get("/api/auth/deletion").json()
    assert plan["outcome"] == "scheduled" and [i["code"] for i in plan["in_flight"]] == [moving.code]
    res = c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "scheduled"
    due = datetime.fromisoformat(body["scheduled_for"].removesuffix("Z"))
    assert timedelta(days=7) - timedelta(minutes=1) < due - utcnow() <= timedelta(days=7)
    assert c.get("/api/auth/deletion").json()["scheduled_for"] == body["scheduled_for"]
    assert db.users.get(user.id).is_active  # still signed in, to follow the order

    assert privacy.process_scheduled_deletions(db, utcnow()) == {"deleted": 0, "flagged": 0}
    assert db.users.get(user.id).deleted_at is None
    closed = OrderStatus.RETURNED_TO_SHOP if status == OrderStatus.DELIVERY_FAILED else OrderStatus.DELIVERED
    db.orders.update_one({"_id": moving.id}, {"$set": {"status": closed.value, "delivered_at": utcnow()}})
    assert privacy.process_scheduled_deletions(db, utcnow())["deleted"] == 1
    gone = db.users.get(user.id)
    assert gone.name == "Deleted user" and gone.deletion_due_at is None and not gone.is_active


def test_an_overdue_scheduled_deletion_is_flagged_for_the_admins_once(db, made, ids):
    user = make_user(db, made)
    order(db, user.id, ids["tape"], OrderStatus.OUT_FOR_DELIVERY)
    assert privacy.request_deletion(db, user, cancel_open=False)["status"] == "scheduled"
    admins = [d["_id"] for d in db.users.find_raw({"role": "admin", "is_active": True}, {"_id": 1})]
    before = db.notifications.count({"user_id": {"$in": admins}, "kind": "deletion_overdue"})

    later = utcnow() + 8 * DAY
    assert privacy.process_scheduled_deletions(db, later) == {"deleted": 0, "flagged": 1}
    assert privacy.process_scheduled_deletions(db, later) == {"deleted": 0, "flagged": 0}  # flagged once
    assert db.users.get(user.id).deletion_flagged_at is not None
    assert db.notifications.count({"user_id": {"$in": admins}, "kind": "deletion_overdue"}) == before + len(admins)
    db.notifications.delete_many({"kind": "deletion_overdue"})


def test_an_owner_closes_the_shop_first_then_is_anonymised(db, made, ids):
    owner, shop, product = owner_with_shop(db, made)
    customer = make_user(db, made)
    request = reservation(db, customer.id, product.id, ReservationStatus.REQUESTED)
    held = reservation(db, customer.id, ids["tape"], ReservationStatus.CONFIRMED)  # another shop: not the owner's
    c = signed_in(owner)

    plan = c.get("/api/auth/deletion").json()
    assert plan["outcome"] == "cancel_first" and plan["shops"] == ["Privacy Stores"]
    assert [i["code"] for i in plan["open"]] == [request.code]
    assert c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS).status_code == 409

    res = c.post("/api/auth/delete", json={"password": PASSWORD, "cancel_open": True}, headers=HEADERS)
    assert res.json()["status"] == "deleted"
    assert db.reservations.get(request.id).status == ReservationStatus.REJECTED
    assert db.reservations.get(held.id).status == ReservationStatus.CONFIRMED
    assert db.shops.get(shop.id).is_active is False
    assert db.products.get(product.id).is_active is False
    assert db.users.get(owner.id).name == "Deleted user"
    db.reservations.update_one({"_id": held.id}, {"$set": {"status": "CANCELLED"}})


def test_an_owner_with_a_delivery_in_flight_is_scheduled(db, made):
    owner, shop, product = owner_with_shop(db, made)
    customer = make_user(db, made)
    order(db, customer.id, product.id, OrderStatus.OUT_FOR_DELIVERY)
    res = signed_in(owner).post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS)
    assert res.json()["status"] == "scheduled"
    assert db.shops.get(shop.id).is_active  # open until its work is done


def test_admins_cannot_delete_themselves(admin, db):
    assert admin.get("/api/auth/deletion").json()["outcome"] == "refused"
    res = admin.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 403 and res.json()["error"]["code"] == "admin_self_delete"
    assert db.users.find_one({"email": "admin@example.com"}).is_active


# ---------------------------------------------------------------- export and nominee


def test_export_holds_every_section_including_the_nominee(db, made, ids):
    user = make_user(db, made, whatsapp=True)
    c = signed_in(user)
    assert c.put("/api/auth/nominee", json={"name": "Ravi Rao", "email": "Ravi@Example.com"},
                 headers=HEADERS).status_code == 200
    reservation(db, user.id, ids["tape"], ReservationStatus.COMPLETED)
    order(db, user.id, ids["tape"], OrderStatus.DELIVERED, delivered_at=utcnow())
    db.notifications.insert(Notification(user_id=user.id, kind="test", title="Hello"))
    db.search_history.insert(SearchEvent(user_id=user.id, query="tape", normalized_query="tape"))

    res = c.get("/api/me/export")
    assert res.status_code == 200
    assert res.headers["content-disposition"] == f'attachment; filename="nearshop-export-{user.id}.json"'
    data = res.json()
    assert data["profile"]["email"] == user.email and data["profile"]["home_label"] == "Home"
    assert data["nominee"]["name"] == "Ravi Rao" and data["nominee"]["email"] == "ravi@example.com"
    assert {r["purpose"] for r in data["consents"]} >= {"account_and_orders", "age_18_plus", "whatsapp"}
    assert data["consents_in_force"]["whatsapp"] is True
    for section in ("reservations", "orders", "notifications", "search_history"):
        assert len(data[section]) == 1, section
    assert data["orders"][0]["contact_phone"] == "9876500011"
    assert "password_hash" not in res.text and "token_version" not in res.text and "sessions" not in data["profile"]


def test_grievances_are_exported_without_the_resolver_and_unlinked_on_deletion(db, made):
    user, officer = make_user(db, made), make_user(db, made, Role.ADMIN)
    c = signed_in(user)
    grievances = db.mongo["grievances"]
    resolved = {**grievance_doc(-11, category="order", contact="9876500011", description="about my order",
                                user_id=user.id),
                "status": "resolved", "resolved_at": utcnow(), "resolved_by": officer.id, "resolution": "Refunded"}
    grievances.insert_many([resolved, grievance_doc(-12, category="other", contact="9876500011",
                                                    description="still looking into it", user_id=user.id)])
    try:
        data = c.get("/api/me/export").json()
        exported = {g["id"]: g for g in data["grievances"]}
        assert set(exported) == {-11, -12}
        assert exported[-11]["resolution"] == "Refunded" and exported[-11]["status"] == "resolved"
        assert all("resolved_by" not in g for g in exported.values())

        assert c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS).json() == {"status": "deleted"}
        kept = list(grievances.find({"_id": {"$in": [-11, -12]}}))
        assert len(kept) == 2 and all(g["user_id"] is None for g in kept)
        assert all(g["contact"] == "9876500011" for g in kept)  # still answerable through the contact given
    finally:
        grievances.delete_many({"_id": {"$in": [-11, -12]}})


def test_whatsapp_outbox_rows_are_exported_and_deleted_with_the_account(db, made):
    user = make_user(db, made)
    c = signed_in(user)
    now = utcnow()
    outbox = db.mongo["notification_outbox"]
    base = {"user_id": user.id, "channel": "whatsapp", "template": "nearshop_order_status", "language": "en",
            "buttons": [], "created_at": now, "next_attempt_at": now}
    outbox.insert_many([
        {**base, "_id": -21, "notification_id": -21, "kind": "order_preparing", "status": "queued", "attempts": 0,
         "params": ["Order D-ABC123 is being prepared", "Your order is being packed."]},
        {**base, "_id": -22, "notification_id": -22, "kind": "order_delivered", "status": "delivered",
         "attempts": 1, "params": None, "message_id": "wamid.privacy.22", "sent_at": now, "delivered_at": now,
         "cost_estimate": 0.1}])
    try:
        data = c.get("/api/me/export").json()
        exported = {m["id"]: m for m in data["whatsapp_messages"]}
        assert set(exported) == {-21, -22}
        assert exported[-21]["params"][0] == "Order D-ABC123 is being prepared"
        assert exported[-22]["status"] == "delivered"
        assert all(k not in m for m in exported.values() for k in privacy.OUTBOX_INTERNAL)

        assert c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS).json() == {"status": "deleted"}
        assert outbox.count_documents({"user_id": user.id}) == 0
    finally:
        outbox.delete_many({"_id": {"$in": [-21, -22]}})


def test_export_of_500_records_takes_under_60_seconds(db, made, ids):
    user = make_user(db, made)
    c = signed_in(user)
    db.notifications.insert_many([Notification(user_id=user.id, kind="test", title=f"N{i}") for i in range(200)])
    db.search_history.insert_many([SearchEvent(user_id=user.id, query=f"q{i}", normalized_query=f"q{i}")
                                   for i in range(200)])
    for _ in range(100):
        order(db, user.id, ids["tape"], OrderStatus.DELIVERED, delivered_at=utcnow())
    started = time.perf_counter()
    res = c.get("/api/me/export")
    seconds = time.perf_counter() - started
    assert res.status_code == 200
    data = res.json()
    assert len(data["notifications"]) + len(data["search_history"]) + len(data["orders"]) == 500
    assert seconds < 60, seconds


def test_a_nominee_is_saved_replaced_and_removed_on_deletion(db, made):
    user = make_user(db, made)
    c = signed_in(user)
    bad = c.put("/api/auth/nominee", json={"name": "Nobody"}, headers=HEADERS)
    assert bad.status_code == 422
    assert c.put("/api/auth/nominee", json={"name": "First Nominee", "phone": "9876500013"},
                 headers=HEADERS).json()["nominee"]["name"] == "First Nominee"
    c.put("/api/auth/nominee", json={"name": "Second Nominee", "email": "second@example.com"}, headers=HEADERS)
    stored = db.users.get(user.id).nominee
    assert stored.name == "Second Nominee" and stored.email == "second@example.com" and stored.phone is None
    assert c.get("/api/auth/privacy").json()["nominee"]["name"] == "Second Nominee"
    assert c.delete("/api/auth/nominee", headers=HEADERS).json() == {"nominee": None}
    assert db.users.get(user.id).nominee is None
    c.put("/api/auth/nominee", json={"name": "Third Nominee", "phone": "9876500014"}, headers=HEADERS)
    assert c.post("/api/auth/delete", json={"password": PASSWORD}, headers=HEADERS).json()["status"] == "deleted"
    assert "nominee" not in db.users.raw.find_one({"_id": user.id})


# ---------------------------------------------------------------- consent and search logging


def test_consent_withdrawal_appends_a_row_per_optional_purpose(db, made):
    user = make_user(db, made, whatsapp=True, search_measurement=True)
    c = signed_in(user)
    before = consents(db, user.id)
    for purpose in ("whatsapp", "search_measurement"):
        res = c.delete(f"/api/auth/consents/{purpose}", headers=HEADERS)
        assert res.status_code == 200 and res.json()["consents"][purpose] is False
    res = c.put("/api/auth/consents/personalised_suggestions", headers=HEADERS)
    assert res.json()["consents"]["personalised_suggestions"] is True
    after = consents(db, user.id)
    assert after[:len(before)] == before  # never updated in place
    added = after[len(before):]
    assert [(r["purpose"], r["granted"]) for r in added] == [
        ("whatsapp", False), ("search_measurement", False), ("personalised_suggestions", True)]
    assert all(r["notice_version"] == settings.notice_version and r["source"] == "web" for r in added)
    for purpose in ("account_and_orders", "age_18_plus", "marketing"):
        assert c.delete(f"/api/auth/consents/{purpose}", headers=HEADERS).status_code == 422


def test_search_logging_keeps_the_user_only_with_consent_and_rounds_to_2_decimals(db, made):
    without = make_user(db, made)
    with_consent = make_user(db, made, personalised_suggestions=True)
    probe = f"privacy probe {uuid.uuid4().hex[:8]}"
    p = SearchParams(q=probe, lat=12.30567, lng=76.65543)
    log_search(db, without.id, p, probe, 3)
    log_search(db, with_consent.id, p, probe, 3)
    rows = list(db.search_history.raw.find({"query": probe}, sort=[("_id", -1)]))
    assert [r["user_id"] for r in rows] == [with_consent.id, None]
    assert all(r["location"]["coordinates"] == [76.66, 12.31] for r in rows)
    db.search_history.delete_many({"_id": {"$in": [r["_id"] for r in rows]}})
    # Withdrawing the consent unlinks earlier searches too.
    db.search_history.insert(SearchEvent(user_id=with_consent.id, query="x", normalized_query="x"))
    privacy.set_consent(db, with_consent.id, "personalised_suggestions", False)
    assert db.search_history.count({"user_id": with_consent.id}) == 0


# ---------------------------------------------------------------- retention


def test_the_retention_job_with_a_controlled_clock(db, made, ids):
    user = make_user(db, made)
    now = utcnow()  # the clock the job is given; rows are placed relative to it
    old_search = db.search_history.insert(SearchEvent(user_id=user.id, query="o", normalized_query="o",
                                                      created_at=now - 31 * DAY))
    new_search = db.search_history.insert(SearchEvent(user_id=user.id, query="n", normalized_query="n",
                                                      created_at=now - 29 * DAY))
    old = order(db, user.id, ids["tape"], OrderStatus.CANCELLED, closed_at=now - 181 * DAY)
    delivered = order(db, user.id, ids["tape"], OrderStatus.DELIVERED, delivered_at=now - 181 * DAY)
    recent = order(db, user.id, ids["tape"], OrderStatus.RETURNED_TO_SHOP, closed_at=now - 179 * DAY)
    disputed = order(db, user.id, ids["tape"], OrderStatus.DELIVERED, delivered_at=now - 200 * DAY)
    by_code = order(db, user.id, ids["tape"], OrderStatus.CANCELLED, closed_at=now - 200 * DAY)
    settled = order(db, user.id, ids["tape"], OrderStatus.CANCELLED, closed_at=now - 200 * DAY)
    still_open = order(db, user.id, ids["tape"], OrderStatus.OUT_FOR_DELIVERY, created_at=now - 300 * DAY)
    grievances = db.mongo["grievances"]

    def grievance(_id: int, status: str, **ref) -> dict:  # a valid grievance (P3.8) naming an order
        return {**grievance_doc(_id, category="order", contact="9876500011", description="about my order",
                                ticket="G-PRV" + "ABC"[-_id - 1] * 3, **ref), "status": status}

    grievances.insert_many([grievance(-1, "in_progress", order_id=disputed.id),
                            grievance(-2, "open", order_code=by_code.code),
                            grievance(-3, "resolved", order_id=settled.id)])
    try:
        result = privacy.run_retention(db, now)
    finally:
        grievances.delete_many({"_id": {"$in": [-1, -2, -3]}})
    assert result["searches_unlinked"] >= 1 and result["orders_held_by_grievance"] == 2
    assert db.search_history.get(old_search.id).user_id is None
    assert db.search_history.get(new_search.id).user_id == user.id

    def phone(o):
        return db.orders.raw.find_one({"_id": o.id})["contact_phone"]

    assert [phone(o) for o in (old, delivered, settled)] == ["", "", ""]
    assert [phone(o) for o in (recent, disputed, by_code, still_open)] == ["9876500011"] * 4
    cleared = db.orders.raw.find_one({"_id": old.id})
    assert cleared["delivery_address"] == "" and cleared["note"] is None and cleared["contact_cleared_at"] == now
    # The grievances are gone now (as if resolved): the next run clears those two orders, and no others.
    assert privacy.run_retention(db, now)["orders_cleared"] == 2
    assert phone(disputed) == phone(by_code) == "" and phone(recent) == "9876500011"
    assert privacy.run_retention(db, now)["orders_cleared"] == 0


def test_the_retention_job_runs_once_per_nightly_slot(db, made, ids):
    user = make_user(db, made)
    db.mongo[jobs.LEASES].delete_one({"_id": "retention"})
    slot = jobs.retention_slot(utcnow())
    clock = {"now": slot + timedelta(minutes=15)}  # just after a 21:30 UTC slot
    scheduler = jobs.Scheduler(db.mongo, jobs={"retention": lambda: jobs.retention(lambda: clock["now"])},
                               holder="retention-test", clock=lambda: clock["now"])
    first = db.search_history.insert(SearchEvent(user_id=user.id, query="a", normalized_query="a",
                                                 created_at=clock["now"] - 40 * DAY))
    assert scheduler.tick() == {"retention": True}
    lease = db.mongo[jobs.LEASES].find_one({"_id": "retention"})["last_result"]
    assert lease["ok"] and lease["ran_for"] == slot.isoformat() and lease["searches_unlinked"] >= 1
    assert db.search_history.get(first.id).user_id is None

    second = db.search_history.insert(SearchEvent(user_id=user.id, query="b", normalized_query="b",
                                                  created_at=clock["now"] - 40 * DAY))
    clock["now"] += timedelta(minutes=30)
    scheduler.tick()  # same slot: not run again
    assert db.search_history.get(second.id).user_id == user.id
    assert db.mongo[jobs.LEASES].find_one({"_id": "retention"})["last_result"]["ran_for"] == slot.isoformat()
    clock["now"] += DAY  # the next night
    scheduler.tick()
    assert db.search_history.get(second.id).user_id is None
    db.mongo[jobs.LEASES].delete_one({"_id": "retention"})


def test_ttl_indexes_exist_with_the_expected_expiry(db):
    history = db.mongo["search_history"].index_information()
    assert history["created_ttl_180d"]["expireAfterSeconds"] == 180 * 24 * 60 * 60
    assert history["created_ttl_180d"]["key"] == [("created_at", 1)]
    assert "created" not in history
    notes = db.mongo["notifications"].index_information()
    assert notes["created_ttl_90d"]["expireAfterSeconds"] == 90 * 24 * 60 * 60
    assert schema.SEARCH_HISTORY_KEEP_SECONDS == 15552000 and schema.NOTIFICATIONS_KEEP_SECONDS == 7776000


def test_migration_4_replaces_the_created_index_and_is_idempotent(db):
    from pymongo import DESCENDING

    scratch = mongo_client[f"{db.mongo.name}_m4"]
    try:
        scratch["search_history"].create_index([("created_at", DESCENDING)], name="created")
        scratch["notifications"].create_index([("user_id", 1)], name="user")
        m0004_retention_ttl.apply(scratch)
        m0004_retention_ttl.apply(scratch)
        history = scratch["search_history"].index_information()
        assert "created" not in history and history["created_ttl_180d"]["expireAfterSeconds"] == 15552000
        assert scratch["notifications"].index_information()["created_ttl_90d"]["expireAfterSeconds"] == 7776000
    finally:
        mongo_client.drop_database(scratch.name)


def test_the_jobs_and_export_route_are_registered():
    assert jobs.JOBS["retention"] is jobs.retention and jobs.JOBS["process_deletions"] is jobs.process_deletions
    assert jobs.RETRY_AFTER_FAILURE["retention"] == timedelta(hours=1)
    assert "/api/me/export" in app.openapi()["paths"]


def test_signed_out_users_get_401(client):
    for method, path in (("get", "/api/me/export"), ("get", "/api/auth/privacy"), ("get", "/api/auth/deletion")):
        assert getattr(client, method)(path).status_code == 401
    assert client.post("/api/auth/delete", json={"password": "x"}, headers=HEADERS).status_code == 401


def test_the_privacy_settings_show_consents_nominee_and_outcome(db, made):
    user = make_user(db, made, whatsapp=True)
    data = signed_in(user).get("/api/auth/privacy").json()
    assert data["consents"]["whatsapp"] is True and data["consents"]["account_and_orders"] is True
    assert data["optional_purposes"] == ["whatsapp", "personalised_suggestions", "search_measurement"]
    assert data["nominee"] is None and data["deletion"]["outcome"] == "immediate"


def test_deleting_twice_is_harmless(made):
    db = Database()
    user = make_user(db, made)
    assert privacy.request_deletion(db, user, cancel_open=False) == {"status": "deleted"}
    assert privacy.request_deletion(db, db.users.get(user.id), cancel_open=False) == {"status": "deleted"}
