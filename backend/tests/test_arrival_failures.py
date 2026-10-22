"""Arrival reports and the arrival-failure record (P9.1, plan section 8.17): a customer's "Not available when I
arrived" on their own CONFIRMED or READY_FOR_PICKUP reservation closes it without putting the held units back, writes
an arrival_failure inventory event with delta 0, unconfirms the listing until the shop next counts it, and tells the
owner in the app and on WhatsApp (through the fake client). Every other cancellation behaves as before."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import Forbidden
from app.main import app
from app.models import InventoryReason, Role
from app.schemas.serializers import STALE_LABEL, listing
from app.services import notification_text as words
from app.services import reservations, search, whatsapp
from app.services.inventory import apply_bulk, change_stock, set_stock
from app.services.privacy import set_consent
from app.services.search import SearchParams, run_search
from tests.conftest import CENTER, HEADERS, login

OWNER_PHONE = "9000054321"


def _reserve(customer, owner, product_id: int, *, ready: bool = False) -> dict:
    r = customer.post("/api/reservations", json={"product_id": product_id, "quantity": 1}, headers=HEADERS)
    assert r.status_code == 200, r.text
    r = owner.post(f"/api/owner/reservations/{r.json()['id']}/confirm", json={}, headers=HEADERS).json()
    if ready:
        r = owner.post(f"/api/owner/reservations/{r['id']}/ready", json={}, headers=HEADERS).json()
    return customer.get(f"/api/reservations/{r['id']}", headers=HEADERS).json()  # as the customer sees it


def _report(customer, reservation_id: int):
    return customer.post(f"/api/reservations/{reservation_id}/not-available", json={}, headers=HEADERS)


@pytest.fixture
def restore(ids):
    """Put the seeded listings' counts and confirmation back after a test that reports or counts them, and close any
    reservation a failed test left open, so the next one can reserve the same item. The notifications a test caused
    are removed too: other modules' tests look for notifications made in the last second."""
    db = Database()
    saved = {p.id: (p.quantity, p.stock_updated_at) for p in db.products.find({})}
    started = utcnow()
    yield
    db.notifications.delete_many({"created_at": {"$gte": started}})
    db.reservations.update_many({"status": {"$in": [s.value for s in reservations.ACTIVE]}},
                                {"$set": {"status": "CANCELLED", "stock_held": False, "closed_at": utcnow()}})
    for pid, (quantity, at) in saved.items():
        db.products.update_one({"_id": pid}, {"$set": {"quantity": quantity, "stock_updated_at": at,
                                                       "stock_unconfirmed_at": None}})


# ---------------------------------------------------------------- who may report, and when


def test_only_the_customers_own_held_reservation_can_be_reported_and_only_once(customer, owner_a, ids, restore):
    db = Database()
    # A request the shop has not confirmed holds nothing: it can only be cancelled.
    requested = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1},
                              headers=HEADERS).json()
    assert "not_available" not in requested["actions"]
    refused = _report(customer, requested["id"])
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "invalid_transition"
    customer.post(f"/api/reservations/{requested['id']}/cancel", json={}, headers=HEADERS)

    held = _reserve(customer, owner_a, ids["tape"])
    assert "not_available" in held["actions"]
    # Nobody else can report it: another customer cannot see it, the owner and an admin may not say it was missing.
    with TestClient(app) as other_client:
        other = login(other_client, "other@example.com")
        assert _report(other, held["id"]).status_code == 404
    owner_view = owner_a.get("/api/owner/reservations", headers=HEADERS).json()
    assert all("not_available" not in r["actions"] for r in owner_view if r["id"] == held["id"])
    owner_user = db.users.find_one({"email": "a@example.com"})
    admin_user = db.users.find_one({"role": Role.ADMIN.value})
    for user in (owner_user, admin_user):
        with pytest.raises(Forbidden):
            reservations.transition(db, held["id"], reservations.NOT_AVAILABLE, user)
    assert db.reservations.get(held["id"]).arrival_reported_at is None

    first = _report(customer, held["id"])
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "CANCELLED" and body["close_reason"] == "not_available_on_arrival"
    assert body["actions"] == []
    stored = db.reservations.get(held["id"])
    assert stored.arrival_reported_at is not None and stored.closed_at == stored.arrival_reported_at
    assert stored.stock_held is False

    again = _report(customer, held["id"])
    assert again.status_code == 409 and "already reported" in again.json()["error"]["message"]
    assert db.inventory_events.count({"product_id": ids["tape"], "reason": InventoryReason.ARRIVAL_FAILURE.value,
                                      "created_at": stored.arrival_reported_at}) == 1


