"""Gate KPIs (P9.12; app/services/kpis.py; plan section 8.23).

Each test builds its own town (a city name no other test uses) and reads the figures at AT, 15 Jun 2033 12:00 IST,
so the month is May 2033 and no other test's data is in it. Thresholds are moved with monkeypatch to sit exactly on
the controlled data: each figure is checked at its boundary (pass) and one step beyond it (fail). Everything a test
writes is removed afterwards."""
import secrets
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import jobs
from app.core.config import settings
from app.core.database import Database
from app.core.security import hash_password
from app.models import Order, OrderItem, Product, Reservation, Role, Shop, User
from app.models.billing import Actor, Period, PlanCode, Subscription, SubscriptionEvent, SubscriptionSource
from app.models.billing import SubscriptionStatus as SS
from app.models.core import VerificationStatus
from app.models.enums import OrderStatus, ReservationStatus
from app.services import kpis, leads, plans, statements
from app.services.reservations import NOT_AVAILABLE_ON_ARRIVAL
from tests.conftest import CENTER, HEADERS, PASSWORD

IST = ZoneInfo("Asia/Kolkata")


def ist(*args: int) -> datetime:
    """An IST wall-clock time as the naive UTC time the database stores."""
    return datetime(*args, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


AT = ist(2033, 6, 15, 12, 0)  # a Wednesday; the month is May 2033
LIVE_UNTIL = datetime(2040, 1, 1)
MAY = leads.month_bounds(2033, 5)
APRIL = leads.month_bounds(2033, 4)


class Town:
    def __init__(self, db: Database):
        self.db = db
        self.tag = secrets.token_hex(4)
        self.city = f"Kpi Town {self.tag}"
        self.users: list[int] = []
        self.shops: list[int] = []
        self.n = 0

    def _user(self, role: Role) -> User:
        self.n += 1
        u = self.db.users.insert(User(name=f"Kpi {role.value} {self.n}", email=f"kpi-{self.tag}-{self.n}@example.com",
                                      password_hash=hash_password(PASSWORD), role=role))
        self.users.append(u.id)
        return u

    def customer(self) -> User:
        return self._user(Role.CUSTOMER)

    def shop(self, age_hours: float = 24, verified: bool = True, city: str | None = None) -> Shop:
        """A shop with one active listing whose stock was counted age_hours before AT."""
        owner = self._user(Role.OWNER)
        decided = AT - timedelta(days=60)
        fields = dict(verification_status=VerificationStatus.VERIFIED, is_verified=True, kyc_type="udyam",
                      kyc_number=f"UDYAM-KR-24-{int(self.tag, 16) % 10**5:05d}{len(self.shops):02d}",
                      phone_verified_at=decided, submitted_at=decided, decided_at=decided,
                      verified_until=LIVE_UNTIL) if verified else {}
        shop = self.db.shops.insert(Shop(
            owner_id=owner.id, name=f"Kpi shop {len(self.shops)}", slug=f"kpi-{self.tag}-{len(self.shops)}",
            address_line="1 Gate Road", city=city or self.city, lat=CENTER[0] + 2, lng=CENTER[1], category_ids=[],
            **fields))
        self.shops.append(shop.id)
        counted = AT - timedelta(hours=age_hours)
        category = self.db.categories.find_one()
        self.db.products.insert(Product(shop_id=shop.id, category_id=category.id, name="Kpi item", keywords="item",
                                        price=100, quantity=5, stock_updated_at=counted, created_at=counted,
                                        updated_at=counted))
        return shop

    def reservation(self, shop: Shop, customer: User | None = None, *, created: datetime,
                    status: ReservationStatus = ReservationStatus.REQUESTED, completed: datetime | None = None,
                    close_reason: str | None = None, closed: datetime | None = None) -> Reservation:
        customer = customer or self.customer()
        return self.db.reservations.insert(Reservation(
            code=f"K{secrets.token_hex(5).upper()}", customer_id=customer.id, shop_id=shop.id,
            product_id=10**8 + self.db.next_id("kpi_test_products"), quantity=1, unit_price=100.0, status=status,
            expires_at=created + timedelta(minutes=30), completed_at=completed, closed_at=closed,
            close_reason=close_reason, arrival_reported_at=closed if close_reason == NOT_AVAILABLE_ON_ARRIVAL else None,
            created_at=created, updated_at=created))

    def order(self, shop: Shop, customer: User | None = None, *, created: datetime,
              delivered: datetime | None = None) -> Order:
        customer = customer or self.customer()
        return self.db.orders.insert(Order(
            code=f"O{secrets.token_hex(5).upper()}", customer_id=customer.id, shop_id=shop.id,
            status=OrderStatus.DELIVERED if delivered else OrderStatus.PENDING, delivery_address="2 Gate Road",
            delivery_lat=CENTER[0], delivery_lng=CENTER[1], contact_phone="9000000000", distance_km=1.0,
            subtotal=100, delivery_fee=0, total=100, delivered_at=delivered, created_at=created, updated_at=created,
            items=[OrderItem(product_id=10**8 + self.n, name="Kpi item", unit_price=100, quantity=1)]))

    def subscription(self, shop: Shop, *, source: SubscriptionSource, status: SS, starts: datetime, ends: datetime,
                     founding: bool = False, paid_at: datetime | None = None) -> Subscription:
        plan = plans.catalogue(self.db, AT)["growth"]
        paid = source in (SubscriptionSource.SHOP_PAID, SubscriptionSource.ADVANCE)
        return plans.subscriptions(self.db).insert(Subscription(
            account_id=shop.owner_id, shop_ids=[shop.id], plan_id=plan.id, plan_code=PlanCode.GROWTH,
            plan_version=plan.version, period=Period.YEAR, price_paise=99_900 if founding else 149_900,
            founding=founding, founding_slot=900_000 + shop.id if founding else None,
            founding_shop_id=shop.id if founding else None, source=source, status=status, starts_at=starts,
            ends_at=ends, paid_until=ends, paid_at=(paid_at or starts) if paid else None, created_at=starts,
            updated_at=starts))

    def renewed(self, sub: Subscription, *, at: datetime, ends_before: datetime) -> None:
        plans.events(self.db).insert(SubscriptionEvent(
            subscription_id=sub.id, shop_ids=sub.shop_ids, kind="renewed", actor=Actor(type="system"),
            reason="Renewed in a test", status_before=SS.ACTIVE, status_after=SS.ACTIVE,
            ends_at_before=ends_before, ends_at_after=ends_before + timedelta(days=365), at=at))

    def statement(self, shop: Shop, month: str, *replies: tuple[str, datetime]) -> None:
        sid = self.db.next_id(statements.STATEMENTS)
        self.db.mongo[statements.STATEMENTS].insert_one({
            "_id": sid, "shop_id": shop.id, "owner_id": shop.owner_id, "month": month, "kind": statements.SHADOW_BILL,
            "counts": {f: 0 for f in leads.COUNT_FIELDS}, "language": "en", "delivery": "not_sent",
            "built_at": AT, "keep_until": LIVE_UNTIL})
        for kind, at in replies:
            self.db.mongo[statements.REPLIES].insert_one({
                "_id": self.db.next_id(statements.REPLIES), "statement_id": sid, "shop_id": shop.id, "month": month,
                "kind": kind, "at": at, "keep_until": LIVE_UNTIL})

    def figures(self, at: datetime = AT) -> dict[str, dict]:
        return {f["key"]: f for f in kpis.compute(self.db, at, self.city)["figures"]}

    def cleanup(self) -> None:
        db = self.db
        shops = {"shop_id": {"$in": self.shops}}
        db.reservations.delete_many(shops)
        db.orders.delete_many(shops)
        db.products.delete_many(shops)
        db.mongo[statements.STATEMENTS].delete_many(shops)
        db.mongo[statements.REPLIES].delete_many(shops)
        subs = [s["_id"] for s in db.mongo[plans.SUBSCRIPTIONS].find({"shop_ids": {"$in": self.shops}}, {"_id": 1})]
        db.mongo[plans.EVENTS].delete_many({"subscription_id": {"$in": subs}})
        db.mongo[plans.SUBSCRIPTIONS].delete_many({"_id": {"$in": subs}})
        db.mongo[kpis.DAYS].delete_many({"city": kpis.city_key(self.city)})
        db.shops.delete_many({"_id": {"$in": self.shops}})
        db.users.delete_many({"_id": {"$in": self.users}})


@pytest.fixture
def town(ids):
    t = Town(Database())
    yield t
    t.cleanup()


@pytest.fixture
def clean_kpis(ids):
    db = Database()
    for name in (kpis.SNAPSHOTS, kpis.INPUTS):
        db.mongo[name].delete_many({})
    yield db
    for name in (kpis.SNAPSHOTS, kpis.INPUTS):
        db.mongo[name].delete_many({})


def judged(fig: dict) -> tuple[float | None, bool | None]:
    return fig["value"], fig["pass"]


# ---------------------------------------------------------------- gates and thresholds


def test_each_figure_is_judged_against_its_next_gate():
    before_g4 = date(2027, 7, 1)
    t = kpis.next_threshold("active_shops", before_g4)
    assert (t["gate"], t["op"], t["value"]) == ("g4", ">=", settings.kpi_g4_min_active_shops)
    t = kpis.next_threshold("active_shops", date(2027, 7, 16))
    assert (t["gate"], t["value"]) == ("ballari", settings.kpi_ballari_min_active_shops)
    assert kpis.next_threshold("accurate_pct", date(2027, 7, 20))["gate"] == "readout"
    # After its last gate a figure stays judged against that gate's value.
    assert kpis.next_threshold("completions_90d", date(2030, 1, 1))["gate"] == "g4"
    assert kpis.next_threshold("accurate_pct", date(2030, 1, 1))["gate"] == "readout"
    assert kpis.next_threshold("sponsored_seats", before_g4) is None
    # Every figure but the separately shown sponsored seats has a threshold, from settings.
    assert set(kpis.THRESHOLDS) == set(kpis.FIGURES) - {"sponsored_seats"}
    for rows in kpis.THRESHOLDS.values():
        for gate, op, attr in rows:
            assert gate in {g for g, _, _ in kpis.GATES} and op in (">=", "<=") and hasattr(settings, attr)
    at_least = {"op": ">=", "value": 10}
    at_most = {"op": "<=", "value": 10}
    assert kpis.judge(10, at_least) and not kpis.judge(9.99, at_least)
    assert kpis.judge(10, at_most) and not kpis.judge(10.01, at_most)
    assert kpis.judge(None, at_most) is None


# ---------------------------------------------------------------- supply


def test_active_shops_and_median_stock_age_at_their_boundaries(town, monkeypatch):
    for age in (10, 48, 48):
        town.shop(age_hours=age)
    town.shop(age_hours=20 * 24)  # verified and live, but no listing changed in 14 days: not active
    town.shop(age_hours=1, verified=False)  # not verified: neither active nor in the median
    town.shop(age_hours=1, city="Somewhere Else")  # another city
    monkeypatch.setattr(settings, "kpi_ballari_min_active_shops", 3)
    f = town.figures()
    assert judged(f["active_shops"]) == (3, True)
    # P7.4's median over the verified shops' active listings: 10, 48, 48 and 480 hours.
    assert judged(f["median_stock_age_hours"]) == (48.0, True)
    assert f["median_stock_age_hours"]["detail"]["listings"] == 4
    assert f["median_stock_age_hours"]["threshold"]["gate"] == "ballari"
    monkeypatch.setattr(settings, "kpi_ballari_min_active_shops", 4)
    monkeypatch.setattr(settings, "kpi_max_median_stock_age_hours", 47.99)
    f = town.figures()
    assert judged(f["active_shops"]) == (3, False)
    assert judged(f["median_stock_age_hours"]) == (48.0, False)


def test_consecutive_fresh_weeks_count_back_from_the_last_complete_week(town, monkeypatch):
    db = town.db
    key = kpis.city_key(town.city)
    last_monday = date(2033, 6, 6)  # AT is Wednesday 15 Jun: the last complete week is 6 to 12 Jun

    def week(monday: date, values: list[float | None]) -> None:
        for i, v in enumerate(values):
            day = monday + timedelta(days=i)
            db.mongo[kpis.DAYS].insert_one({"_id": f"{key}:{day}", "city": key, "day": day.isoformat(),
                                            "median_stock_age_hours": v, "listings": 1, "active_shops": 1,
                                            "recorded_at": AT})

    for n in range(4):
        week(last_monday - timedelta(weeks=n), [48.0] * 7)
    week(last_monday - timedelta(weeks=4), [10.0] * 6 + [48.01])  # one reading over: the run stops here
    week(last_monday - timedelta(weeks=5), [10.0] * 7)
    week(date(2033, 6, 13), [99.0] * 3)  # the week in progress does not count yet
    monkeypatch.setattr(settings, "kpi_ballari_min_fresh_weeks", 4)
    f = town.figures()
    assert judged(f["fresh_weeks"]) == (4, True)
    assert f["fresh_weeks"]["detail"]["last_complete_week"] == "2033-06-06"
    monkeypatch.setattr(settings, "kpi_ballari_min_fresh_weeks", 5)
    assert judged(town.figures()["fresh_weeks"]) == (4, False)
    # A week with too few readings ends the run as well.
    db.mongo[kpis.DAYS].delete_many({"city": key, "day": {"$gte": "2033-05-30", "$lte": "2033-06-01"}})
    assert town.figures()["fresh_weeks"]["value"] == 1


def test_the_daily_reading_is_recorded_once_a_day(town):
    town.shop(age_hours=30)
    first = kpis.record_day(town.db, AT, town.city)
    assert first["recorded"] and first["median_stock_age_hours"] == 30.0
    later = kpis.record_day(town.db, AT + timedelta(hours=5), town.city)
    assert later == {"day": "2033-06-15", "recorded": False}
    row = town.db.mongo[kpis.DAYS].find_one({"city": kpis.city_key(town.city)})
    assert row["median_stock_age_hours"] == 30.0 and row["active_shops"] == 1


# ---------------------------------------------------------------- reliability (check 10.5) and demand


def test_arrival_failure_rate_numerator_and_denominator(town, monkeypatch):
    shop = town.shop()
    other = town.shop(city="Elsewhere Town")
    day = AT - timedelta(days=10)
    for _ in range(15):
        town.reservation(shop, created=day, status=ReservationStatus.COMPLETED, completed=day)
    for _ in range(4):
        town.order(shop, created=day, delivered=day)
    town.reservation(shop, created=day, status=ReservationStatus.CANCELLED, close_reason=NOT_AVAILABLE_ON_ARRIVAL,
                     closed=day)
    # Not counted: a failure 100 days ago, a cancellation for another reason, a failure in another city, and
    # pickups completed before the window.
    old = AT - timedelta(days=100)
    town.reservation(shop, created=old, status=ReservationStatus.CANCELLED, close_reason=NOT_AVAILABLE_ON_ARRIVAL,
                     closed=old)
    town.reservation(shop, created=day, status=ReservationStatus.CANCELLED, close_reason="Changed my mind",
                     closed=day)
    town.reservation(other, created=day, status=ReservationStatus.CANCELLED, close_reason=NOT_AVAILABLE_ON_ARRIVAL,
                     closed=day)
    town.reservation(shop, created=old, status=ReservationStatus.COMPLETED, completed=old)
    monkeypatch.setattr(settings, "kpi_g4_min_completions_90d", 19)
    f = town.figures()
    detail = f["arrival_failure_pct"]["detail"]
    assert (detail["failures"], detail["completions"], detail["pickups"], detail["deliveries"],
            detail["denominator"]) == (1, 19, 15, 4, 20)
    assert judged(f["arrival_failure_pct"]) == (5.0, True)  # 1 / (19 + 1), at the 5% threshold
    assert judged(f["completions_90d"]) == (19, True)
    assert f["completions_90d"]["threshold"]["gate"] == "g4"
    town.reservation(shop, created=day, status=ReservationStatus.CANCELLED, close_reason=NOT_AVAILABLE_ON_ARRIVAL,
                     closed=day)
    monkeypatch.setattr(settings, "kpi_g4_min_completions_90d", 20)
    f = town.figures()
    assert f["arrival_failure_pct"]["detail"]["denominator"] == 21
    assert judged(f["arrival_failure_pct"]) == (round(200 / 21, 2), False)
    assert judged(f["completions_90d"]) == (19, False)


def test_completions_in_the_month(town, monkeypatch):
    shop = town.shop()
    in_may = MAY[0] + timedelta(days=3)
    for _ in range(2):
        town.reservation(shop, created=in_may, status=ReservationStatus.COMPLETED, completed=in_may)
    town.order(shop, created=in_may, delivered=in_may)
    town.reservation(shop, created=APRIL[0], status=ReservationStatus.COMPLETED, completed=APRIL[0])  # April
    town.order(shop, created=MAY[1], delivered=MAY[1] + timedelta(hours=1))  # June: this month so far
    town.order(shop, created=in_may)  # not delivered
    monkeypatch.setattr(settings, "kpi_ballari_min_completions_month", 3)
    f = town.figures()
    assert judged(f["completions_month"]) == (3, True)
    assert f["completions_month"]["detail"]["month"] == "2033-05"
    assert f["completions_month"]["detail"]["this_month_so_far"] == 1
    monkeypatch.setattr(settings, "kpi_ballari_min_completions_month", 4)
    assert judged(town.figures()["completions_month"]) == (3, False)


def test_month_two_return(town, monkeypatch):
    shop = town.shop()
    first = [town.customer() for _ in range(4)]
    for c in first:
        town.reservation(shop, c, created=APRIL[0] + timedelta(days=2))
    regular = town.customer()  # first ordered in March: not in April's cohort, though it ordered in April and May
    town.order(shop, regular, created=APRIL[0] - timedelta(days=5))
    town.reservation(shop, regular, created=APRIL[0] + timedelta(days=4))
    town.reservation(shop, regular, created=MAY[0] + timedelta(days=4))
    town.order(shop, first[0], created=MAY[0] + timedelta(days=9))  # returns in May with an order
    town.reservation(shop, first[1], created=MAY[1] + timedelta(days=1))  # June: too late
    monkeypatch.setattr(settings, "kpi_ballari_min_month2_return_pct", 25.0)
    f = town.figures()
    assert f["month2_return_pct"]["detail"].items() >= {"cohort_month": "2033-04", "return_month": "2033-05",
                                                     "cohort": 4, "returned": 1}.items()
    assert judged(f["month2_return_pct"]) == (25.0, True)
    monkeypatch.setattr(settings, "kpi_ballari_min_month2_return_pct", 25.01)
    assert judged(town.figures()["month2_return_pct"]) == (25.0, False)


def test_zero_result_rate_over_thirty_days(town):
    db = town.db
    rows = [{"query": f"kpi-{town.tag}", "normalized_query": f"kpi-{town.tag}", "results_count": 0 if i == 0 else 3,
             "created_at": AT - timedelta(days=1)} for i in range(10)]
    rows.append({"query": "old", "normalized_query": "old", "results_count": 0, "created_at": AT - timedelta(days=31)})
    ids = [db.next_id("search_history") for _ in rows]
    db.mongo["search_history"].insert_many([{"_id": i, **r} for i, r in zip(ids, rows, strict=True)])
    try:
        f = town.figures()
        assert f["zero_result_pct"]["detail"].items() >= {"searches": 10, "zero": 1}.items()
        assert judged(f["zero_result_pct"]) == (10.0, True)  # at the 10% threshold
        extra = db.next_id("search_history")
        ids.append(extra)
        db.mongo["search_history"].insert_one({"_id": extra, "query": "x", "normalized_query": "x",
                                               "results_count": 0, "created_at": AT - timedelta(hours=2)})
        assert judged(town.figures()["zero_result_pct"]) == (round(200 / 11, 2), False)
    finally:
        db.mongo["search_history"].delete_many({"_id": {"$in": ids}})


# ---------------------------------------------------------------- revenue


def test_shop_paid_share_leaves_sponsored_seats_out(town, monkeypatch):
    paid, advance, sponsored, free, inactive = (town.shop(), town.shop(), town.shop(), town.shop(),
                                                town.shop(age_hours=30 * 24))
    start, end = AT - timedelta(days=30), AT + timedelta(days=300)
    town.subscription(paid, source=SubscriptionSource.SHOP_PAID, status=SS.ACTIVE, starts=start, ends=end)
    town.subscription(advance, source=SubscriptionSource.ADVANCE, status=SS.ACTIVE, starts=start, ends=end)
    town.subscription(sponsored, source=SubscriptionSource.SPONSORED, status=SS.ACTIVE, starts=start, ends=end)
    town.subscription(inactive, source=SubscriptionSource.SHOP_PAID, status=SS.ACTIVE, starts=start, ends=end)
    town.subscription(free, source=SubscriptionSource.SHOP_PAID, status=SS.LAPSED, starts=start - timedelta(days=400),
                      ends=start)
    monkeypatch.setattr(settings, "kpi_ballari_min_shop_paid_pct", 50.0)
    f = town.figures()
    assert f["shop_paid_pct"]["detail"].items() >= {"paid": 2, "sponsored": 1, "active": 4}.items()
    assert judged(f["shop_paid_pct"]) == (50.0, True)
    assert f["sponsored_seats"]["value"] == 1 and f["sponsored_seats"]["threshold"] is None
    monkeypatch.setattr(settings, "kpi_ballari_min_shop_paid_pct", 50.01)
    assert judged(town.figures()["shop_paid_pct"]) == (50.0, False)


def test_founding_cohort_renewal(town, monkeypatch):
    def founding(ended_days_ago: int, status: SS = SS.LAPSED) -> tuple[Shop, Subscription, datetime]:
        shop = town.shop()
        end = AT - timedelta(days=ended_days_ago)
        sub = town.subscription(shop, source=SubscriptionSource.ADVANCE, status=status, starts=end - timedelta(days=365),
                                ends=end, founding=True, paid_at=end - timedelta(days=400))
        return shop, sub, end

    # Renewed 10 days after its first year ended: renewed.
    _, renewed, end = founding(60, SS.ACTIVE)
    town.renewed(renewed, at=end + timedelta(days=10), ends_before=end)
    # Lapsed, never renewed: not renewed.
    founding(60)
    # Renewed 40 days late: not renewed in time.
    _, late, end = founding(80, SS.ACTIVE)
    town.renewed(late, at=end + timedelta(days=40), ends_before=end)
    # Lapsed, then bought a new paid plan 5 days after the end: renewed.
    shop, _, end = founding(50)
    town.subscription(shop, source=SubscriptionSource.SHOP_PAID, status=SS.ACTIVE, starts=end + timedelta(days=5),
                      ends=end + timedelta(days=370))
    # Ended 10 days ago: pending, not counted. Ended 400 days ago: outside the window. Refunded: left out.
    founding(10)
    founding(400)
    founding(60, SS.REFUNDED)
    monkeypatch.setattr(settings, "kpi_ballari_min_renewal_pct", 50.0)
    f = town.figures()
    assert f["founding_renewal_pct"]["detail"].items() >= {"cohort": 4, "renewed": 2, "pending": 1}.items()
    assert judged(f["founding_renewal_pct"]) == (50.0, True)
    monkeypatch.setattr(settings, "kpi_ballari_min_renewal_pct", 50.01)
    assert judged(town.figures()["founding_renewal_pct"]) == (50.0, False)


def test_annual_inactivity_reads_the_snapshot_a_year_earlier(town, clean_kpis, monkeypatch):
    db = town.db
    shops = [town.shop() for _ in range(4)]
    gone = town.shop(age_hours=30 * 24)
    town.shop()  # new since: not in the year-old snapshot, so it does not change the figure
    assert town.figures()["annual_inactivity_pct"]["value"] is None  # no snapshot from May 2032 yet
    db.mongo[kpis.SNAPSHOTS].insert_one({"_id": "2032-05", "month": "2032-05", "taken_at": ist(2032, 6, 2, 6),
                                         "city": town.city, "figures": [],
                                         "active_shop_ids": [s.id for s in shops] + [gone.id]})
    f = town.figures()
    assert f["annual_inactivity_pct"]["detail"].items() >= {"from_month": "2032-05", "then_active": 5, "inactive": 1}.items()
    assert judged(f["annual_inactivity_pct"]) == (20.0, True)
    monkeypatch.setattr(settings, "kpi_ballari_max_annual_inactivity_pct", 19.99)
    assert judged(town.figures()["annual_inactivity_pct"]) == (20.0, False)


def test_paying_partner_plans_are_zero_until_partner_plans_exist(town):
    f = town.figures()["partner_plans"]
    assert f["value"] == 0 and f["pass"] is False and f["threshold"]["value"] == settings.kpi_ballari_min_partner_plans


# ---------------------------------------------------------------- the readout (B25)


def test_readout_figures(town, monkeypatch):
    shops = [town.shop() for _ in range(5)]
    t = AT - timedelta(days=10)
    town.statement(shops[0], "2033-05", (statements.ACCURATE, t))
    town.statement(shops[1], "2033-05", (statements.NOT_ACCURATE, t), (statements.ACCURATE, t + timedelta(hours=1)))
    town.statement(shops[2], "2033-05", (statements.ACCURATE, t), (statements.WOULD_PAY, t))
    town.statement(shops[3], "2033-05", (statements.ACCURATE, t), (statements.NOT_ACCURATE, t + timedelta(hours=1)))
    town.statement(shops[4], "2033-05")
    town.statement(shops[0], "2033-04", (statements.NOT_ACCURATE, t))  # an older month: not the latest
    start, end = AT - timedelta(days=30), AT + timedelta(days=300)
    town.subscription(shops[0], source=SubscriptionSource.ADVANCE, status=SS.ADVANCE, starts=end, ends=end)
    town.subscription(shops[1], source=SubscriptionSource.ADVANCE, status=SS.ACTIVE, starts=start, ends=end)
    town.subscription(shops[2], source=SubscriptionSource.ADVANCE, status=SS.REFUNDED, starts=start, ends=end)
    town.subscription(shops[3], source=SubscriptionSource.SHOP_PAID, status=SS.ACTIVE, starts=start, ends=end)
    monkeypatch.setattr(settings, "kpi_readout_min_advances", 2)
    f = town.figures()
    assert f["accurate_pct"]["detail"].items() >= {"month": "2033-05", "statements": 5, "accurate": 3}.items()
    assert judged(f["accurate_pct"]) == (60.0, True)  # 3 of 5: the 60% readout threshold
    assert f["accurate_pct"]["threshold"]["gate"] == "readout"
    assert judged(f["advances_paid"]) == (2, True)
    monkeypatch.setattr(settings, "kpi_readout_min_accurate_pct", 60.01)
    monkeypatch.setattr(settings, "kpi_readout_min_advances", 3)
    f = town.figures()
    assert judged(f["accurate_pct"]) == (60.0, False)
    assert judged(f["advances_paid"]) == (2, False)


# ---------------------------------------------------------------- entered by hand


def test_entered_inputs_need_a_source_note(admin, clean_kpis, ids):
    url = "/api/admin/kpis/inputs"
    body = {"key": "activation_cost_rupees", "month": "2033-05", "value": 500}
    assert admin.post(url, json=body, headers=HEADERS).status_code == 422
    assert admin.post(url, json={**body, "source_note": "R6 log"}, headers=HEADERS).status_code == 422
    assert admin.post(url, json={**body, "source_note": " " * 20}, headers=HEADERS).status_code == 422
    with pytest.raises(kpis.KpiInputRefused):
        kpis.record_input(clean_kpis, key="support_minutes", month="2033-05", value=4, source_note="   short   ",
                          admin_id=1)
    assert clean_kpis.mongo[kpis.INPUTS].count_documents({}) == 0
    res = admin.post(url, json={**body, "source_note": "R6 cost log, May 2033 tab"}, headers=HEADERS)
    assert res.status_code == 201, res.text
    got = admin.get("/api/admin/kpis").json()
    entered = got["inputs"]["activation_cost_rupees"]
    assert entered["value"] == 500 and entered["source_note"] == "R6 cost log, May 2033 tab" and entered["entered_at"]
    assert got["inputs"]["support_minutes"] is None
    fig = {f["key"]: f for f in got["live"]["figures"]}["activation_cost_rupees"]
    assert judged(fig) == (500.0, True)  # at the ₹500 threshold
    admin.post(url, json={**body, "value": 500.01, "source_note": "R6 cost log, corrected"}, headers=HEADERS)
    fig = {f["key"]: f for f in admin.get("/api/admin/kpis").json()["live"]["figures"]}["activation_cost_rupees"]
    assert judged(fig) == (500.01, False)
    assert clean_kpis.mongo[kpis.INPUTS].count_documents({}) == 2  # the first entry is kept


# ---------------------------------------------------------------- snapshots, the page and the job


def test_snapshots_cannot_be_changed(town, clean_kpis, admin):
    db = town.db
    town.shop()
    first = kpis.take_snapshot(db, 2033, 5, AT, town.city)
    assert first == {"month": "2033-05", "taken": True}
    stored = db.mongo[kpis.SNAPSHOTS].find_one({"_id": "2033-05"})
    assert {f["key"] for f in stored["figures"]} == set(kpis.FIGURES)
    assert next(f for f in stored["figures"] if f["key"] == "active_shops")["value"] == 1
    town.shop()  # the data changes; a second run for the month changes nothing
    assert kpis.take_snapshot(db, 2033, 5, AT + timedelta(days=1), town.city) == {"month": "2033-05", "taken": False}
    assert db.mongo[kpis.SNAPSHOTS].find_one({"_id": "2033-05"}) == stored
    # Nothing in the API writes to a snapshot.
    for method in ("put", "patch", "delete"):
        assert getattr(admin, method)("/api/admin/kpis", headers=HEADERS).status_code == 405
        assert getattr(admin, method)("/api/admin/kpis/snapshots.csv", headers=HEADERS).status_code == 405
    page = admin.get("/api/admin/kpis").json()
    assert [s["month"] for s in page["snapshots"]] == ["2033-05"]
    assert page["note"] == kpis.ASSUMPTIONS_NOTE and "assumptions" in page["note"]
    assert [g["key"] for g in page["gates"]] == ["g4", "readout", "ballari"]
    for f in page["live"]["figures"]:
        assert {"key", "label", "unit", "value", "threshold", "pass", "detail"} <= set(f)
    csv = admin.get("/api/admin/kpis/snapshots.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].split(",") == list(kpis.CSV_COLUMNS)
    assert len(lines) == 1 + len(kpis.FIGURES)
    assert any(line.startswith("2033-05,") and ",active_shops," in line for line in lines)


def test_kpi_routes_are_for_admins_only(owner_a):
    assert owner_a.get("/api/admin/kpis").status_code == 403
    assert owner_a.get("/api/admin/kpis/snapshots.csv").status_code == 403
    assert owner_a.post("/api/admin/kpis/inputs", json={}, headers=HEADERS).status_code in (403, 422)


def test_snapshot_month_is_the_one_before_the_latest_slot():
    assert kpis.snapshot_month(ist(2033, 7, 2, 5, 59)) == (2033, 5)
    assert kpis.snapshot_month(ist(2033, 7, 2, 6, 0)) == (2033, 6)
    assert kpis.snapshot_month(ist(2033, 1, 1, 12, 0)) == (2032, 11)
    assert kpis.snapshot_month(ist(2033, 1, 2, 6, 0)) == (2032, 12)


def test_the_job_reads_daily_and_snapshots_monthly_once(clean_kpis, monkeypatch):
    db = clean_kpis
    assert jobs.JOBS[jobs.KPIS_JOB] is jobs.kpis and jobs.RETRY_AFTER_FAILURE[jobs.KPIS_JOB] == timedelta(hours=1)
    city = kpis.city_key(kpis.default_city())
    db.mongo[jobs.LEASES].delete_many({"_id": jobs.KPIS_JOB})
    try:
        holder = "kpi-test"
        now = ist(2033, 8, 2, 7, 0)
        assert jobs.run_leased(db.mongo, jobs.KPIS_JOB, holder, lambda: jobs.kpis(lambda: now), lambda: now)
        last = db.mongo[jobs.LEASES].find_one({"_id": jobs.KPIS_JOB})["last_result"]
        assert last["ok"] and last["reading_for"] == "2033-08-02" and last["snapshot_for"] == "2033-07"
        assert last["snapshot"] == {"month": "2033-07", "taken": True}
        stored = db.mongo[kpis.SNAPSHOTS].find_one({"_id": "2033-07"})
        assert db.mongo[kpis.DAYS].count_documents({"city": city, "day": "2033-08-02"}) == 1
        # The next tick that day does nothing.
        later = now + timedelta(minutes=1)
        jobs.run_leased(db.mongo, jobs.KPIS_JOB, holder, lambda: jobs.kpis(lambda: later), lambda: later)
        last = db.mongo[jobs.LEASES].find_one({"_id": jobs.KPIS_JOB})["last_result"]
        assert "snapshot" not in last and last["snapshot_for"] == "2033-07"
        assert db.mongo[kpis.SNAPSHOTS].find_one({"_id": "2033-07"}) == stored
        # Nothing is recorded from demo data.
        monkeypatch.setattr(settings, "demo_mode", True)
        assert jobs.kpis(lambda: now + timedelta(days=3)) == {"skipped": "demo data"}
    finally:
        db.mongo[jobs.LEASES].delete_many({"_id": jobs.KPIS_JOB})
        db.mongo[kpis.DAYS].delete_many({"city": city, "day": {"$gte": "2033-01-01"}})
