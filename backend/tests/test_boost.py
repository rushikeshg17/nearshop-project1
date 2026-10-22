"""Boost packs: the sponsored slot, city eligibility, pausing, the make-good, the discount and attribution (P10.12;
app/services/boost.py; plan section 8.22).

The tests build their own world far from the shared seed (its own categories, catalogue items, shops and customers,
around WORLD), so no other test's search sees it, and remove everything they wrote afterwards. Thresholds are set per
test with monkeypatch; nothing here reaches a payment provider: payments are matched by hand as an admin would."""
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.main import app
from app.models import CatalogItem, Category, Product, Role, Shop, User
from app.models.billing import Actor
from app.models.core import VerificationStatus, months_after
from app.services import billing, boost, plans
from app.services import search as search_module
from app.services.loaders import reset_category_cache
from app.services.search import SearchParams, run_search
from database.migrations import m0011_plan_catalogue
from tests.conftest import HEADERS, PASSWORD, login

WORLD = (14.9000, 75.9000)  # far from the shared seed's CENTER
CITY = "Boostpur"
PW_HASH = hash_password(PASSWORD)
ADMIN = Actor(type="admin", id=0)
QUERY = "zorblex cable"


class World:
    """Shops around WORLD selling two catalogue items of one category and one of another, removed afterwards."""

    def __init__(self, db: Database):
        self.db = db
        self.tag = uuid.uuid4().hex[:8]
        self.users: list[int] = []
        self.shops: list[int] = []
        self.products: list[int] = []
        self.cat = db.categories.insert(Category(slug=f"boost-cables-{self.tag}", name=f"Boost Cables {self.tag}",
                                                 icon="Cable", color_hue=10))
        self.other_cat = db.categories.insert(Category(slug=f"boost-tapes-{self.tag}", name=f"Boost Tapes {self.tag}",
                                                       icon="Droplets", color_hue=20))
        self.cable = db.catalog_items.insert(CatalogItem(
            slug=f"zorblex-cable-{self.tag}", name="Zorblex USB-C Cable", brand="Zorblex", category_id=self.cat.id,
            subcategory="cables", typical_price=300, mrp=400, tags=["zorblex cable", "usb cable"], icon="Cable"))
        self.charger = db.catalog_items.insert(CatalogItem(
            slug=f"zorblex-charger-{self.tag}", name="Zorblex Cable Charger Kit", brand="Zorblex",
            category_id=self.cat.id, subcategory="chargers", typical_price=900, mrp=1100,
            tags=["zorblex cable charger"], icon="PlugZap"))
        self.tape = db.catalog_items.insert(CatalogItem(
            slug=f"zorblex-tape-{self.tag}", name="Zorblex Thread Tape", brand="Zorblex", category_id=self.other_cat.id,
            subcategory="sealants", typical_price=40, mrp=50, tags=["zorblex tape"], icon="Droplets"))
        reset_category_cache()  # categories are cached per process

    def user(self, role: Role, n: int) -> User:
        u = self.db.users.insert(User(name=f"Boost {role.value} {n}", email=f"boost-{role.value}{n}-{self.tag}@example.com",
                                      password_hash=PW_HASH, role=role))
        self.users.append(u.id)
        return u

    def shop(self, n: int, *, dlat: float = 0.002, verified: bool = True, city: str = CITY) -> Shop:
        owner = self.user(Role.OWNER, n)
        now = utcnow()
        fields = ({"verification_status": VerificationStatus.VERIFIED, "is_verified": True, "kyc_type": "udyam",
                   "kyc_number": f"UDYAM-KR-23-{7000000 + n:07d}", "phone_verified_at": now, "submitted_at": now,
                   "decided_at": now, "verified_until": months_after(now)} if verified else {})
        shop = self.db.shops.insert(Shop(
            owner_id=owner.id, name=f"Boost Shop {n} {self.tag}", slug=f"boost-shop-{n}-{self.tag}",
            address_line=f"{n} Boost Road", city=city, lat=WORLD[0] + dlat * n, lng=WORLD[1],
            category_ids=[self.cat.id, self.other_cat.id], **fields))
        self.shops.append(shop.id)
        return shop

    def listing(self, shop: Shop, item: CatalogItem, price: float, qty: int = 5,
                updated: datetime | None = None) -> Product:
        p = self.db.products.insert(Product(
            shop_id=shop.id, catalog_item_id=item.id, category_id=item.category_id, name=item.name, brand=item.brand,
            keywords=" ".join(item.tags), icon=item.icon, price=price, mrp=item.mrp, quantity=qty,
            stock_updated_at=updated or utcnow()))
        self.products.append(p.id)
        return p

    def cleanup(self) -> None:
        db = self.db
        pack_ids = [p["_id"] for p in db.mongo[boost.PACKS].find({"shop_id": {"$in": self.shops}}, {"_id": 1})]
        db.mongo[boost.PACKS].delete_many({"shop_id": {"$in": self.shops}})
        db.mongo[boost.STATS].delete_many({"pack_id": {"$in": pack_ids}})
        db.mongo[boost.CITY_DAYS].delete_many({})  # the daily check also wrote rows for the shared seed's cities
        for name in (billing.REQUESTS, billing.PAYMENTS):
            db.mongo[name].delete_many({"shop_id": {"$in": self.shops}})
        db.mongo[billing.ACCOUNTS].delete_many({"contact_user_id": {"$in": self.users}})
        db.mongo[plans.SUBSCRIPTIONS].delete_many({"shop_ids": {"$in": self.shops}})
        db.reservations.delete_many({"shop_id": {"$in": self.shops}})
        db.orders.delete_many({"shop_id": {"$in": self.shops}})
        db.notifications.delete_many({"user_id": {"$in": self.users}})
        db.products.delete_many({"_id": {"$in": self.products}})
        db.shops.delete_many({"_id": {"$in": self.shops}})
        db.users.delete_many({"_id": {"$in": self.users}})
        db.catalog_items.delete_many({"_id": {"$in": [self.cable.id, self.charger.id, self.tape.id]}})
        db.categories.delete_many({"_id": {"$in": [self.cat.id, self.other_cat.id]}})
        reset_category_cache()