def test_a_ready_for_pickup_reservation_can_be_reported(customer, owner_a, ids, restore):
    ready = _reserve(customer, owner_a, ids["charger"], ready=True)
    assert ready["status"] == "READY_FOR_PICKUP" and "not_available" in ready["actions"]
    res = _report(customer, ready["id"])
    assert res.status_code == 200, res.text
    assert res.json()["close_reason"] == "not_available_on_arrival"


# ---------------------------------------------------------------- the stock, the event and the unconfirmed listing


def test_a_report_keeps_the_count_records_the_event_and_unconfirms_until_the_next_count(customer, owner_a, ids,
                                                                                        restore):
    db = Database()
    before = db.products.get(ids["tape"]).quantity
    held = _reserve(customer, owner_a, ids["tape"])
    assert db.products.get(ids["tape"]).quantity == before - 1  # the hold

    assert _report(customer, held["id"]).status_code == 200
    product = db.products.get(ids["tape"])
    assert product.quantity == before - 1, "the units were not there: nothing goes back"
    reported_at = db.reservations.get(held["id"]).arrival_reported_at
    [event] = db.inventory_events.find({"product_id": product.id, "reason": "arrival_failure",
                                        "created_at": reported_at})
    assert (event.delta, event.quantity_after, event.shop_id) == (0, before - 1, ids["shop_a"])
    assert db.inventory_events.count({"product_id": product.id, "reason": "reservation_release",
                                      "created_at": {"$gte": reported_at}}) == 0
    assert product.stock_unconfirmed_at == reported_at
    # Fresh by its time, but unconfirmed: labelled and banded as stock not confirmed recently.
    shown = listing(product, now=product.stock_updated_at + timedelta(minutes=1))["freshness"]
    assert shown == {"band": "stale", "label": STALE_LABEL}
    # The owner's own view of the listing says so too.
    mine = owner_a.get("/api/owner/products", params={"q": "thread seal"}, headers=HEADERS)
    assert mine.status_code == 200, mine.text
    row = next(p for p in mine.json()["items"] if p["id"] == product.id)
    assert row["freshness"]["label"] == STALE_LABEL

    # The shop's next count clears it, even one that leaves the number unchanged (the web app).
    res = owner_a.put(f"/api/owner/products/{product.id}/stock", json={"quantity": product.quantity}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert db.products.get(product.id).stock_unconfirmed_at is None
    assert res.json()["freshness"]["band"] == "fresh"


def test_a_whatsapp_count_or_an_import_clears_it_and_a_hold_does_not(ids, restore):
    db = Database()
    owner = db.users.find_one({"email": "a@example.com"})
    shop = db.shops.get(ids["shop_a"])

    def unconfirm():
        db.products.update_one({"_id": ids["tape"]}, {"$set": {"stock_unconfirmed_at": utcnow()}})
        return db.products.get(ids["tape"])

    # A stock count sent on WhatsApp (P4.4) goes through set_stock with its own reason.
    product = unconfirm()
    db.transaction(lambda: set_stock(db, product, product.quantity + 2, owner.id, InventoryReason.WHATSAPP_UPDATE))
    assert db.products.get(ids["tape"]).stock_unconfirmed_at is None

    # An Excel or CSV import (apply_bulk) that sets the count.
    product = unconfirm()
    db.transaction(lambda: apply_bulk(db, shop, owner.id, [], [(product, {}, product.quantity)]))
    assert db.products.get(ids["tape"]).stock_unconfirmed_at is None

    # An import row that leaves stock alone, and a reservation's hold and release, are not the shop's count.
    product = unconfirm()
    db.transaction(lambda: apply_bulk(db, shop, owner.id, [], [(product, {"price": product.price}, None)]))
    db.transaction(lambda: change_stock(db, product, -1, InventoryReason.RESERVATION_HOLD, None))
    db.transaction(lambda: change_stock(db, product, 1, InventoryReason.RESERVATION_RELEASE, None))
    assert db.products.get(ids["tape"]).stock_unconfirmed_at is not None


# ---------------------------------------------------------------- search: band, multiplier and the in-stock filter


@pytest.fixture
def clock():
    yield search.set_clock
    search.set_clock(None)


def _search(db, **kw):
    return run_search(db, SearchParams(lat=CENTER[0], lng=CENTER[1], radius_km=30, **kw), log=False)


def _group(res, product_id):
    return next((g for g in res["groups"] if any(o["id"] == product_id for o in g["offers"])), None)


def test_an_unconfirmed_listing_ranks_and_filters_as_over_seven_days(ids, clock, restore):
    db = Database()
    charger = db.products.get(ids["charger"])
    clock(charger.stock_updated_at + timedelta(hours=1))  # fresh by its time
    fresh = _group(_search(db), charger.id)
    assert fresh["best_offer"]["freshness"]["band"] == "fresh"
    assert _group(_search(db, in_stock_only=True), charger.id) is not None

    db.products.update_one({"_id": charger.id}, {"$set": {"stock_unconfirmed_at": utcnow()}})
    unconfirmed = _group(_search(db), charger.id)
    assert unconfirmed["best_offer"]["freshness"] == {"band": "stale", "label": STALE_LABEL}
    assert unconfirmed["score"] == pytest.approx(fresh["score"] * 0.6, abs=1e-3)
    assert _group(_search(db, in_stock_only=True), charger.id) is None, "left out of the in-stock-only filter"


def test_a_group_with_one_unconfirmed_offer_ranks_by_its_confirmed_one(ids, clock, restore):
    db = Database()
    tape, far = db.products.get(ids["tape"]), db.products.get(ids["tape_far"])
    clock(max(tape.stock_updated_at, far.stock_updated_at) + timedelta(hours=1))
    both = _group(_search(db), tape.id)["score"]
    db.products.update_one({"_id": tape.id}, {"$set": {"stock_unconfirmed_at": utcnow()}})
    group = _group(_search(db), tape.id)
    assert group["score"] == pytest.approx(both, abs=1e-3)  # the other shop's offer is still confirmed
    offers = {o["id"]: o for o in group["offers"]}
    assert offers[tape.id]["freshness"]["band"] == "stale" and offers[far.id]["freshness"]["band"] == "fresh"
    in_stock = {o["id"] for g in _search(db, in_stock_only=True)["groups"] for o in g["offers"]}
    assert far.id in in_stock and tape.id not in in_stock


# ---------------------------------------------------------------- every other cancellation is unchanged


def test_an_ordinary_cancellation_still_releases_the_hold(customer, owner_a, ids, restore):
    db = Database()
    before = db.products.get(ids["charger"]).quantity
    held = _reserve(customer, owner_a, ids["charger"])
    assert db.products.get(ids["charger"]).quantity == before - 1
    # Even a typed reason equal to the report's code is an ordinary cancellation.
    res = customer.post(f"/api/reservations/{held['id']}/cancel", json={"reason": "not_available_on_arrival"},
                        headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "CANCELLED" and res.json()["close_reason"] is None
    product = db.products.get(ids["charger"])
    assert product.quantity == before, "the held unit is back on the shelf"
    assert product.stock_unconfirmed_at is None
    stored = db.reservations.get(held["id"])
    assert stored.arrival_reported_at is None and stored.stock_held is False
    assert db.inventory_events.count({"product_id": product.id, "reason": "reservation_release",
                                      "created_at": stored.closed_at}) == 1
    assert db.inventory_events.count({"product_id": product.id, "reason": "arrival_failure",
                                      "created_at": stored.closed_at}) == 0
    owner = db.users.find_one({"email": "a@example.com"})
    note = db.notifications.find({"user_id": owner.id}, sort=[("_id", -1)], limit=1)[0]
    assert note.kind == "reservation_cancelled" and held["code"] in note.title

    with_reason = _reserve(customer, owner_a, ids["charger"])
    res = customer.post(f"/api/reservations/{with_reason['id']}/cancel", json={"reason": "changed my mind"},
                        headers=HEADERS)
    assert res.json()["close_reason"] == "changed my mind"
    assert db.products.get(ids["charger"]).quantity == before


# ---------------------------------------------------------------- the owner's notification


@pytest.fixture
def owner_on_whatsapp(ids, monkeypatch):
    """Owner A with a verified phone and the WhatsApp consent, WhatsApp notifications on (the fake client)."""
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    db = Database()
    owner = db.users.find_one({"email": "a@example.com"})
    saved = {"phone": owner.phone, "phone_verified_at": owner.phone_verified_at, "language": owner.language}
    consents = {r["_id"] for r in db.mongo["consents"].find({}, {"_id": 1})}
    db.users.update_one({"_id": owner.id}, {"$set": {"phone": OWNER_PHONE, "phone_verified_at": utcnow()}})
    set_consent(db, owner.id, "whatsapp", True)
    db.mongo[whatsapp.OUTBOX].delete_many({})
    whatsapp.FAKE.reset()
    yield owner
    db.mongo[whatsapp.OUTBOX].delete_many({})
    whatsapp.FAKE.reset()
    db.mongo["consents"].delete_many({"_id": {"$nin": list(consents)}, "user_id": owner.id})
    db.users.update_one({"_id": owner.id}, {"$set": saved})


@pytest.mark.parametrize("language", ["en", "kn"])
def test_the_owner_is_told_in_the_app_and_on_whatsapp(customer, owner_a, ids, restore, owner_on_whatsapp, language):
    db = Database()
    owner = owner_on_whatsapp
    db.users.update_one({"_id": owner.id}, {"$set": {"language": language}})
    held = _reserve(customer, owner_a, ids["tape"])
    db.mongo[whatsapp.OUTBOX].delete_many({})
    assert _report(customer, held["id"]).status_code == 200
    product = db.products.get(ids["tape"])

    note = db.notifications.find({"user_id": owner.id, "kind": "arrival_failure"}, sort=[("_id", -1)], limit=1)[0]
    params = {"code": held["code"], "product": product.name, "quantity": product.quantity}
    assert (note.title, note.body) == words.render("arrival_failure", language, params)
    assert note.title == f"Item not available for reservation {held['code']}"
    assert note.body == f"The customer found {product.name} missing. NearShop showed {product.quantity} in stock."
    assert note.link == f"/shop/inventory?focus={product.id}"  # straight to updating its stock

    [row] = list(db.mongo[whatsapp.OUTBOX].find({"user_id": owner.id}))
    assert row["kind"] == "arrival_failure" and row["template"] == "nearshop_shop_arrival_report"
    assert row["params"] == [note.title, note.body] and row["buttons"] == []
    assert row["language"] == whatsapp.template_language(language)
    assert row["subject"] == f"reservation:{held['code']}"
    assert whatsapp.send_due(Database(), client=whatsapp.FAKE)["sent"] == 1
    assert whatsapp.FAKE.sent[-1]["template"] == "nearshop_shop_arrival_report"


def test_the_arrival_message_awaits_the_teams_kannada():
    """P9.1 uses only Kannada the team supplies: until it arrives, a Kannada reader gets the English text, and the
    key is listed as awaiting it."""
    assert "arrival_failure" in words.AWAITING_KANNADA
    params = {"code": "R-7KQ2MX", "product": "Astral PTFE Thread Seal Tape", "quantity": 50}
    assert words.render("arrival_failure", "kn", params) == words.render("arrival_failure", "en", params) == (
        "Item not available for reservation R-7KQ2MX",
        "The customer found Astral PTFE Thread Seal Tape missing. NearShop showed 50 in stock.")
