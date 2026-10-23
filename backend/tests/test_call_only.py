"""Call-only listings and the owner's out-of-stock reason (P9.8, plan section 8.17).

From settings.call_only_from (an IST day), a listing whose stock count is more than settings.call_only_after_days days
old, or that an arrival failure has left unconfirmed for longer than that, is call-only: public listings carry call_only
(and the shop's phone as call_phone), and reservations and delivery orders for it get 409 listing_call_only. The next
stock update of any kind restores it. An owner who cancels a confirmed reservation or order with the reason
out_of_stock_after_confirm records an arrival failure, as P9.1 does for a customer's report: the hold ends without the
units going back, an arrival_failure event with delta 0 is written and the listing is unconfirmed. Other reasons release
the hold and put the units back, as before."""
from datetime import date, datetime, timedelta

import pytest

from app.core.config import settings
from app.core.database import Database, utcnow
from app.models import InventoryReason
from app.schemas.serializers import OUT_OF_STOCK_AFTER_CONFIRM, call_only_since, is_call_only
from app.services import orders, reservations
from app.services.inventory import apply_bulk, change_stock, set_stock
from tests.conftest import CENTER, HEADERS

DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)


@pytest.fixture
def rule_on(monkeypatch):
    """The call-only rule in force since well before now, with the plan's 14 days."""
    monkeypatch.setattr(settings, "call_only_from", date(2026, 1, 1))
    monkeypatch.setattr(settings, "call_only_after_days", 14)


@pytest.fixture
def restore(ids):
    """Put the seeded listings' counts and confirmation back after each test, close any reservation or order a test left
    open, and remove the notifications and arrival-failure events it made."""
    db = Database()
    saved = {p.id: (p.quantity, p.stock_updated_at) for p in db.products.find({})}
    started = utcnow()
    yield
    db.notifications.delete_many({"created_at": {"$gte": started}})
    db.inventory_events.delete_many({"created_at": {"$gte": started}, "reason": InventoryReason.ARRIVAL_FAILURE.value})
    db.reservations.update_many({"status": {"$in": [s.value for s in reservations.ACTIVE]}},
                                {"$set": {"status": "CANCELLED", "stock_held": False, "closed_at": utcnow()}})
    db.orders.update_many({"status": {"$in": [s.value for s in orders.ACTIVE]}},
                          {"$set": {"status": "CANCELLED", "stock_held": False, "closed_at": utcnow()}})
    for pid, (quantity, at) in saved.items():
        db.products.update_one({"_id": pid}, {"$set": {"quantity": quantity, "stock_updated_at": at,
                                                       "stock_unconfirmed_at": None}})


def _age(product_id: int, days: float, *, unconfirmed_days: float | None = None) -> None:
    now = utcnow()
    Database().products.update_one({"_id": product_id}, {"$set": {
        "stock_updated_at": now - timedelta(days=days),
        "stock_unconfirmed_at": None if unconfirmed_days is None else now - timedelta(days=unconfirmed_days)}})


def _reserve(customer, product_id: int):
    return customer.post("/api/reservations", json={"product_id": product_id, "quantity": 1}, headers=HEADERS)