@pytest.fixture
def world(ids):
    boost.reset_rotation()
    w = World(Database())
    yield w
    w.cleanup()
    boost.reset_rotation()


@pytest.fixture
def market(world):
    """Four verified shops near WORLD, each selling the cable and the charger, in stock and fresh."""
    shops = [world.shop(n) for n in range(1, 5)]
    listings = {s.id: (world.listing(s, world.cable, 290 + s.id % 7), world.listing(s, world.charger, 880))
                for s in shops}
    return shops, listings


def params(**kw) -> SearchParams:
    return SearchParams(**{"q": QUERY, "lat": WORLD[0], "lng": WORLD[1], "radius_km": 5.0, **kw})


def search(db: Database, now: datetime, **kw) -> dict:
    return run_search(db, params(**kw), log=False, now=now)


def plant_pack(db: Database, shop: Shop, groups: list[tuple[str, int]], *, status: str = "active",
               now: datetime | None = None, days: int = 30, **fields) -> int:
    """A pack written directly, as if bought and paid for: serving from now - 1 day for `days` days."""
    now = now or utcnow()
    pack = boost.BoostPack(
        id=db.next_id(boost.PACKS), shop_id=shop.id, owner_id=shop.owner_id, city=boost.city_key(shop.city),
        kind="standard", groups=[boost.ItemGroup(type=t, id=i, name=f"group {i}") for t, i in groups], days=days,
        list_price_paise=49_900, amount_paise=49_900, plan_code="free", status=status,
        period_start=now - timedelta(days=1), ends_at=now - timedelta(days=1) + timedelta(days=days),
        created_at=now, updated_at=now)
    for k, v in fields.items():
        setattr(pack, k, v)
    boost.packs(db).insert(pack)
    return pack.id


def organic(result: dict) -> dict:
    return {k: v for k, v in result.items() if k != "sponsored"}


