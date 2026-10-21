"""Review integrity (P3.5, plan section 8.2): completion codes, who may review, what counts toward a shop's
rating, the self-dealing flag and migration 6's recompute. Each test builds its own verified shop, owner and
customers, so the ratings it checks see only its own reviews."""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.main import app
from app.models import Order, OrderItem, OrderStatus, Product, Reservation, ReservationStatus, Role, Shop, User
from app.models.core import VerificationStatus, months_after
from app.services import reviews as review_service
from app.services.reliability import shop_reliability
from app.utils.text import short_code
from database.migrations import m0006_review_integrity
from tests.conftest import CENTER, HEADERS, PASSWORD, login

PW_HASH = hash_password(PASSWORD)
OWNER_PHONE = "9811100001"
SHOP_PHONE = "9811100002"


@pytest.fixture
def world(db, ids, monkeypatch):
    """A verified shop (pickup and delivery) with its own owner and one listing; removed afterwards."""
    monkeypatch.setattr(settings, "signups_open", True)
    tag = uuid.uuid4().hex[:8]
    owner = db.users.insert(User(name="Review Owner", email=f"rev-owner-{tag}@example.com", phone=OWNER_PHONE,
                                 password_hash=PW_HASH, role=Role.OWNER))
    now = utcnow()
    shop = db.shops.insert(Shop(
        owner_id=owner.id, name=f"Review Shop {tag}", slug=f"review-shop-{tag}", phone=SHOP_PHONE,
        address_line="9 Review Road", city="Mysuru", lat=CENTER[0] + 0.003, lng=CENTER[1], offers_delivery=True,
        delivery_radius_km=5, delivery_fee=20, category_ids=[], verification_status=VerificationStatus.VERIFIED,
        is_verified=True, kyc_type="udyam", kyc_number="UDYAM-KR-23-0000099", phone_verified_at=now,
        submitted_at=now, decided_at=now, verified_until=months_after(now)))
    product = db.products.insert(Product(shop_id=shop.id, category_id=1, name="Review Test Bulb", price=100,
                                         quantity=500))
    data = {"tag": tag, "owner": owner, "shop": shop, "product": product, "other_shop": ids["shop_b"],
            "other_product": ids["tape_far"], "users": [owner.id]}
    yield data
    users = data["users"] + [u["_id"] for u in db.users.find_raw({"email": {"$regex": f"-{tag}@"}}, {"_id": 1})]
    for coll in ("reservations", "orders", "reviews", "status_events", "sales_history"):
        db.mongo[coll].delete_many({"customer_id": {"$in": users}})
        db.mongo[coll].delete_many({"shop_id": shop.id})
    db.notifications.delete_many({"user_id": {"$in": users}})
    db.products.delete_many({"shop_id": shop.id})
    db.shops.delete_many({"_id": shop.id})
    db.users.delete_many({"_id": {"$in": users}})
    db.mongo["consents"].delete_many({"user_id": {"$in": users}})


def client_for(email: str) -> TestClient:
    return login(TestClient(app), email)


def make_customer(db: Database, world: dict, name: str, *, age_days: float = 10, phone: str | None = None) -> User:
    user = db.users.insert(User(name=name, email=f"rev-{name}-{world['tag']}@example.com", phone=phone,
                                password_hash=PW_HASH, role=Role.CUSTOMER,
                                created_at=utcnow() - timedelta(days=age_days)))
    world["users"].append(user.id)
    return user


def completed(db: Database, customer: User, shop_id: int, product_id: int, *, placed: datetime | None = None,
              done: datetime | None = None) -> Reservation:
    """A completed pickup, written directly (the code path that completes it is tested on its own)."""
    done = done or utcnow() - timedelta(hours=1)
    placed = placed or done - timedelta(hours=1)
    return db.reservations.insert(Reservation(
        code=short_code("R"), customer_id=customer.id, shop_id=shop_id, product_id=product_id, quantity=1,
        unit_price=100, status=ReservationStatus.COMPLETED, expires_at=placed + timedelta(minutes=30),
        confirmed_at=placed, completed_at=done, closed_at=None, created_at=placed, updated_at=done))