def _order(customer, product_id: int, quantity: int = 1):
    body = {"items": [{"product_id": product_id, "quantity": quantity}], "lat": CENTER[0], "lng": CENTER[1],
            "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    return customer.post("/api/orders", json=body, headers=HEADERS)


def _quantity(product_id: int) -> int:
    return Database().products.get(product_id).quantity


def _arrival_failures(product_id: int, since: datetime) -> list:
    return Database().inventory_events.find({"product_id": product_id, "reason": InventoryReason.ARRIVAL_FAILURE.value,
                                             "created_at": {"$gte": since}})


# ---------------------------------------------------------------- the rule


def test_the_14_day_boundary(rule_on):
    now = utcnow()
    limit = 14 * DAY
    assert not is_call_only(now - limit, None, now)
    assert is_call_only(now - limit - SECOND, None, now)
    assert is_call_only(None, None, now)  # never counted
    # Unconfirmed by an arrival failure: call-only once that has lasted more than 14 days, however recent the count.
    assert not is_call_only(now - DAY, now - limit, now)
    assert is_call_only(now - DAY, now - limit - SECOND, now)
    assert not is_call_only(now - 3 * DAY, now - 3 * DAY, now)


def test_no_effect_before_call_only_from(monkeypatch):
    monkeypatch.setattr(settings, "call_only_from", date(2027, 6, 1))
    start = call_only_since()
    # The IST day starts at 18:30 UTC the day before.
    assert start == datetime(2027, 5, 31, 18, 30)
    ancient = start - 400 * DAY
    assert not is_call_only(ancient, ancient, start - SECOND)
    assert is_call_only(ancient, ancient, start)


def test_the_rule_is_off_before_call_only_from(customer, ids, restore, monkeypatch):
    """The rule ships off: call_only_from defaults to 1 Jun 2027, and until that day a listing untouched for months is
    still reserved. The day is moved to after now so the test does not depend on the real date."""
    assert type(settings).model_fields["call_only_from"].default == date(2027, 6, 1)
    monkeypatch.setattr(settings, "call_only_from", (utcnow() + 30 * DAY).date())
    assert utcnow() < call_only_since()
    _age(ids["tape"], 90)
    listing = customer.get(f"/api/products/{ids['tape']}", headers=HEADERS).json()
    assert listing["call_only"] is False and listing["call_phone"] is None
    assert _reserve(customer, ids["tape"]).status_code == 200


# ---------------------------------------------------------------- what customers see, and the 409s


def test_public_listing_carries_call_only_and_the_shops_phone(client, ids, rule_on, restore):
    db = Database()
    db.shops.update_one({"_id": ids["shop_a"]}, {"$set": {"phone": "9000011111"}})
    try:
        _age(ids["tape"], 15)
        detail = client.get(f"/api/products/{ids['tape']}", headers=HEADERS).json()
        assert detail["call_only"] is True and detail["call_phone"] == "9000011111"
        found = client.get("/api/search", params={"q": "teflon tape", "lat": CENTER[0], "lng": CENTER[1]},
                           headers=HEADERS).json()
        offers = [o for g in found["groups"] for o in g["offers"]]
        assert {o["id"]: o["call_only"] for o in offers}[ids["tape"]] is True
        _age(ids["tape"], 13)
        detail = client.get(f"/api/products/{ids['tape']}", headers=HEADERS).json()
        assert detail["call_only"] is False and detail["call_phone"] is None
    finally:
        db.shops.update_one({"_id": ids["shop_a"]}, {"$set": {"phone": None}})


def test_ranking_is_unchanged_by_call_only(client, ids, monkeypatch, restore):
    """A call-only listing ranks as a stale one: turning the rule on does not move any result."""
    _age(ids["tape"], 15)
    params = {"q": "teflon tape", "lat": CENTER[0], "lng": CENTER[1], "radius_km": 30}

    def order():
        found = client.get("/api/search", params=params, headers=HEADERS).json()
        return [(g["key"], g["score"], [o["id"] for o in g["offers"]]) for g in found["groups"]]

    before = order()
    monkeypatch.setattr(settings, "call_only_from", date(2026, 1, 1))
    assert order() == before


def test_reserving_a_call_only_listing_is_refused(customer, ids, rule_on, restore):
    _age(ids["tape"], 15)
    res = _reserve(customer, ids["tape"])
    assert res.status_code == 409 and res.json()["error"]["code"] == "listing_call_only"
    _age(ids["tape"], 3, unconfirmed_days=15)
    res = _reserve(customer, ids["tape"])
    assert res.status_code == 409 and res.json()["error"]["code"] == "listing_call_only"


def test_an_order_with_a_call_only_listing_is_refused(customer, ids, rule_on, restore):
    _age(ids["charger"], 1)
    _age(ids["tape"], 15)
    body = {"items": [{"product_id": ids["charger"], "quantity": 1}, {"product_id": ids["tape"], "quantity": 1}],
            "lat": CENTER[0], "lng": CENTER[1], "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    res = customer.post("/api/orders", json=body, headers=HEADERS)
    assert res.status_code == 409 and res.json()["error"]["code"] == "listing_call_only"
    assert not Database().orders.find({"customer_id": {"$exists": True}, "status": "PENDING"})


# ---------------------------------------------------------------- any stock update restores it


@pytest.mark.parametrize("update", ["same_count", "new_count", "restock", "import", "whatsapp"])
def test_any_stock_update_restores_the_listing(customer, ids, rule_on, restore, update):
    db = Database()
    _age(ids["tape"], 30, unconfirmed_days=20)
    product = db.products.get(ids["tape"])
    owner_id = db.shops.get(ids["shop_a"]).owner_id
    assert is_call_only(product.stock_updated_at, product.stock_unconfirmed_at)
    if update == "same_count":
        set_stock(db, product, product.quantity, owner_id)
    elif update == "new_count":
        set_stock(db, product, product.quantity - 1, owner_id)
    elif update == "restock":
        change_stock(db, product, 5, InventoryReason.RESTOCK, owner_id)
    elif update == "import":
        apply_bulk(db, db.shops.get(ids["shop_a"]), owner_id, [], [(product, {}, product.quantity + 2)])
    else:
        set_stock(db, product, product.quantity, owner_id, reason=InventoryReason.WHATSAPP_UPDATE)
    product = db.products.get(ids["tape"])
    assert not is_call_only(product.stock_updated_at, product.stock_unconfirmed_at)
    assert customer.get(f"/api/products/{ids['tape']}", headers=HEADERS).json()["call_only"] is False
    assert _reserve(customer, ids["tape"]).status_code == 200


# ---------------------------------------------------------------- the owner's out-of-stock reason


@pytest.mark.parametrize("ready", [False, True])
def test_out_of_stock_reason_on_a_reservation_is_an_arrival_failure(customer, owner_a, ids, restore, ready):
    db = Database()
    r = _reserve(customer, ids["tape"]).json()
    owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
    if ready:
        owner_a.post(f"/api/owner/reservations/{r['id']}/ready", json={}, headers=HEADERS)
    held = _quantity(ids["tape"])
    started = utcnow()
    res = owner_a.post(f"/api/owner/reservations/{r['id']}/cancel", json={"reason": OUT_OF_STOCK_AFTER_CONFIRM},
                       headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "CANCELLED" and res.json()["close_reason"] == OUT_OF_STOCK_AFTER_CONFIRM
    assert _quantity(ids["tape"]) == held  # the unit that was not there is not put back
    events = _arrival_failures(ids["tape"], started)
    assert [(e.delta, e.quantity_after) for e in events] == [(0, held)]
    assert db.products.get(ids["tape"]).stock_unconfirmed_at is not None
    stored = db.reservations.get(r["id"])
    assert (stored.stock_held, stored.close_reason, stored.arrival_reported_at) == \
        (False, OUT_OF_STOCK_AFTER_CONFIRM, None)
    # The customer sees a cancelled reservation, not the code, and is told in the plain cancelled words.
    mine = customer.get(f"/api/reservations/{r['id']}", headers=HEADERS).json()
    assert mine["status"] == "CANCELLED" and mine["close_reason"] is None
    assert all(e["note"] is None for e in mine["timeline"])
    told = db.notifications.find({"kind": "reservation_cancelled", "created_at": {"$gte": started}})
    assert told and all(OUT_OF_STOCK_AFTER_CONFIRM not in (n.body or "") for n in told)


@pytest.mark.parametrize("prepared", [False, True])
def test_out_of_stock_reason_on_an_order_is_an_arrival_failure(customer, owner_a, ids, restore, prepared):
    db = Database()
    o = _order(customer, ids["tape"], 2).json()
    owner_a.post(f"/api/owner/orders/{o['id']}/confirm", json={}, headers=HEADERS)
    if prepared:
        owner_a.post(f"/api/owner/orders/{o['id']}/prepare", json={}, headers=HEADERS)
    held = _quantity(ids["tape"])
    started = utcnow()
    res = owner_a.post(f"/api/owner/orders/{o['id']}/cancel", json={"reason": OUT_OF_STOCK_AFTER_CONFIRM},
                       headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "CANCELLED" and res.json()["close_reason"] == OUT_OF_STOCK_AFTER_CONFIRM
    assert _quantity(ids["tape"]) == held
    assert [(e.delta, e.quantity_after) for e in _arrival_failures(ids["tape"], started)] == [(0, held)]
    assert db.products.get(ids["tape"]).stock_unconfirmed_at is not None
    assert db.orders.get(o["id"]).stock_held is False
    mine = customer.get(f"/api/orders/{o['id']}", headers=HEADERS).json()
    assert mine["status"] == "CANCELLED" and mine["close_reason"] is None
    told = db.notifications.find({"kind": "order_cancelled", "created_at": {"$gte": started}})
    assert told and all(OUT_OF_STOCK_AFTER_CONFIRM not in (n.body or "") for n in told)


@pytest.mark.parametrize("reason", ["Customer asked to cancel", "Shop closing early today", None])
def test_other_reasons_release_the_hold(customer, owner_a, ids, restore, reason):
    before = _quantity(ids["tape"])
    started = utcnow()
    r = _reserve(customer, ids["tape"]).json()
    owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
    assert _quantity(ids["tape"]) == before - 1
    res = owner_a.post(f"/api/owner/reservations/{r['id']}/cancel", json={"reason": reason}, headers=HEADERS).json()
    assert res["close_reason"] == reason
    assert _quantity(ids["tape"]) == before

    o = _order(customer, ids["tape"], 2).json()
    owner_a.post(f"/api/owner/orders/{o['id']}/confirm", json={}, headers=HEADERS)
    assert _quantity(ids["tape"]) == before - 2
    res = owner_a.post(f"/api/owner/orders/{o['id']}/cancel", json={"reason": reason}, headers=HEADERS).json()
    assert res["close_reason"] == reason
    assert _quantity(ids["tape"]) == before
    assert not _arrival_failures(ids["tape"], started)
    assert Database().products.get(ids["tape"]).stock_unconfirmed_at is None


def test_only_the_owner_can_record_out_of_stock(customer, owner_a, ids, restore):
    """A customer who cancels with the owner's code just cancels: the hold is released and the code is not stored. A
    request the owner has not confirmed holds nothing, so the code there is no arrival failure either."""
    before = _quantity(ids["tape"])
    started = utcnow()
    r = _reserve(customer, ids["tape"]).json()
    owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
    res = customer.post(f"/api/reservations/{r['id']}/cancel", json={"reason": OUT_OF_STOCK_AFTER_CONFIRM},
                        headers=HEADERS)
    assert res.status_code == 200, res.text
    assert Database().reservations.get(r["id"]).close_reason is None
    assert _quantity(ids["tape"]) == before

    o = _order(customer, ids["tape"]).json()
    res = owner_a.post(f"/api/owner/orders/{o['id']}/cancel", json={"reason": OUT_OF_STOCK_AFTER_CONFIRM},
                       headers=HEADERS)
    assert res.status_code == 200, res.text
    assert Database().orders.get(o["id"]).close_reason is None
    assert _quantity(ids["tape"]) == before
    assert not _arrival_failures(ids["tape"], started)


@pytest.mark.parametrize("typed", [OUT_OF_STOCK_AFTER_CONFIRM, reservations.NOT_AVAILABLE_ON_ARRIVAL])
def test_a_failed_delivery_never_stores_a_reserved_reason(customer, owner_a, ids, restore, typed):
    """A failed delivery's reason is free text: one typed as an arrival-failure code is not stored, so the gate KPI
    (kpis.arrival_failures) can never count it. A real reason is kept."""
    for reason, stored in ((typed, None), ("Customer not at home", "Customer not at home")):
        o = _order(customer, ids["tape"]).json()
        for action in ("confirm", "prepare", "dispatch"):
            assert owner_a.post(f"/api/owner/orders/{o['id']}/{action}", json={}, headers=HEADERS).status_code == 200
        res = owner_a.post(f"/api/owner/orders/{o['id']}/fail", json={"reason": reason}, headers=HEADERS)
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "DELIVERY_FAILED"
        assert Database().orders.get(o["id"]).close_reason == stored
        owner_a.post(f"/api/owner/orders/{o['id']}/return", json={}, headers=HEADERS)
