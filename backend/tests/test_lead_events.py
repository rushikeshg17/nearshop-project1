"""Lead events and monthly lead counts per shop (P9.2; app/services/leads.py; plan sections 8.3, 8.4 and 8.18).

The rollup tests use their own shop, owner and customers, and IST months years ahead, so no other test's data is in
them; everything they insert is removed afterwards."""
import argparse
import csv
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo.errors import WriteError

from app import jobs, main
from app.core import ratelimit
from app.core.config import settings
from app.core.database import Database, utcnow
from app.models import Order, Reservation, Role, Shop, User
from app.models.activity import OrderItem
from app.models.core import VerificationStatus, live_shop_filter, months_after
from app.models.enums import OrderStatus, ReservationStatus
from app.services import leads
from database import admin_tasks
from tests.conftest import CENTER, HEADERS

IST = ZoneInfo("Asia/Kolkata")
SECRET = "edge-secret-for-lead-tests-" + "x" * 24
LEAD_COLLECTIONS = (leads.EVENTS, leads.DEDUPE, leads.SALTS, leads.COUNTS, leads.STAFF)


def ist(*args: int) -> datetime:
    """An IST wall-clock time as the naive UTC time the database stores."""
    return datetime(*args, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


@pytest.fixture
def clean(ids):
    db = Database()
    for name in LEAD_COLLECTIONS:
        db.mongo[name].delete_many({})
    leads._salts.clear()
    yield db
    for name in LEAD_COLLECTIONS:
        db.mongo[name].delete_many({})


@pytest.fixture
def api(clean, monkeypatch):
    """An edge secret configured (so a test can name the visitor's IP) but not required."""
    monkeypatch.setattr(settings, "edge_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "edge_secret_required", False)
    with TestClient(main.app) as c:
        yield c


def visitor(ip: str = "203.0.113.10", ua: str = "Mozilla/5.0 (Linux; Android 12) Phone-A") -> dict[str, str]:
    return {**HEADERS, "X-NearShop-Edge": SECRET, "X-NearShop-Client-IP": ip, "User-Agent": ua}


def tap(api, shop_id: int, kind: str, headers: dict[str, str], product_id: int | None = None):
    body = {"shop_id": shop_id, "kind": kind} | ({"product_id": product_id} if product_id else {})
    return api.post("/api/leads", json=body, headers=headers)


def events(db: Database, **filter) -> list[dict]:
    return list(db.mongo[leads.EVENTS].find(filter))


# ---------------------------------------------------------------- recording taps


def test_a_tap_counts_once_per_visitor_shop_kind_and_day(api, clean, ids):
    db = clean
    one, two = visitor(), visitor("198.51.100.20", "Mozilla/5.0 (iPhone) Phone-B")
    for _ in range(10):
        assert tap(api, ids["shop_a"], "call_tap", one).status_code == 204
        assert tap(api, ids["shop_a"], "directions_tap", one).status_code == 204
        assert tap(api, ids["shop_a"], "call_tap", two).status_code == 204
        assert tap(api, ids["shop_a"], "directions_tap", two).status_code == 204
    today = leads.ist_start(leads.ist_day(utcnow()))
    assert len(events(db, shop_id=ids["shop_a"], kind="call_tap", day=today)) == 2
    assert len(events(db, shop_id=ids["shop_a"], kind="directions_tap", day=today)) == 2
    # The same visitor counts again at another shop, and the same IP with another browser is another visitor.
    assert tap(api, ids["shop_b"], "call_tap", one).status_code == 204
    assert tap(api, ids["shop_a"], "call_tap", visitor(ua="Another browser")).status_code == 204
    assert len(events(db, shop_id=ids["shop_b"])) == 1
    assert len(events(db, shop_id=ids["shop_a"], kind="call_tap")) == 3
    # An IPv6 visitor counts by the /64 network, as the rate limits do: a new address in it is the same device.
    assert tap(api, ids["shop_b"], "directions_tap", visitor("2001:db8:1:2::1")).status_code == 204
    assert tap(api, ids["shop_b"], "directions_tap", visitor("2001:db8:1:2::99")).status_code == 204
    assert len(events(db, shop_id=ids["shop_b"], kind="directions_tap")) == 1


def test_the_next_ist_day_counts_again_with_a_new_salt(clean, ids):
    db = clean
    late = ist(2031, 5, 10, 23, 59)
    assert leads.record_tap(db, ids["shop_a"], "call_tap", None, "203.0.113.10", "ua", now=late)
    assert not leads.record_tap(db, ids["shop_a"], "call_tap", None, "203.0.113.10", "ua",
                                now=late + timedelta(seconds=30))
    assert leads.record_tap(db, ids["shop_a"], "call_tap", None, "203.0.113.10", "ua", now=late + timedelta(minutes=2))
    days = sorted(e["day"] for e in events(db, shop_id=ids["shop_a"]))
    assert days == [ist(2031, 5, 10), ist(2031, 5, 11)]
    salts = {d["_id"]: d for d in db.mongo[leads.SALTS].find({"_id": {"$in": ["2031-05-10", "2031-05-11"]}})}
    assert salts["2031-05-10"]["salt"] != salts["2031-05-11"]["salt"]
    assert salts["2031-05-10"]["expires_at"] == ist(2031, 5, 11, 1, 0)  # an hour after its day ends


def test_stored_events_carry_no_ip_user_agent_user_or_phone(api, clean, ids, customer):
    db = clean
    ip, ua = "203.0.113.77", "Mozilla/5.0 UniqueAgent/77"
    # Signed in, with a phone on the account: still nothing about the visitor is stored.
    assert tap(customer, ids["shop_a"], "call_tap", {**visitor(ip, ua)}, product_id=ids["charger"]).status_code == 204
    (event,) = events(db)
    assert set(event) == {"_id", "shop_id", "product_id", "kind", "day"}
    assert (event["shop_id"], event["product_id"], event["kind"]) == (ids["shop_a"], ids["charger"], "call_tap")
    assert isinstance(event["_id"], int)
    assert event["day"] == leads.ist_start(leads.ist_day(utcnow()))  # the day only, not the time of the tap
    (dedupe,) = db.mongo[leads.DEDUPE].find()
    assert set(dedupe) == {"_id", "expires_at"}
    stored = repr([event, dedupe])
    for secret in (ip, ua, "9876543210", "cust@example.com"):
        assert secret not in stored
    # The database itself refuses any other field on an event or a dedupe row.
    with pytest.raises(WriteError):
        db.mongo[leads.EVENTS].insert_one({"_id": 10**9, "shop_id": 1, "product_id": None, "kind": "call_tap",
                                           "day": utcnow(), "ip": ip})
    with pytest.raises(WriteError):
        db.mongo[leads.DEDUPE].insert_one({"_id": "0" * 64, "expires_at": utcnow(), "user_agent": ua})


def test_dedupe_rows_and_events_expire_through_ttl_indexes(clean):
    events_ix = clean.mongo[leads.EVENTS].index_information()
    assert events_ix["day_ttl_90d"]["expireAfterSeconds"] == 90 * 24 * 3600
    assert clean.mongo[leads.DEDUPE].index_information()["expires_ttl"]["expireAfterSeconds"] == 0
    assert clean.mongo[leads.SALTS].index_information()["expires_ttl"]["expireAfterSeconds"] == 0
    assert leads.DEDUPE_KEEP == timedelta(hours=48)
    leads.record_tap(clean, 1, "call_tap", None, "203.0.113.1", "ua", now=ist(2031, 5, 1, 12))
    (row,) = clean.mongo[leads.DEDUPE].find()
    assert row["expires_at"] == ist(2031, 5, 1, 12) + timedelta(hours=48)


def test_a_tap_names_a_live_shop_and_its_own_listing(api, clean, ids):
    h = visitor()
    assert tap(api, ids["shop_a"], "directions_tap", h, product_id=ids["tape"]).status_code == 204
    assert tap(api, ids["shop_a"], "call_tap", h, product_id=ids["tape_far"]).status_code == 404  # shop B's listing
    assert tap(api, 999999, "call_tap", h).status_code == 404
    assert tap(api, ids["shop_a"], "whatsapp_tap", h).status_code == 422
    res = api.post("/api/leads", json={"shop_id": ids["shop_a"], "kind": "call_tap", "ip": "1.2.3.4"}, headers=h)
    assert res.status_code == 422  # no other field is accepted
    no_client_header = {k: v for k, v in h.items() if k != "X-NearShop-Client"}
    assert tap(api, ids["shop_a"], "call_tap", no_client_header).status_code == 403  # the CSRF guard
    assert len(events(clean)) == 1


def test_the_endpoint_limit_returns_429_with_retry_after(api, ids, monkeypatch):
    row = ratelimit.match("POST", "/api/leads")
    assert row is not None and row.rules == (ratelimit.LEAD_EVENT,)
    rule = ratelimit.LEAD_EVENT
    assert (rule.limit, rule.per, rule.key) == (30, 60, (ratelimit.IP,))
    monkeypatch.setattr(settings, "rate_limit_mode", "enforce")
    start = (datetime.now(UTC) + timedelta(days=40)).replace(second=0, microsecond=0).timestamp() + 1
    monkeypatch.setattr(ratelimit, "now", lambda: start)
    Database().mongo[ratelimit.COLLECTION].delete_many({})
    try:
        h = visitor("192.0.2.30")
        for n in range(30):
            assert tap(api, ids["shop_a"], "call_tap", visitor("192.0.2.30", f"ua-{n}")).status_code == 204
        res = tap(api, ids["shop_a"], "call_tap", h)
        assert res.status_code == 429
        assert res.json()["error"]["code"] == "rate_limited"
        assert res.headers["Retry-After"] == "59"
        assert tap(api, ids["shop_a"], "call_tap", visitor("192.0.2.31")).status_code == 204  # another IP
    finally:
        Database().mongo[ratelimit.COLLECTION].delete_many({})


# ---------------------------------------------------------------- monthly counts


@pytest.fixture
def town(clean):
    """A live shop whose owner, shop and staff have phones, three customers, and a listing; removed afterwards."""
    db = clean
    tag = secrets.token_hex(3)
    owner, cust, alt, staffer = db.users.insert_many([
        User(name="Lead Owner", email=f"lead-owner-{tag}@example.com", password_hash="", role=Role.OWNER,
             phone="9000000001"),
        User(name="Lead Customer", email=f"lead-cust-{tag}@example.com", password_hash="", role=Role.CUSTOMER,
             phone="9123400001"),
        # Another account on the shop's own number (written differently): its requests are not customer requests.
        User(name="Shop Alt", email=f"lead-alt-{tag}@example.com", password_hash="", role=Role.CUSTOMER,
             phone="09000000002"),
        User(name="No Phone", email=f"lead-nophone-{tag}@example.com", password_hash="", role=Role.CUSTOMER),
    ])
    decided = utcnow()
    shop = db.shops.insert(Shop(
        owner_id=owner.id, name="Lead Test Shop", slug=f"lead-test-{tag}", address_line="3 Lead Road", city="Mysuru",
        lat=CENTER[0] + 0.01, lng=CENTER[1], phone="+91 90000 00002", category_ids=[],
        verification_status=VerificationStatus.VERIFIED, is_verified=True, kyc_type="udyam",
        phone_verified_at=decided, submitted_at=decided, decided_at=decided, verified_until=months_after(decided)))
    product = db.products.find_one({})
    data = {"owner": owner, "cust": cust, "alt": alt, "staffer": staffer, "shop": shop, "product": product,
            "reservations": [], "orders": []}
    yield data
    db.reservations.delete_many({"_id": {"$in": data["reservations"]}})
    db.orders.delete_many({"_id": {"$in": data["orders"]}})
    db.shops.delete_many({"_id": shop.id})
    db.users.delete_many({"_id": {"$in": [owner.id, cust.id, alt.id, staffer.id]}})


def reserve(db: Database, town: dict, customer: User, created: datetime, status=ReservationStatus.REQUESTED, *,
            confirmed: datetime | None = None, completed: datetime | None = None, quantity: int = 1,
            unit_price: float = 100.0) -> None:
    r = db.reservations.insert(Reservation(
        code=f"L{secrets.token_hex(4).upper()}", customer_id=customer.id, shop_id=town["shop"].id,
        product_id=town["product"].id + len(town["reservations"]) + 10**6, quantity=quantity, unit_price=unit_price,
        status=status, expires_at=created + timedelta(minutes=30), confirmed_at=confirmed, completed_at=completed,
        created_at=created, updated_at=created))
    town["reservations"].append(r.id)


def order(db: Database, town: dict, customer: User, created: datetime, status=OrderStatus.PENDING, *,
          delivered: datetime | None = None, contact: str = "9123499999", total: float = 530.0) -> None:
    o = db.orders.insert(Order(
        code=f"O{secrets.token_hex(4).upper()}", customer_id=customer.id, shop_id=town["shop"].id, status=status,
        delivery_address="4 Lead Street", delivery_lat=CENTER[0], delivery_lng=CENTER[1], contact_phone=contact,
        distance_km=1.0, subtotal=total - 30, delivery_fee=30, total=total, delivered_at=delivered,
        items=[OrderItem(product_id=town["product"].id, name="Item", unit_price=total - 30, quantity=1)],
        created_at=created, updated_at=created))
    town["orders"].append(o.id)


def controlled_month(db: Database, town: dict) -> None:
    """May 2031 (IST), with activity just outside it on both sides."""
    cust, alt, nophone, shop = town["cust"], town["alt"], town["staffer"], town["shop"]
    # Reservation requests: 4 in May (one from the shop's own number), 1 on 30 April at 23:59 IST, 1 on 1 June.
    reserve(db, town, cust, ist(2031, 5, 1, 0, 0), ReservationStatus.COMPLETED, confirmed=ist(2031, 5, 1, 0, 10),
            completed=ist(2031, 5, 1, 1, 0))
    reserve(db, town, cust, ist(2031, 5, 15, 10), ReservationStatus.CANCELLED, confirmed=ist(2031, 5, 15, 11))
    reserve(db, town, nophone, ist(2031, 5, 31, 23, 59), ReservationStatus.EXPIRED)
    reserve(db, town, alt, ist(2031, 5, 20), ReservationStatus.REJECTED)
    # Requested in April, confirmed and completed in May: counted where each step happened.
    reserve(db, town, cust, ist(2031, 4, 30, 23, 59), ReservationStatus.COMPLETED, confirmed=ist(2031, 5, 1, 9),
            completed=ist(2031, 5, 2, 9))
    reserve(db, town, cust, ist(2031, 6, 1, 0, 0), ReservationStatus.COMPLETED, confirmed=ist(2031, 6, 1, 1),
            completed=ist(2031, 6, 1, 2))
    # Delivery orders: 3 in May. One by a customer whose contact phone is the owner's: not a customer request.
    order(db, town, cust, ist(2031, 5, 3), OrderStatus.DELIVERED, delivered=ist(2031, 5, 3, 18))
    order(db, town, nophone, ist(2031, 5, 4), OrderStatus.CANCELLED, contact="+91-90000-00001")
    order(db, town, nophone, ist(2031, 5, 31, 22), OrderStatus.DELIVERED, delivered=ist(2031, 6, 1, 0, 30))
    order(db, town, cust, ist(2031, 4, 30, 20), OrderStatus.DELIVERED, delivered=ist(2031, 5, 1, 0, 30))
    # Taps: 2 calls and 1 directions in May, 1 call on 1 June.
    for when, kind, ip in ((ist(2031, 5, 5, 10), "call_tap", "203.0.113.1"), (ist(2031, 5, 5, 11), "call_tap", "203.0.113.2"),
                           (ist(2031, 5, 31, 23), "directions_tap", "203.0.113.1"),
                           (ist(2031, 6, 1, 0, 5), "call_tap", "203.0.113.1")):
        assert leads.record_tap(db, shop.id, kind, None, ip, "ua", now=when)


MAY_2031 = {"call_taps": 2, "directions_taps": 1, "reservation_requests": 4, "reservations_confirmed": 3,
            "pickups_completed": 2, "delivery_orders": 3, "deliveries_completed": 2,
            # 4 reservation requests + 3 orders, less the request from the shop's own number and the order whose
            # contact phone is the owner's.
            "customer_requests": 5}


def test_the_monthly_rollup_gives_every_count_and_leaves_the_shops_own_phones_out(town, clean):
    db = clean
    controlled_month(db, town)
    result = leads.rollup_month(db, 2031, 5, now=ist(2031, 6, 1, 2))
    assert result["month"] == "2031-05"
    row = db.mongo[leads.COUNTS].find_one({"shop_id": town["shop"].id, "month": "2031-05"})
    assert {f: row[f] for f in leads.COUNT_FIELDS} == MAY_2031
    assert row["keep_until"] == ist(2039, 6, 1)  # kept 8 years after the month ends
    assert isinstance(row["_id"], int)
    # Every live shop gets a row, with zeros when nothing happened.
    live = {d["_id"] for d in db.shops.find_raw(live_shop_filter(), {"_id": 1})}
    rows = {d["shop_id"]: d for d in db.mongo[leads.COUNTS].find({"month": "2031-05"})}
    assert live <= set(rows)
    assert all(rows[s]["reservation_requests"] == 0 for s in live - {town["shop"].id})

    # A staff member's phone (shop_staff, P9.13) is left out as well: the request from the customer whose phone it is.
    db.mongo[leads.STAFF].insert_one(staff_row(town, phone="9123400001"))
    counts = leads.compute_counts(db, *leads.month_bounds(2031, 5), [town["shop"].id])[town["shop"].id]
    assert counts["customer_requests"] == 2  # only the account without a phone is left: a reservation and an order
    db.mongo[leads.STAFF].delete_many({})
    db.mongo[leads.STAFF].insert_one(staff_row(town, phone="6000000001", user_id=town["cust"].id))
    counts = leads.compute_counts(db, *leads.month_bounds(2031, 5), [town["shop"].id])[town["shop"].id]
    assert counts["customer_requests"] == 2  # a staff row naming the account instead of a phone


def staff_row(town: dict, *, phone: str, user_id: int | None = None) -> dict:
    """A shop_staff row as P9.13 writes it (app/services/staff.py), valid under its validator."""
    now = utcnow()
    return {"_id": 10**8 + int(phone[-4:]), "shop_id": town["shop"].id, "user_id": user_id, "phone": phone,
            "name": "Lead Staff", "status": "active", "invited_by": town["shop"].owner_id, "invited_at": now,
            "created_at": now, "updated_at": now}


def test_a_rerun_replaces_the_months_row(town, clean):
    db = clean
    controlled_month(db, town)
    leads.rollup_month(db, 2031, 5, now=ist(2031, 6, 1, 2))
    first = db.mongo[leads.COUNTS].find_one({"shop_id": town["shop"].id, "month": "2031-05"})
    reserve(db, town, town["cust"], ist(2031, 5, 25), ReservationStatus.CANCELLED)
    db.mongo[leads.COUNTS].insert_one({"_id": 10**8, "shop_id": 10**8, "month": "2031-05", "computed_at": utcnow(),
                                       "keep_until": ist(2039, 6, 1), **dict.fromkeys(leads.COUNT_FIELDS, 1)})
    leads.rollup_month(db, 2031, 5, now=ist(2031, 6, 2))
    rows = list(db.mongo[leads.COUNTS].find({"shop_id": town["shop"].id, "month": "2031-05"}))
    assert len(rows) == 1 and rows[0]["_id"] == first["_id"]
    assert rows[0]["reservation_requests"] == 5 and rows[0]["customer_requests"] == 6
    assert rows[0]["computed_at"] == ist(2031, 6, 2)
    assert db.mongo[leads.COUNTS].find_one({"shop_id": 10**8}) is None  # a shop with nothing to count is gone


def test_month_counts_gives_a_stored_month_and_the_current_month_so_far(town, clean):
    db = clean
    controlled_month(db, town)
    so_far = leads.month_counts(db, town["shop"].id, 2031, 5, now=ist(2031, 5, 16))
    assert so_far["complete"] is False and so_far["month"] == "2031-05"
    assert so_far["reservation_requests"] == 2 and so_far["call_taps"] == 2
    computed = leads.month_counts(db, town["shop"].id, 2031, 5, now=ist(2031, 6, 1, 1))  # before the job ran
    assert computed["complete"] is True and {f: computed[f] for f in leads.COUNT_FIELDS} == MAY_2031
    leads.rollup_month(db, 2031, 5, now=ist(2031, 6, 1, 2))
    db.mongo[leads.COUNTS].update_one({"shop_id": town["shop"].id, "month": "2031-05"}, {"$set": {"call_taps": 7}})
    assert leads.month_counts(db, town["shop"].id, 2031, 5, now=ist(2031, 7, 1))["call_taps"] == 7  # the stored row
    with pytest.raises(ValueError):
        leads.month_counts(db, town["shop"].id, 2031, 7, now=ist(2031, 6, 30))
    # A finished month never rolled up is recomputed only while its lead events can still all be there.
    db.mongo[leads.COUNTS].delete_many({"shop_id": town["shop"].id, "month": "2031-05"})
    late = leads.month_counts(db, town["shop"].id, 2031, 5, now=ist(2031, 5, 1) + leads.EVENTS_KEEP - timedelta(1))
    assert {f: late[f] for f in leads.COUNT_FIELDS} == MAY_2031
    with pytest.raises(leads.MonthNotRolledUp):
        leads.month_counts(db, town["shop"].id, 2031, 5, now=ist(2031, 5, 1) + leads.EVENTS_KEEP)


def test_the_job_runs_on_the_first_at_0200_ist_for_the_previous_month(town, clean, monkeypatch):
    db = clean
    assert jobs.lead_counts_month(ist(2031, 6, 1, 1, 59)) == (2031, 4)
    assert jobs.lead_counts_month(ist(2031, 6, 1, 2, 0)) == (2031, 5)
    assert jobs.lead_counts_month(ist(2031, 6, 30, 23)) == (2031, 5)
    assert jobs.lead_counts_month(ist(2031, 1, 1, 2)) == (2030, 12)
    assert jobs.lead_counts_month(ist(2031, 1, 1, 1)) == (2030, 11)
    assert "lead_counts" in jobs.JOBS and jobs.RETRY_AFTER_FAILURE["lead_counts"] == timedelta(hours=1)
    controlled_month(db, town)
    calls = []
    real = leads.rollup_month
    monkeypatch.setattr(leads, "rollup_month", lambda *a, **k: calls.append(a[1:3]) or real(*a, **k))
    db.mongo[jobs.LEASES].delete_many({"_id": jobs.LEAD_COUNTS_JOB})
    clock = [ist(2031, 6, 1, 2, 0, 30)]
    scheduler = jobs.Scheduler(db.mongo, jobs={jobs.LEAD_COUNTS_JOB: lambda: jobs.lead_counts(lambda: clock[0])},
                               clock=lambda: clock[0], holder="lead-test")
    try:
        assert scheduler.tick()[jobs.LEAD_COUNTS_JOB]
        clock[0] += timedelta(minutes=1)
        scheduler.tick()  # the same month: only the last result again
        assert calls == [(2031, 5)]
        lease = db.mongo[jobs.LEASES].find_one({"_id": jobs.LEAD_COUNTS_JOB})
        assert lease["last_result"]["ok"] is True and lease["last_result"]["ran_for"] == "2031-05"
        row = db.mongo[leads.COUNTS].find_one({"shop_id": town["shop"].id, "month": "2031-05"})
        assert {f: row[f] for f in leads.COUNT_FIELDS} == MAY_2031
    finally:
        db.mongo[jobs.LEASES].delete_many({"_id": jobs.LEAD_COUNTS_JOB})


# ---------------------------------------------------------------- facilitated sales


def test_facilitated_sales_split_at_the_31_march_to_1_april_boundary(town, clean, tmp_path):
    db = clean
    cust = town["cust"]
    reserve(db, town, cust, ist(2031, 3, 31, 20), ReservationStatus.COMPLETED, completed=ist(2031, 3, 31, 23, 59, 59),
            quantity=2, unit_price=150)
    reserve(db, town, cust, ist(2031, 3, 31, 23), ReservationStatus.COMPLETED, completed=ist(2031, 4, 1, 0, 0),
            quantity=1, unit_price=40)
    reserve(db, town, cust, ist(2031, 4, 2), ReservationStatus.CANCELLED)  # not completed: not counted
    order(db, town, cust, ist(2031, 3, 31, 18), OrderStatus.DELIVERED, delivered=ist(2031, 3, 31, 23, 30), total=530)
    order(db, town, cust, ist(2031, 3, 31, 23), OrderStatus.DELIVERED, delivered=ist(2031, 4, 1, 0, 15), total=1030)
    order(db, town, cust, ist(2031, 4, 5), OrderStatus.CANCELLED)

    def mine(start_year: int) -> dict:
        return next(r for r in leads.facilitated_sales(db, start_year) if r["shop_id"] == town["shop"].id)

    assert mine(2030) == {"shop_id": town["shop"].id, "shop_name": "Lead Test Shop", "completed_pickups": 1,
                          "completed_deliveries": 1, "total_value": 830.0}
    assert mine(2031) == {"shop_id": town["shop"].id, "shop_name": "Lead Test Shop", "completed_pickups": 1,
                          "completed_deliveries": 1, "total_value": 1070.0}
    assert all(r["shop_id"] != town["shop"].id for r in leads.facilitated_sales(db, 2032))

    printed: list[str] = []
    path = tmp_path / "fy.csv"
    assert admin_tasks.main(["facilitated-sales", "--fy", "2031-32", "--out", str(path)], out=printed.append) == 0
    rows = list(csv.DictReader(path.open()))
    assert list(rows[0]) == list(leads.FACILITATED_COLUMNS)
    assert {"shop_id": str(town["shop"].id), "shop_name": "Lead Test Shop", "completed_pickups": "1",
            "completed_deliveries": "1", "total_value": "1070.00"} in rows
    assert printed == [str(path), f"{len(rows)} row(s)"]  # only the path and the row count
    # A shop name a spreadsheet would run as a formula is written as text.
    db.mongo["shops"].update_one({"_id": town["shop"].id}, {"$set": {"name": "=HYPERLINK(\"http://x\",\"y\")"}})
    assert admin_tasks.main(["facilitated-sales", "--fy", "2031-32", "--out", str(path), "--force"],
                            out=printed.append) == 0
    mine_row = next(r for r in csv.DictReader(path.open()) if r["shop_id"] == str(town["shop"].id))
    assert mine_row["shop_name"] == "'=HYPERLINK(\"http://x\",\"y\")"
    for name in ("+1", "-1", "@SUM(A1)", "\tx", "\rx"):
        assert admin_tasks.csv_text(name) == "'" + name
    assert admin_tasks.csv_text("Lead Test Shop") == "Lead Test Shop" and admin_tasks.csv_text("") == ""
    # An existing file is never replaced without --force.
    assert admin_tasks.main(["facilitated-sales", "--fy", "2031-32", "--out", str(path)], out=printed.append) == 1
    assert admin_tasks.main(["facilitated-sales", "--fy", "2031-32", "--out", str(path), "--force"],
                            out=printed.append) == 0


def test_the_financial_year_argument():
    assert admin_tasks.financial_year("2027-28") == 2027
    assert admin_tasks.financial_year("2027-2028") == 2027
    assert admin_tasks.financial_year("2027") == 2027
    for bad in ("2027-29", "27-28", "FY27"):
        with pytest.raises(argparse.ArgumentTypeError):
            admin_tasks.financial_year(bad)
    assert leads.financial_year_bounds(2027) == (ist(2027, 4, 1), ist(2028, 4, 1))
