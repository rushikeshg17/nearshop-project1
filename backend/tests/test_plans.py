"""Plans, entitlements and subscriptions (P9.4; app/services/plans.py; plan sections 8.17 and 8.18).

The tests use their own shops, owners and customers and, where the rules depend on dates, a controlled clock (the
now passed to the service and to the job); everything they write is removed afterwards. The catalogue is version 1,
inserted by migration 8 when the test database was reset."""
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from PIL import Image

from app import jobs
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.main import app
from app.models import Product, Reservation, ReservationStatus, Review, Role, Shop, User
from app.models.billing import Actor, SubscriptionStatus
from app.models.core import VerificationStatus, months_after
from app.services import plans
from app.utils.text import short_code
from database.migrations import m0008_plan_catalogue
from tests.conftest import CENTER, HEADERS, PASSWORD, login

IST = ZoneInfo("Asia/Kolkata")
PW_HASH = hash_password(PASSWORD)
ADMIN = Actor(type="admin", id=0)
REASON = "Recorded by hand from the payments ledger"
NOTICE_KINDS = [plans.KIND_REMINDER, plans.KIND_LAPSED, plans.KIND_GUARANTEE, plans.KIND_ACTIVATED,
                plans.KIND_ADVANCE_ALERT]


def ist(*args: int) -> datetime:
    """An IST wall-clock time as the naive UTC time the database stores."""
    return datetime(*args, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (30, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


def _clean(db: Database) -> None:
    db.mongo[plans.SUBSCRIPTIONS].delete_many({})
    db.mongo[plans.EVENTS].delete_many({})
    db.mongo[plans.PLANS].delete_many({"version": {"$gt": 1}})
    db.mongo["counters"].delete_many({"_id": plans.FOUNDING_COUNTER})
    db.notifications.delete_many({"kind": {"$in": NOTICE_KINDS}})


@pytest.fixture(autouse=True)
def clean(ids):
    db = Database()
    _clean(db)
    yield db
    _clean(db)


class World:
    """Shops with their own owners (and their listings, reservations, orders and reviews), removed afterwards."""

    def __init__(self, db: Database):
        self.db = db
        self.tag = uuid.uuid4().hex[:8]
        self.users: list[int] = []
        self.shops: list[int] = []
        self.started = utcnow()

    def owner(self, n: int, phone: str | None = None) -> User:
        user = self.db.users.insert(User(name=f"Plan Owner {n}", email=f"plan-owner{n}-{self.tag}@example.com",
                                         phone=phone, password_hash=PW_HASH, role=Role.OWNER))
        self.users.append(user.id)
        return user

    def customer(self, n: int, phone: str) -> User:
        user = self.db.users.insert(User(name=f"Plan Customer {n}", email=f"plan-cust{n}-{self.tag}@example.com",
                                         phone=phone, password_hash=PW_HASH, role=Role.CUSTOMER))
        self.users.append(user.id)
        return user

    def shop(self, n: int, city: str = "Ballari", owner: User | None = None, shop_phone: str = "9811200002") -> Shop:
        owner = owner or self.owner(n, phone="9811200001" if n == 0 else None)
        now = utcnow()
        shop = self.db.shops.insert(Shop(
            owner_id=owner.id, name=f"Plan Shop {n} {self.tag}", slug=f"plan-shop-{n}-{self.tag}", phone=shop_phone,
            address_line=f"{n} Plan Road", city=city, lat=CENTER[0] + 0.003, lng=CENTER[1], offers_delivery=True,
            delivery_radius_km=5, delivery_fee=20, category_ids=[], verification_status=VerificationStatus.VERIFIED,
            is_verified=True, kyc_type="udyam", kyc_number="UDYAM-KR-23-0000777", phone_verified_at=now,
            submitted_at=now, decided_at=now, verified_until=months_after(now)))
        self.shops.append(shop.id)
        return shop

    def product(self, shop: Shop, name: str = "Plantest Brass Tap") -> Product:
        return self.db.products.insert(Product(shop_id=shop.id, category_id=1, name=name, keywords="plantest tap",
                                               price=100, quantity=500))

    def cleanup(self) -> None:
        db, shops, users = self.db, self.shops, self.users
        for coll in ("reservations", "orders", "reviews", "status_events", "sales_history", "inventory_events"):
            db.mongo[coll].delete_many({"shop_id": {"$in": shops}})
        db.notifications.delete_many({"user_id": {"$in": users}})
        db.products.delete_many({"shop_id": {"$in": shops}})
        db.shops.delete_many({"_id": {"$in": shops}})
        db.users.delete_many({"_id": {"$in": users}})
        # The seeded customer's notifications about these shops' reservations and orders.
        cust = db.users.find_one({"email": "cust@example.com"})
        db.notifications.delete_many({"user_id": cust.id, "created_at": {"$gte": self.started}})
        from app.ai.semantic_index import index

        index.mark_dirty()


@pytest.fixture
def world(db):
    w = World(db)
    yield w
    w.cleanup()


def grant(db: Database, shop: Shop, now: datetime, source: str = "shop_paid", **kw) -> plans.Subscription:
    return plans.create_subscription(db, shop_ids=[shop.id], plan_code=kw.pop("plan_code", "growth"),
                                     period=kw.pop("period", "year"), source=source, actor=ADMIN, reason=REASON,
                                     now=now, **kw)


def event_kinds(db: Database, sub_id: int) -> list[str]:
    return [e["kind"] for e in db.mongo[plans.EVENTS].find({"subscription_id": sub_id}).sort("_id", 1)]


def request_at(db: Database, world: World, shop: Shop, product: Product, customer: User, at: datetime) -> None:
    """A customer request (a reservation request, P9.2) placed at `at`."""
    world.db.reservations.insert(Reservation(
        code=short_code("R"), customer_id=customer.id, shop_id=shop.id, product_id=product.id, quantity=1,
        unit_price=100, status=ReservationStatus.EXPIRED, expires_at=at + timedelta(minutes=30), created_at=at,
        updated_at=at, closed_at=at + timedelta(minutes=30)))


# ---------------------------------------------------------------- the catalogue (migration 8)


def test_migration_inserts_catalogue_version_1_with_the_business_model_values(db):
    rows = plans.catalogue(db, at=ist(2027, 8, 1))
    assert plans.catalogue_version(db) == 1
    assert set(rows) == {"free", "growth", "pro", "business", "partner_seat"}
    free, growth, pro, business, seat = (rows[c] for c in ("free", "growth", "pro", "business", "partner_seat"))
    assert (free.price_year_paise, free.on_sale) == (0, True)
    assert free.entitlements.demand_report == "teaser" and free.entitlements.logins == 1
    assert (growth.price_year_paise, growth.price_month_paise, growth.founding_year_paise, growth.on_sale) == \
        (149_900, 14_900, 99_900, True)
    g = growth.entitlements
    assert (g.demand_report, g.low_stock_alerts, g.logins, g.roi_statement, g.priority_support,
            g.boost_discount_pct, g.outcome_guarantee) == ("full", True, 3, True, True, 25, True)
    assert (pro.price_year_paise, pro.on_sale, pro.entitlements.pro_insights, pro.entitlements.logins) == \
        (349_900, False, True, 5)
    assert (business.price_year_paise, business.on_sale, business.entitlements.outlets,
            business.entitlements.logins) == (799_900, False, 5, 10)
    assert seat.entitlements == growth.entitlements and seat.price_year_paise is None and not seat.on_sale

    m0008_plan_catalogue.apply(db.mongo)  # idempotent
    assert db.mongo[plans.PLANS].count_documents({"version": 1}) == 5


# ---------------------------------------------------------------- versions


def test_a_new_price_version_leaves_existing_subscriptions_on_their_version_until_renewal(db, world, admin,
                                                                                          monkeypatch):
    monkeypatch.setattr(settings, "billing_starts_on", date(2020, 1, 1))
    shop = world.shop(1, city="Mysuru")
    res = admin.post("/api/admin/subscriptions", json={"shop_ids": [shop.id], "plan_code": "growth",
                                                       "source": "shop_paid", "reason": REASON}, headers=HEADERS)
    assert res.status_code == 200, res.text
    sub = res.json()
    assert (sub["plan_version"], sub["price_paise"], sub["founding"]) == (1, 149_900, False)

    short = admin.post("/api/admin/plans", json={"reason": "cut", "plans": {"growth": {"price_year_paise": 1}}},
                       headers=HEADERS)
    assert short.status_code == 422  # a reason of 10 characters or more
    published = admin.post("/api/admin/plans", headers=HEADERS, json={
        "reason": "B25 readout: Growth price cut", "plans": {"growth": {"price_year_paise": 119_900},
                                                            "pro": {"on_sale": True}}})
    assert published.status_code == 200, published.text
    assert published.json()["version"] == 2
    listed = admin.get("/api/admin/plans", headers=HEADERS).json()
    assert listed["in_force"] == 2 and listed["versions"] == [1, 2]
    by_code = {p["code"]: p for p in listed["plans"]}
    assert by_code["growth"]["price_year_paise"] == 119_900 and by_code["pro"]["on_sale"]
    assert by_code["growth"]["entitlements"] == plans.catalogue(db, version=1)["growth"].entitlements.model_dump()

    now = utcnow()
    current = plans.effective_plan(db, shop.id, now)
    assert (current["code"], current["version"]) == ("growth", 1)
    stored = plans.subscriptions(db).get(sub["id"])
    assert (stored.plan_version, stored.price_paise) == (1, 149_900)
    # A new subscription is sold on version 2.
    other = world.shop(2, city="Mysuru")
    assert grant(db, other, now).plan_version == 2

    renewed = plans.renew(db, sub["id"], actor=ADMIN, reason="Renewal payment recorded", now=now)
    assert (renewed.plan_version, renewed.price_paise) == (2, 119_900)
    assert renewed.ends_at == months_after(stored.ends_at, 12)
    assert plans.effective_plan(db, shop.id, now)["version"] == 2
    assert event_kinds(db, sub["id"]) == ["created", "renewed"]


# ---------------------------------------------------------------- entitlements


def test_the_entitlement_function_for_every_status_and_free_when_nothing_is_active(db, world, monkeypatch):
    shop = world.shop(1)
    paid = ist(2027, 6, 10, 11)
    nothing = plans.effective_plan(db, shop.id, paid)
    assert (nothing["code"], nothing["status"], nothing["subscription_id"]) == ("free", "free", None)
    assert nothing["entitlements"]["demand_report"] == "teaser"

    sub = grant(db, shop, paid, source="advance")
    assert sub.status == SubscriptionStatus.ADVANCE
    assert plans.effective_plan(db, shop.id, ist(2027, 7, 1))["code"] == "free"  # advance: not started

    plans.run_jobs(db, ist(2027, 8, 1, 0, 1))
    active = plans.effective_plan(db, shop.id, ist(2027, 9, 1))
    assert (active["code"], active["status"], active["subscription_id"]) == ("growth", "active", sub.id)
    assert active["entitlements"]["roi_statement"] is True
    assert plans.entitled(db, shop.id, "logins", ist(2027, 9, 1)) == 3

    end = plans.subscriptions(db).get(sub.id).ends_at
    assert plans.effective_plan(db, shop.id, end)["code"] == "free"  # Free from the end date, before the job
    assert plans.effective_plan(db, shop.id, end - timedelta(seconds=1))["code"] == "growth"
    monkeypatch.setattr(settings, "plan_guarantee_min_requests", 0)  # no guarantee credit in the way of the lapse
    plans.run_jobs(db, end)
    assert plans.subscriptions(db).get(sub.id).status == SubscriptionStatus.LAPSED
    assert plans.effective_plan(db, shop.id, end - timedelta(days=1))["code"] == "free"  # lapsed

    other = world.shop(2)
    cancelled = grant(db, other, ist(2027, 9, 1))
    plans.cancel(db, cancelled.id, actor=ADMIN, reason="Owner asked to stop", now=ist(2027, 9, 2))
    assert plans.effective_plan(db, other.id, ist(2027, 9, 3))["code"] == "free"

    third = world.shop(3)
    refunded = grant(db, third, ist(2027, 9, 1))
    db.mongo[plans.SUBSCRIPTIONS].update_one({"_id": refunded.id}, {"$set": {"status": "refunded"}})  # P9.5
    assert plans.effective_plan(db, third.id, ist(2027, 9, 3))["code"] == "free"

    # The route dependency for a paid feature asks effective_plan: 402 on Free (the real clock: nothing is active).
    check = plans.require_entitlement("roi_statement").dependency
    with pytest.raises(plans.PlanRequired) as refused:
        check(db.shops.get(third.id), db)
    assert (refused.value.status_code, refused.value.code) == (402, "plan_required")


# ---------------------------------------------------------------- never behind a payment


def _dependency_calls(dependant) -> list:
    out = []
    for dep in dependant.dependencies:
        out.append(dep.call)
        out.extend(_dependency_calls(dep))
    return out


def test_no_freshness_endpoint_asks_for_an_entitlement():
    # Each route with the dependencies it runs, its routers' included (the effective route, when FastAPI has one).
    routes = {(m, r.path): getattr(r, "_effective_route", r.route) for r in iter_route_contexts(app.routes)
              if hasattr(r, "route") for m in (r.methods or ())}
    assert ("GET", "/api/owner/plan") in routes
    for key in plans.FREE_TOOLS:
        assert key in routes, f"{key} is listed as a free tool but no such route exists"
        calls = _dependency_calls(routes[key].dependant)
        assert calls, key
        assert not [c for c in calls if hasattr(c, "__entitlement__")], key


def free_tool_calls(world: World, owner: TestClient, customer: TestClient, shop: Shop, round_: int
                    ) -> dict[tuple[str, str], int]:
    """Call every FREE_TOOLS endpoint as the shop's owner, with a new listing for the round's reservation and order.
    Returns each one's status code."""
    db = world.db
    product = world.product(shop, f"Plantest Brass Tap {round_}")
    out: dict[tuple[str, str], int] = {}

    def call(method: str, path: str, url: str, **kw) -> dict:
        res = owner.request(method, url, headers=HEADERS, **kw)
        out[(method, path)] = res.status_code
        assert res.status_code != 402, (method, url, res.text)
        return res.json() if res.headers.get("content-type", "").startswith("application/json") else {}

    call("GET", "/api/owner/products", "/api/owner/products")
    created = call("POST", "/api/owner/products", "/api/owner/products", json={
        "name": f"Plantest Washer {uuid.uuid4().hex[:4]}", "category_slug": "plumbing", "price": 25, "quantity": 9})
    pid = created["id"]
    call("PATCH", "/api/owner/products/{product_id}", f"/api/owner/products/{pid}", json={"price": 30})
    call("PUT", "/api/owner/products/{product_id}/stock", f"/api/owner/products/{pid}/stock", json={"quantity": 4})
    call("POST", "/api/owner/products/{product_id}/image", f"/api/owner/products/{pid}/image",
         files={"file": ("p.png", png(), "image/png")})
    call("DELETE", "/api/owner/products/{product_id}", f"/api/owner/products/{pid}")
    call("GET", "/api/owner/catalog", "/api/owner/catalog")
    item = db.catalog_items.find_one({"slug": "teflon-tape"})
    added = db.products.exists({"shop_id": shop.id, "catalog_item_id": item.id})
    if added:  # the second round: a fresh shop of the same world has it already
        db.products.delete_many({"shop_id": shop.id, "catalog_item_id": item.id})
    call("POST", "/api/owner/catalog/add", "/api/owner/catalog/add",
         json={"items": [{"catalog_item_id": item.id, "price": 40, "quantity": 10}]})
    call("GET", "/api/owner/import/template", "/api/owner/import/template")
    csv = b"Item name,Brand,Category,Quantity,Selling price,MRP\nPlantest Brass Tap,,Plumbing,7,100,\n"
    call("POST", "/api/owner/import/preview", "/api/owner/import/preview",
         files={"file": ("stock.csv", csv, "text/csv")})
    call("POST", "/api/owner/import/commit", "/api/owner/import/commit",
         json={"rows": [{"action": "update", "product_id": product.id, "quantity": 8}]})
    call("GET", "/api/owner/overview", "/api/owner/overview")

    r = customer.post("/api/reservations", json={"product_id": product.id, "quantity": 1}, headers=HEADERS)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    call("GET", "/api/owner/reservations", "/api/owner/reservations")
    call("GET", "/api/owner/reservations/{reservation_id}", f"/api/owner/reservations/{rid}")
    call("POST", "/api/owner/reservations/{reservation_id}/{action}", f"/api/owner/reservations/{rid}/confirm",
         json={})
    o = customer.post("/api/orders", headers=HEADERS, json={
        "items": [{"product_id": product.id, "quantity": 1}], "lat": CENTER[0], "lng": CENTER[1],
        "address": "12 Temple Street, Agrahara", "phone": "9876543210"})
    assert o.status_code == 200, o.text
    oid = o.json()["id"]
    call("GET", "/api/owner/orders", "/api/owner/orders")
    call("GET", "/api/owner/orders/{order_id}", f"/api/owner/orders/{oid}")
    call("POST", "/api/owner/orders/{order_id}/{action}", f"/api/owner/orders/{oid}/confirm", json={})

    call("GET", "/api/owner/verification", "/api/owner/verification")
    renewal = owner.post("/api/owner/verification", headers=HEADERS, data={"kyc_type": "udyam",
                                                                          "kyc_number": "UDYAM-KR-23-0000777"},
                         files={"storefront_photo": ("s.png", png(), "image/png")})
    # A verified shop outside its renewal window is told when it may renew: the same answer on every plan.
    assert renewal.status_code == 409 and renewal.json()["error"]["code"] == "renewal_not_open", renewal.text
    out[("POST", "/api/owner/verification")] = 200
    review = db.reviews.insert(Review(customer_id=world.users[-1], shop_id=shop.id, product_id=product.id,
                                      reservation_id=db.next_id("plan_test_reviews") + 10_000_000, rating=5))
    call("GET", "/api/owner/reviews", "/api/owner/reviews")
    call("POST", "/api/owner/reviews/{review_id}/reply", f"/api/owner/reviews/{review.id}/reply",
         json={"text": "Thank you!"})
    call("PUT", "/api/owner/reviews/{review_id}/reply", f"/api/owner/reviews/{review.id}/reply",
         json={"text": "Thank you, see you soon"})
    call("GET", "/api/owner/shop", "/api/owner/shop")
    call("PATCH", "/api/owner/shop", "/api/owner/shop", json={"tagline": "Taps and fittings"})
    return out


def test_every_freshness_endpoint_works_for_a_free_shop_and_for_a_lapsed_shop(db, world, customer):
    owner = world.owner(0, phone="9811200001")
    shop = world.shop(0, owner=owner)
    world.customer(0, "9811200099")  # the reviews' author
    with TestClient(app) as c:
        owner_client = login(c, owner.email)
        assert plans.effective_plan(db, shop.id)["code"] == "free"
        free = free_tool_calls(world, owner_client, customer, shop, 1)
        assert set(free) == set(plans.FREE_TOOLS)
        assert all(200 <= s < 300 for s in free.values()), free

        sub = grant(db, shop, utcnow() - timedelta(days=400), source="sponsored", plan_code="partner_seat")
        plans.run_jobs(db, utcnow())
        assert plans.subscriptions(db).get(sub.id).status == SubscriptionStatus.LAPSED
        assert plans.effective_plan(db, shop.id)["code"] == "free"
        lapsed = free_tool_calls(world, owner_client, customer, shop, 2)
        assert lapsed == free


def test_a_lapse_returns_the_shop_to_free_with_no_change_to_its_search_results(db, world, client, monkeypatch):
    monkeypatch.setattr(settings, "plan_guarantee_min_requests", 0)
    shop = world.shop(1)
    product = world.product(shop, "Plantest Gunmetal Valve")
    params = {"q": "plantest gunmetal valve", "lat": CENTER[0], "lng": CENTER[1]}
    sub = grant(db, shop, utcnow() - timedelta(days=10), source="sponsored", plan_code="partner_seat")
    assert plans.effective_plan(db, shop.id)["code"] == "partner_seat"
    before = client.get("/api/search", params=params).json()["groups"]
    assert product.id in [o["id"] for g in before for o in g["offers"]]

    plans.run_jobs(db, plans.subscriptions(db).get(sub.id).ends_at)
    assert plans.subscriptions(db).get(sub.id).status == SubscriptionStatus.LAPSED
    assert plans.effective_plan(db, shop.id)["code"] == "free"
    assert db.shops.get(shop.id).is_live
    after = client.get("/api/search", params=params).json()["groups"]
    assert after == before
    lapsed = db.notifications.find({"user_id": sub.account_id, "kind": plans.KIND_LAPSED})
    assert len(lapsed) == 1 and "unchanged" in lapsed[0].body


# ---------------------------------------------------------------- reminders and lapse on a controlled clock


def test_reminders_go_out_on_the_30th_7th_and_last_day_then_the_shop_lapses(db, world, monkeypatch):
    monkeypatch.setattr(settings, "plan_guarantee_min_requests", 0)
    shop = world.shop(1, city="Mysuru")
    sub = grant(db, shop, ist(2027, 9, 1, 10))
    assert sub.ends_at == ist(2028, 9, 1, 10)
    sent: list[date] = []
    day = date(2028, 7, 28)
    while day <= date(2028, 8, 31):
        for hour in (9, 18):  # twice a day: a stage goes out once
            result = plans.run_jobs(db, ist(day.year, day.month, day.day, hour))
            sent += [day] * result["reminders"]
        day += timedelta(days=1)
    assert sent == [date(2028, 8, 2), date(2028, 8, 25), date(2028, 8, 31)]
    notes = db.notifications.find({"user_id": sub.account_id, "kind": plans.KIND_REMINDER}, sort=[("_id", 1)])
    assert [n.title for n in notes] == ["Your NearShop plan ends in 30 days", "Your NearShop plan ends in 7 days",
                                        "Your NearShop plan ends tomorrow"]
    assert plans.effective_plan(db, shop.id, ist(2028, 9, 1, 9, 59))["code"] == "growth"
    assert plans.run_jobs(db, ist(2028, 9, 1, 10))["lapsed"] == 1
    assert plans.effective_plan(db, shop.id, ist(2028, 9, 1, 10))["code"] == "free"
    kinds = event_kinds(db, sub.id)
    assert [k for k in kinds if k != "guarantee_met"] == ["created", "reminder", "reminder", "reminder", "lapsed"]
    assert kinds.count("guarantee_met") == 4  # min requests 0 here: every quarter meets the guarantee
    assert db.shops.get(shop.id).is_active
    # Through the leased job too: nothing more is due.
    assert jobs.JOBS[jobs.SUBSCRIPTIONS_JOB] is jobs.subscriptions
    assert jobs.subscriptions(clock=lambda: ist(2028, 9, 2))["lapsed"] == 0


# ---------------------------------------------------------------- the outcome guarantee


def test_the_guarantee_credits_a_quarter_with_2_customer_requests_and_not_one_with_3(db, world):
    shop = world.shop(1, city="Mysuru")
    product = world.product(shop)
    customers = [world.customer(i, f"98112001{i:02d}") for i in range(6)]
    start = ist(2027, 9, 1, 10)
    sub = grant(db, shop, start)
    q1_end, q2_end = months_after(start, 3), months_after(start, 6)
    for c in customers[:2]:
        request_at(db, world, shop, product, c, start + timedelta(days=20))
    request_at(db, world, shop, product, customers[2], q1_end + timedelta(days=1))  # counts in quarter 2
    # The shop's and the owner's own requests never count.
    own = world.customer(9, "9811200002")
    request_at(db, world, shop, product, own, start + timedelta(days=21))

    assert plans.run_jobs(db, q1_end - timedelta(minutes=1))["guarantee_credits"] == 0
    first = plans.run_jobs(db, q1_end)
    assert (first["guarantee_credits"], first["guarantee_met"]) == (1, 0)
    after = plans.subscriptions(db).get(sub.id)
    assert after.ends_at == months_after(sub.ends_at, 3) and after.paid_until == sub.ends_at
    notice = db.notifications.find({"user_id": sub.account_id, "kind": plans.KIND_GUARANTEE})
    assert len(notice) == 1 and "2 customer request(s)" in notice[0].body
    assert plans.run_jobs(db, q1_end + timedelta(hours=1))["guarantee_credits"] == 0  # once

    for c in customers[3:5]:
        request_at(db, world, shop, product, c, q1_end + timedelta(days=30))
    second = plans.run_jobs(db, q2_end)
    assert (second["guarantee_credits"], second["guarantee_met"]) == (0, 1)
    assert plans.subscriptions(db).get(sub.id).ends_at == after.ends_at
    assert event_kinds(db, sub.id) == ["created", "guarantee_credit", "guarantee_met"]
    credit = db.mongo[plans.EVENTS].find_one({"subscription_id": sub.id, "kind": "guarantee_credit"})
    assert credit["actor"] == {"type": "system", "id": None} and credit["ends_at_after"] == after.ends_at

    # The owner sees this quarter's progress.
    with TestClient(app) as c:
        owner = login(c, db.users.get(sub.account_id).email)
        mine = owner.get("/api/owner/plan", headers=HEADERS).json()
    assert mine["plan"]["code"] == "free"  # the real clock is before this subscription starts
    progress = plans.guarantee_progress(db, plans.subscriptions(db).get(sub.id), q2_end + timedelta(days=10))
    assert progress["applies"] and progress["needed"] == 3 and progress["customer_requests"] == 0


# ---------------------------------------------------------------- the founding offer


def test_the_founding_cap_holds_at_100_under_concurrent_requests(db, world):
    assert settings.founding_offer_cap == 100
    owners = db.users.insert_many([User(name=f"Founding Owner {i}", email=f"found{i}-{world.tag}@example.com",
                                        password_hash=PW_HASH, role=Role.OWNER) for i in range(105)])
    world.users += [u.id for u in owners]
    now = utcnow()
    shops = db.shops.insert_many([Shop(
        owner_id=u.id, name=f"Founding Shop {i}", slug=f"found-{i}-{world.tag}", address_line=f"{i} Fort Road",
        city="Ballari", lat=15.14, lng=76.92, category_ids=[], verification_status=VerificationStatus.VERIFIED,
        is_verified=True, submitted_at=now, decided_at=now, verified_until=months_after(now))
        for i, u in enumerate(owners)])
    world.shops += [s.id for s in shops]
    paid = ist(2027, 6, 15, 12)

    def pay(shop: Shop):
        return plans.create_subscription(Database(), shop_ids=[shop.id], plan_code="growth", period="year",
                                         source="advance", actor=ADMIN, reason=REASON, now=paid)

    with ThreadPoolExecutor(max_workers=16) as pool:
        subs = list(pool.map(pay, shops))
    founding = [s for s in subs if s.founding]
    assert len(founding) == 100 and sorted(s.founding_slot for s in founding) == list(range(1, 101))
    assert {s.price_paise for s in founding} == {99_900}
    assert {s.price_paise for s in subs if not s.founding} == {149_900} and len(subs) == 105
    assert plans.founding_counter(db) | {"cities": None} == {"cap": 100, "taken": 100, "remaining": 0, "cities": None}
    assert db.mongo[plans.SUBSCRIPTIONS].count_documents({"founding": True}) == 100


def test_founding_offer_only_in_the_founding_cities_once_per_shop(db, world, admin):
    ballari, mysuru = world.shop(1, city="Ballari"), world.shop(2, city="Mysuru")
    paid = ist(2027, 6, 15, 12)
    first = grant(db, ballari, paid, source="advance")
    assert first.founding and first.price_paise == 99_900 and first.founding_slot == 1
    assert not grant(db, mysuru, paid, source="advance").founding
    plans.cancel(db, first.id, actor=ADMIN, reason="Refund requested by the owner", now=paid + timedelta(days=1))
    again = grant(db, ballari, paid + timedelta(days=2), source="advance")
    assert not again.founding and again.price_paise == 149_900  # one founding offer per shop, ever
    counter = admin.get("/api/admin/founding", headers=HEADERS).json()
    assert (counter["taken"], counter["remaining"]) == (1, 99)


# ---------------------------------------------------------------- advances


def test_advances_activate_on_billing_starts_on_and_alert_admins_after_300_days(db, world, monkeypatch):
    shop = world.shop(1)
    paid = ist(2027, 6, 10, 11)
    sub = grant(db, shop, paid, source="advance")
    assert (sub.status, sub.founding, sub.paid_at) == (SubscriptionStatus.ADVANCE, True, paid)
    with pytest.raises(plans.PlanRefused):
        grant(db, world.shop(2), paid)  # no shop-paid plan before billing starts
    with pytest.raises(plans.PlanRefused):
        grant(db, world.shop(3), ist(2027, 8, 2), source="advance")  # no advance once it has

    assert plans.run_jobs(db, ist(2027, 7, 31, 23, 59))["activated"] == 0
    assert plans.run_jobs(db, ist(2027, 8, 1, 0, 0, 30))["activated"] == 1
    active = plans.subscriptions(db).get(sub.id)
    assert (active.status, active.starts_at, active.ends_at, active.price_paise) == (
        SubscriptionStatus.ACTIVE, ist(2027, 8, 1), ist(2028, 8, 1), 99_900)
    assert plans.effective_plan(db, shop.id, ist(2027, 8, 2))["code"] == "growth"
    assert db.notifications.find({"user_id": sub.account_id, "kind": plans.KIND_ACTIVATED})
    assert event_kinds(db, sub.id) == ["created", "activated"]

    # Billing delayed: an advance still waiting 300 days after its payment is flagged to the admins, once.
    monkeypatch.setattr(settings, "billing_starts_on", date(2028, 12, 1))
    late = grant(db, world.shop(4), paid, source="advance")
    assert plans.run_jobs(db, paid + timedelta(days=299, hours=23))["advance_alerts"] == 0
    assert plans.run_jobs(db, paid + timedelta(days=300))["advance_alerts"] == 1
    assert plans.run_jobs(db, paid + timedelta(days=301))["advance_alerts"] == 0
    admin_id = db.users.find_one({"email": "admin@example.com"}).id
    alerts = db.notifications.find({"user_id": admin_id, "kind": plans.KIND_ADVANCE_ALERT})
    assert len(alerts) == 1 and f"{late.id}" in alerts[0].title
    assert plans.subscriptions(db).get(late.id).advance_alerted_at == paid + timedelta(days=300)
    assert event_kinds(db, late.id) == ["created", "advance_alert"]


# ---------------------------------------------------------------- the admin and owner endpoints


def test_admin_lists_grants_extends_and_cancels_with_a_reason(db, world, admin, monkeypatch):
    monkeypatch.setattr(settings, "billing_starts_on", date(2020, 1, 1))
    shop = world.shop(1, city="Mysuru")
    body = {"shop_ids": [shop.id], "plan_code": "growth", "source": "shop_paid", "reason": REASON}
    sub = admin.post("/api/admin/subscriptions", json=body, headers=HEADERS).json()
    assert admin.post("/api/admin/subscriptions", json=body, headers=HEADERS).status_code == 409  # one at a time
    pro = admin.post("/api/admin/subscriptions", headers=HEADERS,
                     json={**body, "shop_ids": [world.shop(2).id], "plan_code": "pro"})
    assert pro.status_code == 409 and "not on sale" in pro.text
    assert admin.post("/api/admin/subscriptions", json={**body, "reason": "short"}, headers=HEADERS).status_code == 422

    listed = admin.get("/api/admin/subscriptions", params={"status": "active"}, headers=HEADERS).json()
    assert [r["id"] for r in listed] == [sub["id"]] and listed[0]["shops"][0]["name"] == shop.name
    assert admin.get("/api/admin/subscriptions", params={"status": "lapsed"}, headers=HEADERS).json() == []

    url = f"/api/admin/subscriptions/{sub['id']}"
    both = admin.post(f"{url}/extend", json={"months": 1, "ends_at": "2030-01-01T00:00:00Z", "reason": REASON},
                      headers=HEADERS)
    assert both.status_code == 422
    extended = admin.post(f"{url}/extend", json={"months": 2, "reason": "Goodwill: app outage in March"},
                          headers=HEADERS).json()
    assert extended["ends_at"] > sub["ends_at"] and extended["paid_until"] == sub["paid_until"]
    cancelled = admin.post(f"{url}/cancel", json={"reason": "Owner closed the shop"}, headers=HEADERS).json()
    assert cancelled["status"] == "cancelled"
    assert plans.effective_plan(db, shop.id)["code"] == "free"
    assert admin.post(f"{url}/cancel", json={"reason": "Owner closed the shop"}, headers=HEADERS).status_code == 409

    detail = admin.get(url, headers=HEADERS).json()
    assert [(e["kind"], e["reason"], e["actor"]["type"]) for e in detail["events"]] == [
        ("created", REASON, "admin"), ("extended", "Goodwill: app outage in March", "admin"),
        ("cancelled", "Owner closed the shop", "admin")]
    assert detail["events"][-1]["status_before"] == "active" and detail["events"][-1]["status_after"] == "cancelled"


def test_owner_sees_plan_entitlements_founding_eligibility_and_guarantee_progress(db, world, monkeypatch):
    monkeypatch.setattr(settings, "billing_starts_on", date(2020, 1, 1))
    owner = world.owner(5, phone="9811200005")
    shop = world.shop(5, owner=owner)
    product = world.product(shop)
    with TestClient(app) as c:
        client = login(c, owner.email)
        free = client.get("/api/owner/plan", headers=HEADERS).json()
        assert free["plan"]["code"] == "free" and free["plan"]["entitlements"]["logins"] == 1
        assert free["founding"]["eligible"] and free["founding"]["price_year_paise"] == 99_900
        assert {p["code"] for p in free["on_sale"]} == {"free", "growth"}
        assert free["guarantee"]["applies"] is False

        sub = grant(db, shop, utcnow() - timedelta(days=5))
        request_at(db, world, shop, product, world.customer(1, "9811200111"), utcnow() - timedelta(days=1))
        mine = client.get("/api/owner/plan", headers=HEADERS).json()
    assert mine["plan"]["code"] == "growth" and mine["plan"]["subscription_id"] == sub.id
    assert mine["subscription"]["founding"] and mine["subscription"]["price_paise"] == 99_900
    assert mine["founding"]["already_claimed"] and not mine["founding"]["eligible"]
    g = mine["guarantee"]
    assert g["applies"] and (g["customer_requests"], g["needed"], g["met"]) == (1, 3, False)
    assert mine["plan"]["ends_at"] == sub.ends_at.isoformat() + "Z"