def plant_city_days(db: Database, now: datetime, *, customers: int = 5_000, shops: int = 150, age: float = 20.0,
                    days: int = 28, skip: set[int] = frozenset()) -> None:
    """Daily eligibility rows for CITY over the last `days` IST days (skipping the day offsets in skip)."""
    today = boost._ist_day(now)
    key = boost.city_key(CITY)
    for back in range(days):
        if back in skip:
            continue
        day = (today - timedelta(days=back)).isoformat()
        db.mongo[boost.CITY_DAYS].replace_one({"_id": f"{key}:{day}"}, {
            "_id": f"{key}:{day}", "city": key, "name": CITY, "day": day, "verified_shops": shops,
            "monthly_customers": customers, "median_stock_age_hours": age, "checked_at": now}, upsert=True)


def wait_for(check, timeout: float = 5.0):
    """Background writes (impressions, clicks, attribution) land shortly after the response."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.05)


def stats(db: Database, pack_id: int) -> dict:
    out = {f: 0 for f in boost.STAT_FIELDS}
    for r in db.mongo[boost.STATS].find({"pack_id": pack_id}):
        for f in boost.STAT_FIELDS:
            out[f] += int(r.get(f) or 0)
    return out


# ---------------------------------------------------------------- organic results are untouched


def test_with_no_pack_results_are_identical_to_search_without_boost(world, market, monkeypatch):
    db, now = Database(), utcnow()
    with_boost = search(db, now)
    monkeypatch.setattr(search_module, "_sponsored", lambda *a, **k: None)
    without = search(db, now)
    assert with_boost == without and "sponsored" not in with_boost
    assert with_boost["total"] == 2  # the cable and the charger kit, grouped across the four shops


def test_a_pack_that_cannot_serve_changes_nothing(world, market):
    db, now = Database(), utcnow()
    shops, _ = market
    before = search(db, now)
    plant_pack(db, shops[0], [("category", world.other_cat.id)])  # tape only: does not match "zorblex cable"
    plant_pack(db, shops[1], [("category", world.cat.id)], status="pending")
    plant_pack(db, shops[2], [("category", world.cat.id)], status="paused")
    plant_pack(db, shops[3], [("category", world.cat.id)], status="ended")
    assert search(db, now) == before


def test_with_packs_the_organic_list_is_identical_and_one_card_sits_at_position_3(world, market):
    db, now = Database(), utcnow()
    shops, listings = market
    before = search(db, now)
    for s in shops[:3]:
        plant_pack(db, s, [("item", world.cable.id)])
    after = search(db, now)
    assert organic(after) == before  # groups, order, scores, counts, pins: all as without Boost
    card = after["sponsored"]
    assert card["sponsored"] is True and card["label"] == "Sponsored"
    assert card["position"] == min(settings.boost_slot_position, len(after["groups"]) + 1) == 3
    assert card["listing"]["id"] in {listings[s.id][0].id for s in shops[:3]}
    assert card["listing"]["shop"]["id"] in {s.id for s in shops[:3]}
    # Later pages and a shop's own page carry no card.
    assert "sponsored" not in search(db, now, page=2, page_size=1)
    assert "sponsored" not in search(db, now, shop_id=shops[0].id)


def test_rotation_is_equal_and_never_the_same_shop_twice(world, market):
    db, now = Database(), utcnow()
    shops, _ = market
    for s in shops[:3]:
        plant_pack(db, s, [("category", world.cat.id)])
    # A fourth shop with two packs: rotation is per pack, and a page still shows one card.
    plant_pack(db, shops[3], [("item", world.cable.id)])
    plant_pack(db, shops[3], [("item", world.charger.id)])
    seen: dict[int, int] = {}
    for _ in range(50):
        result = search(db, now)
        cards = [result["sponsored"]]
        assert len(cards) == 1
        token = boost.read_token(cards[0]["token"])
        assert token is not None
        seen[token[0]] = seen.get(token[0], 0) + 1
    assert sorted(seen.values()) == [10, 10, 10, 10, 10]


# ---------------------------------------------------------------- the slot's rules


def test_the_slot_needs_fresh_in_stock_listings_of_a_verified_shop_in_radius(world):
    db, now = Database(), utcnow()
    fresh_shop, stale_shop, empty_shop = world.shop(1), world.shop(2), world.shop(3)
    unverified = world.shop(4, verified=False)
    far = world.shop(5, dlat=0.03)  # 5 x 0.03 degrees: about 16 km away
    world.listing(fresh_shop, world.cable, 300)
    world.listing(stale_shop, world.cable, 300, updated=now - timedelta(hours=settings.boost_fresh_hours + 1))
    world.listing(empty_shop, world.cable, 300, qty=0)
    world.listing(unverified, world.cable, 300)
    world.listing(far, world.cable, 300)
    for s in (stale_shop, empty_shop, unverified, far):
        plant_pack(db, s, [("category", world.cat.id)])
    # Stale (confirmed 49 hours ago), out of stock, unverified, and outside the 5 km radius: no card.
    assert "sponsored" not in search(db, now)
    # With a 25 km radius the far shop's fresh listing is in radius, and serves.
    assert search(db, now, radius_km=25.0)["sponsored"]["listing"]["shop"]["id"] == far.id
    # Updated within 48 hours: serves.
    pack = plant_pack(db, fresh_shop, [("category", world.cat.id)])
    result = search(db, now)
    assert result["sponsored"]["listing"]["shop"]["id"] == fresh_shop.id
    assert boost.read_token(result["sponsored"]["token"])[0] == pack


def test_no_card_when_a_filter_or_the_query_excludes_the_listing(world):
    db, now = Database(), utcnow()
    shop = world.shop(1)
    world.listing(shop, world.cable, 300)
    world.listing(shop, world.tape, 40, qty=0)
    plant_pack(db, shop, [("item", world.tape.id)])
    # The tape is out of stock: no card, with or without the in-stock filter.
    assert "sponsored" not in search(db, now, q="zorblex tape")
    assert "sponsored" not in search(db, now, q="zorblex tape", in_stock_only=True)
    # The cable pack: shown for its query, not for another category's filter or a price that excludes it.
    plant_pack(db, shop, [("item", world.cable.id)])
    assert "sponsored" in search(db, now, in_stock_only=True)
    assert "sponsored" not in search(db, now, category=world.other_cat.slug)
    assert "sponsored" not in search(db, now, max_price=100)
    assert "sponsored" not in search(db, now, q="")  # browsing with no query and no category


def test_a_search_that_logs_counts_an_impression(world):
    db, now = Database(), utcnow()
    shop = world.shop(1)
    world.listing(shop, world.cable, 300)
    pack = plant_pack(db, shop, [("item", world.cable.id)])
    run_search(db, params(), log=True, now=now)
    assert wait_for(lambda: stats(db, pack)["impressions"] == 1)
    search(db, now)  # the evaluation harness and other unlogged searches count nothing
    time.sleep(0.2)
    assert stats(db, pack)["impressions"] == 1


# ---------------------------------------------------------------- eligibility, pausing and the days added back


def test_city_eligibility_needs_every_threshold_and_four_weeks_of_customers(world, monkeypatch):
    db, now = Database(), utcnow()
    key = boost.city_key(CITY)
    assert boost.city_eligibility(db, key, now)["reasons"] == ["no_recent_check", "customer_weeks"]
    plant_city_days(db, now)
    assert boost.city_eligibility(db, key, now)["eligible"] is True
    plant_city_days(db, now, days=1, shops=99)
    assert boost.city_eligibility(db, key, now)["reasons"] == ["verified_shops"]
    plant_city_days(db, now, days=1, age=48.5)
    assert boost.city_eligibility(db, key, now)["reasons"] == ["median_stock_age"]
    plant_city_days(db, now)
    db.mongo[boost.CITY_DAYS].update_one({"_id": f"{key}:{(boost._ist_day(now) - timedelta(days=20)).isoformat()}"},
                                         {"$set": {"monthly_customers": 2_999}})
    assert boost.city_eligibility(db, key, now)["reasons"] == ["monthly_customers"]
    db.mongo[boost.CITY_DAYS].delete_many({"city": key})
    plant_city_days(db, now, skip=set(range(14, 21)))  # the third week back has no check
    assert boost.city_eligibility(db, key, now)["reasons"] == ["customer_weeks"]


def test_the_daily_check_measures_shops_customers_and_stock_age(world, monkeypatch):
    db, now = Database(), utcnow()
    shops = [world.shop(n) for n in range(1, 4)]
    world.shop(4, verified=False)
    for i, s in enumerate(shops):
        world.listing(s, world.cable, 300, updated=now - timedelta(hours=10 * (i + 1)))  # 10, 20, 30 hours
    seen = world.user(Role.CUSTOMER, 1)
    db.users.update_one({"_id": seen.id}, {"$set": {"last_login_at": now - timedelta(days=3)}})
    old = world.user(Role.CUSTOMER, 2)
    db.users.update_one({"_id": old.id}, {"$set": {"last_login_at": now - timedelta(days=40)}})
    # A reservation in this city places the customer here (the deployment's city is not CITY).
    from app.models import Reservation

    db.reservations.insert(Reservation(code=f"RB{world.tag}", customer_id=seen.id, shop_id=shops[0].id,
                                       product_id=world.products[0], quantity=1, unit_price=300,
                                       expires_at=now + timedelta(hours=1), created_at=now - timedelta(days=1)))
    figures = boost.measure_cities(db, now)[boost.city_key(CITY)]
    assert figures["verified_shops"] == 3 and figures["monthly_customers"] == 1
    assert figures["median_stock_age_hours"] == pytest.approx(20.0, abs=0.1)
    rows = boost.record_city_days(db, now)
    assert any(r["city"] == boost.city_key(CITY) for r in rows)


def test_packs_pause_below_a_threshold_and_resume_with_the_days_added_back(world, monkeypatch):
    db, now = Database(), utcnow()
    shop = world.shop(1)
    world.listing(shop, world.cable, 300)
    pack_id = plant_pack(db, shop, [("item", world.cable.id)], now=now)
    ends_at = boost.packs(db).get(pack_id).ends_at
    plant_city_days(db, now, customers=100)  # below 3,000
    assert boost.apply_eligibility(db, now) == {"paused": 1, "resumed": 0}
    paused = boost.packs(db).get(pack_id)
    assert paused.status == "paused" and paused.resume_status == "active"
    assert "sponsored" not in search(db, now)
    later = now + timedelta(days=3)
    plant_city_days(db, later)
    assert boost.apply_eligibility(db, later) == {"paused": 0, "resumed": 1}
    resumed = boost.packs(db).get(pack_id)
    assert resumed.status == "active" and resumed.ends_at == ends_at + timedelta(days=3)
    assert resumed.paused_seconds == 3 * 86400
    # The daily job does both: today's figures (here, the real measure: this city has one shop) pause it again.
    monkeypatch.setattr(settings, "boost_min_verified_shops", 2)
    assert boost.run_daily(db, later)["paused"] == 1


def test_no_pack_is_sold_while_sales_are_off_or_the_city_is_not_eligible(world, monkeypatch):
    db, now = Database(), utcnow()
    shop = world.shop(1)
    monkeypatch.setattr(settings, "billing_upi_id", "nearshop-test@testbank")
    monkeypatch.setattr(settings, "billing_payee_name", "NearShop Test")
    with TestClient(app) as c:
        owner = login(c, db.users.get(shop.owner_id).email)
        body = {"kind": "standard", "groups": [{"type": "category", "id": world.cat.id}]}
        res = owner.post("/api/owner/boost/packs", json=body, headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "boost_sales_off"
        assert owner.get("/api/owner/boost/eligibility", headers=HEADERS).json()["reasons"] == [
            "sales_off", "city_no_recent_check", "city_customer_weeks"]
        monkeypatch.setattr(settings, "boost_sales_enabled", True)
        res = owner.post("/api/owner/boost/packs", json=body, headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "boost_city_not_eligible"
        plant_city_days(db, now)
        too_many = {"kind": "standard", "groups": [{"type": "category", "id": world.cat.id},
                                                   {"type": "item", "id": world.cable.id},
                                                   {"type": "item", "id": world.charger.id},
                                                   {"type": "item", "id": world.tape.id}]}
        res = owner.post("/api/owner/boost/packs", json=too_many, headers=HEADERS)
        assert res.status_code == 409 and res.json()["error"]["code"] == "boost_groups"
        eligibility = owner.get("/api/owner/boost/eligibility", headers=HEADERS).json()
        assert eligibility["can_buy"] is True and eligibility["reasons"] == []
        res = owner.post("/api/owner/boost/packs", json=body, headers=HEADERS)
        assert res.status_code == 200, res.text
        assert res.json()["pack"]["status"] == "pending"
        assert res.json()["payment_request"]["purpose"] == "boost"


# ---------------------------------------------------------------- the discount, payment and the make-good


def test_growth_takes_25_percent_off_and_paying_starts_the_pack(world, monkeypatch):
    db, now = Database(), utcnow()
    m0011_plan_catalogue.apply(db.mongo)  # idempotent: the plan catalogue (Growth: boost_discount_pct 25)
    monkeypatch.setattr(settings, "billing_upi_id", "nearshop-test@testbank")
    monkeypatch.setattr(settings, "billing_payee_name", "NearShop Test")
    monkeypatch.setattr(settings, "boost_sales_enabled", True)
    free_shop, growth_shop = world.shop(1), world.shop(2)
    world.listing(growth_shop, world.cable, 300)
    plans.create_subscription(db, shop_ids=[growth_shop.id], plan_code="growth", period="year", source="sponsored",
                              actor=ADMIN, reason="A sponsored seat for the Boost test", months=12)
    assert boost.quote(db, free_shop, "standard")["amount_paise"] == 49_900
    assert boost.quote(db, growth_shop, "standard")["amount_paise"] == 37_425  # 25% off 499.00
    assert boost.quote(db, growth_shop, "festive")["amount_paise"] == 74_925  # 25% off 999.00
    plant_city_days(db, now)
    first, req1 = boost.buy(db, growth_shop, kind="standard", groups=[("item", world.cable.id)],
                            actor=Actor(type="owner", id=growth_shop.owner_id))
    pack, req = boost.buy(db, growth_shop, kind="standard", groups=[("item", world.cable.id)],
                          actor=Actor(type="owner", id=growth_shop.owner_id))
    # A newer purchase replaces the pending one and cancels its request.
    assert boost.packs(db).get(first.id).status == "ended" and boost.packs(db).get(first.id).end_reason == "replaced"
    assert billing.requests(db).get(req1.id).status == "cancelled"
    assert (req.purpose, req.amount_paise, req.total_paise, req.boost_pack_id) == ("boost", 37_425, 37_425, pack.id)
    pay = billing.match_by_hand(db, upi_reference=f"BOOST{world.tag}", request_number=req.number,
                                reason="Matched from the bank statement by hand", actor=ADMIN,
                                amount_paise=req.total_paise, credited_on=boost._ist_day(now), now=now)
    active = boost.packs(db).get(pack.id)
    assert (pay.boost_pack_id, pay.subscription_id) == (pack.id, None)
    assert active.status == "active" and active.payment_id == pay.id
    assert active.ends_at - active.period_start == timedelta(days=settings.boost_pack_days)
    assert "Boost standard pack" in billing.documents(db).get(pay.document_id).description
    assert "sponsored" in search(db, now + timedelta(seconds=1))
    # A refund ends the pack at once.
    billing.refund_payment(db, pay.id, reason="Refunded on request", bank_reference=f"RF{world.tag}", actor=ADMIN,
                           now=now)
    assert boost.packs(db).get(pack.id).end_reason == "refunded"
    db.mongo[billing.DOCUMENTS].delete_many({"payment_id": pay.id})
    db.mongo[billing.CREDITS].delete_many({"upi_reference": f"BOOST{world.tag}"})


def test_a_period_with_no_requests_earns_one_free_period(world):
    db, now = Database(), utcnow()
    shop = world.shop(1)
    quiet = plant_pack(db, shop, [("item", world.cable.id)], now=now - timedelta(days=30), days=30)
    busy = plant_pack(db, shop, [("item", world.cable.id)], now=now - timedelta(days=30), days=30, period_requests=2,
                      requests_total=2)
    old_end = boost.packs(db).get(quiet).ends_at
    assert boost.end_periods(db, now) == {"made_good": 1, "ended": 1}
    made_good = boost.packs(db).get(quiet)
    assert made_good.status == "make_good" and made_good.make_good_used is True
    assert (made_good.period_start, made_good.ends_at) == (old_end, old_end + timedelta(days=30))
    assert boost.packs(db).get(busy).status == "ended"
    # The free period serves like a paid one, and ends, with no second make-good, even with 0 requests again.
    world.listing(shop, world.cable, 300)
    assert "sponsored" in search(db, now)
    assert boost.end_periods(db, made_good.ends_at) == {"made_good": 0, "ended": 1}
    assert boost.packs(db).get(quiet).status == "ended"


# ---------------------------------------------------------------- attribution


def _customer(world: World, n: int) -> TestClient:
    user = world.user(Role.CUSTOMER, n)
    c = TestClient(app)
    c.__enter__()
    return login(c, user.email)


def test_attribution_counts_a_request_in_the_same_session_and_day_only(world, market):
    db = Database()
    shops, listings = market
    pack = plant_pack(db, shops[0], [("item", world.cable.id)])
    cable_0, charger_0 = listings[shops[0].id]
    cable_1, _ = listings[shops[1].id]
    loc = {"lat": WORLD[0], "lng": WORLD[1], "radius_km": 5}
    clicked, other = _customer(world, 1), _customer(world, 2)
    try:
        card = clicked.get("/api/search", params={"q": QUERY, **loc}).json()["sponsored"]
        assert card["listing"]["shop"]["id"] == shops[0].id
        assert wait_for(lambda: stats(db, pack)["impressions"] == 1)
        assert clicked.post("/api/search/sponsored-click", json={"token": card["token"]},
                            headers=HEADERS).status_code == 204
        assert stats(db, pack)["clicks"] == 1 and boost.COOKIE in clicked.cookies
        # Another shop's listing in the same item group, and the right shop's other item group: no request.
        assert clicked.post("/api/reservations", json={"product_id": cable_1.id, "quantity": 1},
                            headers=HEADERS).status_code == 200
        assert clicked.post("/api/reservations", json={"product_id": charger_0.id, "quantity": 1},
                            headers=HEADERS).status_code == 200
        # Another browser session, with no click: no request either.
        assert other.post("/api/reservations", json={"product_id": cable_0.id, "quantity": 1},
                          headers=HEADERS).status_code == 200
        time.sleep(0.3)
        assert stats(db, pack)["requests"] == 0
        # The pack's item group at the pack's shop, in the session that clicked: one request.
        assert clicked.post("/api/reservations", json={"product_id": cable_0.id, "quantity": 1},
                            headers=HEADERS).status_code == 200
        assert wait_for(lambda: stats(db, pack)["requests"] == 1)
        assert boost.packs(db).get(pack).period_requests == 1
        # A click remembered from yesterday (IST) counts nothing today.
        yesterday = boost._epoch(utcnow() - timedelta(days=1, hours=1))
        other.cookies.set(boost.COOKIE, boost._cookie_value([(pack, yesterday)]), path="/api")
        db.reservations.delete_many({"product_id": cable_0.id})
        assert other.post("/api/reservations", json={"product_id": cable_0.id, "quantity": 1},
                          headers=HEADERS).status_code == 200
        time.sleep(0.3)
        assert stats(db, pack)["requests"] == 1
        # A forged cookie or token counts nothing.
        other.cookies.set(boost.COOKIE, f"{pack}.{boost._epoch(utcnow())}.forged", path="/api")
        assert boost._cookie_entries(other.cookies.get(boost.COOKIE)) == []
        assert other.post("/api/search/sponsored-click", json={"token": card["token"][:-2] + "xx"},
                          headers=HEADERS).status_code == 204
        assert stats(db, pack)["clicks"] == 1
        # The owner sees impressions, clicks and requests per pack.
        with TestClient(app) as c:
            owner = login(c, db.users.get(shops[0].owner_id).email)
            [row] = owner.get("/api/owner/boost/packs", headers=HEADERS).json()["packs"]
            assert row["id"] == pack and row["totals"] == {"impressions": 1, "clicks": 1, "requests": 1}
            assert len(row["by_day"]) == 1 and row["by_day"][0]["day"] == boost._ist_day(utcnow()).isoformat()
    finally:
        clicked.__exit__(None, None, None)
        other.__exit__(None, None, None)


def test_a_click_token_expires(world):
    issued = utcnow() - timedelta(minutes=settings.boost_token_minutes + 1)
    assert boost.read_token(boost.token(1, 2, issued)) is None
    assert boost.read_token(boost.token(1, 2)) == (1, 2)
    assert boost.read_token("b1.1.2.3.nope") is None