def review(customer: User, reservation: Reservation, rating: int = 5):
    return client_for(customer.email).post("/api/reviews", json={"reservation_id": reservation.id, "rating": rating},
                                           headers=HEADERS)


def regular(db: Database, world: dict, name: str) -> User:
    """A customer who also bought at another shop, so the self-dealing rule never applies to them."""
    c = make_customer(db, world, name)
    completed(db, c, world["other_shop"], world["other_product"])
    return c


def shop_rating(client: TestClient, db: Database, world: dict) -> tuple[float | None, float | None, Shop]:
    shop = db.shops.get(world["shop"].id)
    page = client.get(f"/api/shops/{shop.slug}", headers=HEADERS).json()
    assert page["reliability"]["rating_avg"] == shop_reliability(db, shop)["rating_avg"]
    return page["rating_avg"], page["reliability"]["rating_avg"], shop


# ---------------------------------------------------------------- completion codes

def test_pickup_completes_only_with_the_reservation_code(db, world):
    customer = make_customer(db, world, "pickup")
    owner = client_for(world["owner"].email)
    c = client_for(customer.email)
    r = c.post("/api/reservations", json={"product_id": world["product"].id, "quantity": 1}, headers=HEADERS).json()
    owner.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
    url = f"/api/owner/reservations/{r['id']}/complete"
    for body in ({}, {"code": ""}, {"code": "R-WRONG1"}, {"code": "R-ÄÖÜ"}):
        res = owner.post(url, json=body, headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "wrong_code"
    assert db.reservations.get(r["id"]).status == ReservationStatus.CONFIRMED
    done = owner.post(url, json={"code": f"  {r['code'].lower()} "}, headers=HEADERS)
    assert done.status_code == 200 and done.json()["status"] == "COMPLETED"


def test_delivery_code_is_the_customers_alone_and_delivery_needs_it(db, world):
    customer = make_customer(db, world, "delivery")
    owner = client_for(world["owner"].email)
    c = client_for(customer.email)
    body = {"items": [{"product_id": world["product"].id, "quantity": 1}], "lat": CENTER[0], "lng": CENTER[1],
            "address": "5 Review Lane", "phone": "9876543210"}
    o = c.post("/api/orders", json=body, headers=HEADERS).json()
    assert o["delivery_code"] is None  # created when the shop confirms
    owner_views = [owner.post(f"/api/owner/orders/{o['id']}/confirm", json={}, headers=HEADERS).json()]
    code = db.orders.get(o["id"]).delivery_code
    assert code and len(code) == 4 and code.isdigit()
    assert c.get(f"/api/orders/{o['id']}", headers=HEADERS).json()["delivery_code"] == code
    assert any(x["id"] == o["id"] and x["delivery_code"] == code for x in c.get("/api/orders", headers=HEADERS).json())
    notice = db.notifications.find_one({"user_id": customer.id, "kind": "order_shop_confirmed"})
    assert code in notice.body

    for action in ("prepare", "dispatch"):
        owner_views.append(owner.post(f"/api/owner/orders/{o['id']}/{action}", json={}, headers=HEADERS).json())
    owner_views.append(owner.get(f"/api/owner/orders/{o['id']}", headers=HEADERS).json())
    owner_views += owner.get("/api/owner/orders", headers=HEADERS).json()
    url = f"/api/owner/orders/{o['id']}/deliver"
    wrong = f"{(int(code) + 1) % 10_000:04d}"
    for attempt in ({}, {"code": wrong}):
        res = owner.post(url, json=attempt, headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "wrong_code"
        owner_views.append(res.json())
    owner_views.append(owner.post(url, json={"code": code}, headers=HEADERS).json())
    assert owner_views[-1]["status"] == "DELIVERED"
    for view in owner_views:
        assert "delivery_code" not in view and code not in str(view.get("events", ""))


# ---------------------------------------------------------------- who may review

def test_a_customer_with_the_owners_or_shops_phone_cannot_review(db, world):
    for name, phone in (("ownerphone", f"+91 {OWNER_PHONE}"), ("shopphone", SHOP_PHONE)):
        c = make_customer(db, world, name, phone=phone)
        res = review(c, completed(db, c, world["shop"].id, world["product"].id))
        assert res.status_code == 409 and res.json()["error"]["code"] == "review_not_allowed"
    assert db.reviews.count({"shop_id": world["shop"].id}) == 0


def test_review_window_and_account_age(db, world):
    late = make_customer(db, world, "late", age_days=40)
    res = review(late, completed(db, late, world["shop"].id, world["product"].id,
                                 done=utcnow() - timedelta(days=14, hours=1)))
    assert res.status_code == 409 and res.json()["error"]["code"] == "review_window_closed"

    # Created 2 days before the purchase: too new. Created 3 days before it: fine.
    young = make_customer(db, world, "young", age_days=2.5)
    res = review(young, completed(db, young, world["shop"].id, world["product"].id,
                                  placed=utcnow() - timedelta(days=0.5)))
    assert res.status_code == 409 and res.json()["error"]["code"] == "account_too_new"
    ok = make_customer(db, world, "oldenough", age_days=3.6)
    assert review(ok, completed(db, ok, world["shop"].id, world["product"].id,
                                placed=utcnow() - timedelta(days=0.5))).status_code == 200


def test_a_verified_phone_is_required_once_the_setting_is_on(db, world, monkeypatch):
    monkeypatch.setattr(settings, "review_requires_verified_phone", True)
    c = make_customer(db, world, "unverified", phone="9822200001")
    res = review(c, completed(db, c, world["shop"].id, world["product"].id))
    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_not_verified"
    db.users.update_one({"_id": c.id}, {"$set": {"phone_verified_at": utcnow()}})
    assert review(c, completed(db, c, world["shop"].id, world["product"].id)).status_code == 200


# ---------------------------------------------------------------- counting

def test_a_second_review_within_30_days_is_not_counted(db, world):
    c = regular(db, world, "repeat")
    first = review(c, completed(db, c, world["shop"].id, world["product"].id), rating=5)
    second = review(c, completed(db, c, world["shop"].id, world["product"].id), rating=1)
    assert first.status_code == second.status_code == 200
    assert db.reviews.get(first.json()["id"]).counted is True
    assert db.reviews.get(second.json()["id"]).counted is False
    shop = db.shops.get(world["shop"].id)
    assert (shop.rating_sum, shop.rating_count, shop.rating_customers) == (5, 1, 1)


def test_no_rating_below_5_distinct_customers(client, db, world):
    customers = [regular(db, world, f"rated{n}") for n in range(5)]
    for c in customers[:4]:
        assert review(c, completed(db, c, world["shop"].id, world["product"].id), rating=4).status_code == 200
    # A fifth review from one of the four is not a fifth customer.
    review(customers[0], completed(db, customers[0], world["shop"].id, world["product"].id), rating=4)
    page_avg, trust_avg, shop = shop_rating(client, db, world)
    assert (shop.rating_count, shop.rating_customers) == (4, 4)
    assert shop.rating_avg is None and page_avg is None and trust_avg is None
    nearby = client.get("/api/shops", params={"lat": CENTER[0], "lng": CENTER[1]}, headers=HEADERS).json()["shops"]
    assert [s["rating_avg"] for s in nearby if s["id"] == shop.id] == [None]

    assert review(customers[4], completed(db, customers[4], world["shop"].id, world["product"].id),
                  rating=5).status_code == 200
    page_avg, trust_avg, shop = shop_rating(client, db, world)
    assert shop.rating_customers == 5 and page_avg == trust_avg == round(21 / 5, 1)


# ---------------------------------------------------------------- self-dealing

def test_self_dealing_reviews_are_held_out_of_the_rating(db, world):
    for n in range(5):
        c = regular(db, world, f"honest{n}")
        assert review(c, completed(db, c, world["shop"].id, world["product"].id), rating=3).status_code == 200
    # One single-shop customer among six counted reviews (17%): nothing is held.
    singles = [make_customer(db, world, f"single{n}") for n in range(3)]
    first = review(singles[0], completed(db, singles[0], world["shop"].id, world["product"].id)).json()["id"]
    assert db.reviews.get(first).held is False
    assert db.shops.get(world["shop"].id).rating_count == 6
    # Three of eight (37.5%): all three single-shop reviews are held and flagged for the admin queue.
    for c in singles[1:]:
        review(c, completed(db, c, world["shop"].id, world["product"].id))
    held = db.reviews.find({"shop_id": world["shop"].id, "held": True})
    assert sorted(r.customer_id for r in held) == sorted(c.id for c in singles)
    assert all(r.held_at and r.held_reason == "self_dealing" for r in held)
    shop = db.shops.get(world["shop"].id)
    assert (shop.rating_sum, shop.rating_count, shop.rating_customers) == (15, 5, 5) and shop.rating_avg == 3.0
    # Held reviews stay held even once the 30-day window has passed them by.
    review_service.recompute_shop(db.mongo, shop.id, now=utcnow() + timedelta(days=45))
    assert db.reviews.count({"shop_id": shop.id, "held": True}) == 3


def test_the_owner_self_dealing_path_is_blocked_end_to_end(db, world):
    """Current behaviour before P3.5: an owner registers a second, customer account with any email, reserves
    their own item, completes it and posts 5 stars. Each step is now blocked or neutralised."""
    owner = client_for(world["owner"].email)
    tag = world["tag"]

    def register(name: str, phone: str | None = None) -> TestClient:
        c = TestClient(app)
        body = {"name": "Not The Owner", "email": f"rev-{name}-{tag}@example.com", "password": "longenough1",
                "consent": {"account_and_orders": True, "age_18_plus": True}}
        if phone:
            body["phone"] = phone
        assert c.post("/api/auth/register", json=body, headers=HEADERS).status_code == 200
        return c

    def buy_and_complete(c: TestClient) -> int:
        r = c.post("/api/reservations", json={"product_id": world["product"].id, "quantity": 1},
                   headers=HEADERS).json()
        owner.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
        refused = owner.post(f"/api/owner/reservations/{r['id']}/complete", json={}, headers=HEADERS)
        assert refused.status_code == 409  # no completing without the code
        assert owner.post(f"/api/owner/reservations/{r['id']}/complete", json={"code": r["code"]},
                          headers=HEADERS).json()["status"] == "COMPLETED"
        return r["id"]

    def five_stars(c: TestClient, reservation_id: int):
        return c.post("/api/reviews", json={"reservation_id": reservation_id, "rating": 5}, headers=HEADERS)

    # 1. A fresh account: refused, it was created less than 3 days before the purchase.
    sock = register("sock")
    res = five_stars(sock, buy_and_complete(sock))
    assert res.status_code == 409 and res.json()["error"]["code"] == "account_too_new"

    # 2. With the owner's own phone, whatever its age: refused.
    phone_sock = register("phonesock", OWNER_PHONE)
    db.users.update_one({"email": f"rev-phonesock-{tag}@example.com"},
                        {"$set": {"created_at": utcnow() - timedelta(days=30)}})
    res = five_stars(phone_sock, buy_and_complete(phone_sock))
    assert res.status_code == 409 and res.json()["error"]["code"] == "review_not_allowed"

    # 3. An aged account with no phone gets through the eligibility rules, but buys only at this shop: the review
    # is held out of the rating and the shop still shows no rating.
    patient = register("patient")
    db.users.update_one({"email": f"rev-patient-{tag}@example.com"},
                        {"$set": {"created_at": utcnow() - timedelta(days=30)}})
    res = five_stars(patient, buy_and_complete(patient))
    assert res.status_code == 200
    assert db.reviews.get(res.json()["id"]).held is True
    shop = db.shops.get(world["shop"].id)
    assert shop.rating_count == 0 and shop.rating_avg is None


# ---------------------------------------------------------------- migration 6

@pytest.fixture
def scratch_db(ids):
    from app.core.database import client as mongo_client
    name = f"{Database().mongo.name}_r{utcnow():%H%M%S%f}"
    yield mongo_client[name]
    mongo_client.drop_database(name)


def test_migration_recomputes_ratings_and_codes_open_orders(scratch_db):
    now = utcnow()
    scratch_db.shops.insert_many([{"_id": 1, "rating_sum": 999, "rating_count": 99}, {"_id": 2}])
    purchases, reviews = [], []
    # Customers 1-5 also bought at shop 2; customer 6 bought only at shop 1. Customer 1 reviewed twice in 10 days.
    for n, cid in enumerate([1, 2, 3, 4, 5, 6, 1]):
        purchases.append({"_id": 100 + n, "customer_id": cid, "shop_id": 1, "status": "COMPLETED"})
        reviews.append({"_id": 200 + n, "customer_id": cid, "shop_id": 1, "rating": 1 if n == 6 else 4,
                        "reservation_id": 100 + n, "order_id": None, "created_at": now - timedelta(days=20 - n)})
    purchases += [{"_id": 150 + cid, "customer_id": cid, "shop_id": 2, "status": "COMPLETED"} for cid in range(1, 6)]
    scratch_db.reservations.insert_many(purchases)
    scratch_db.reviews.insert_many(reviews)
    item = OrderItem(product_id=1, name="x", unit_price=1, quantity=1)
    orders = [Order(id=300 + n, code=f"D-{n}", customer_id=1, shop_id=1, status=status, delivery_address="a",
                    delivery_lat=0, delivery_lng=0, contact_phone="1", distance_km=1, subtotal=1, delivery_fee=0,
                    total=1, items=[item]).to_mongo()
              for n, status in enumerate([OrderStatus.PENDING, OrderStatus.SHOP_CONFIRMED, OrderStatus.PREPARING,
                                          OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED])]
    orders[2]["delivery_code"] = "4321"
    scratch_db.orders.insert_many(orders)

    m0006_review_integrity.apply(scratch_db)
    first = {d["_id"]: d for d in scratch_db.reviews.find()}
    m0006_review_integrity.apply(scratch_db)  # idempotent
    assert {d["_id"]: d for d in scratch_db.reviews.find()} == first

    assert first[206]["counted"] is False and all(first[200 + n]["counted"] for n in range(6))
    # One single-shop customer among six counted reviews (17%): below the 30% line, nothing held.
    assert not any(r["held"] for r in first.values())
    shop = scratch_db.shops.find_one({"_id": 1})
    assert (shop["rating_sum"], shop["rating_count"], shop["rating_customers"]) == (24, 6, 6)
    assert Shop.model_validate({**shop, "owner_id": 1, "name": "s", "slug": "s", "address_line": "a", "city": "c",
                                "location": {"coordinates": [0, 0]}}).rating_avg == 4.0
    assert scratch_db.shops.find_one({"_id": 2})["rating_customers"] == 0

    codes = {d["_id"]: d.get("delivery_code") for d in scratch_db.orders.find()}
    assert codes[300] is None and codes[304] is None and codes[302] == "4321"
    assert all(codes[i] and len(codes[i]) == 4 and codes[i].isdigit() for i in (301, 303))
