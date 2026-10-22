"""Public stock bands (P10.1, plan section 8.20): outside the shop, a listing's quantity is capped at
settings.public_stock_cap unless the shop opted in to exact counts (Shop.show_exact_stock); one reservation or order
line may ask for at most the cap; and no insufficient_stock error states a count. Owners and admins see exact counts."""
import re
from contextlib import contextmanager

import pytest

from app.core.config import settings
from app.core.database import Database
from app.models import PriceAnomaly
from app.schemas.serializers import listing
from app.services import search as search_module
from app.services.search import SearchParams, run_search
from tests.conftest import CENTER, HEADERS

CAP = 10
EXACT = 37  # above the cap, so a capped response and an exact one differ


@pytest.fixture(autouse=True)
def _cap_is_default():
    assert settings.public_stock_cap == CAP


@contextmanager
def tape_stock(ids, quantity: int, *, exact: bool = False):
    """Shop A's tape at `quantity` units, with Shop A's opt-in as given; both restored afterwards."""
    db = Database()
    before = db.products.get(ids["tape"]).quantity
    db.products.update_one({"_id": ids["tape"]}, {"$set": {"quantity": quantity}})
    db.shops.update_one({"_id": ids["shop_a"]}, {"$set": {"show_exact_stock": exact}})
    try:
        yield
    finally:
        db.products.update_one({"_id": ids["tape"]}, {"$set": {"quantity": before}})
        db.shops.update_one({"_id": ids["shop_a"]}, {"$unset": {"show_exact_stock": ""}})


def _public_offers(client, ids) -> list[dict]:
    """Every listing of Shop A's tape that the public endpoints return."""
    loc = {"lat": CENTER[0], "lng": CENTER[1]}
    found = []
    detail = client.get(f"/api/products/{ids['tape']}", params=loc).json()
    found.append(detail)
    found += [o for o in client.get(f"/api/products/{ids['tape_far']}", params=loc).json()["other_offers"]
              if o["id"] == ids["tape"]]
    search = client.get("/api/search", params={"q": "teflon tape", **loc, "radius_km": 50}).json()
    for g in search["groups"]:
        found += [o for o in [g["best_offer"], *g["offers"]] if o["id"] == ids["tape"]]
    items = client.get("/api/shops/shop-a/products").json()["items"]
    found += [o for o in items if o["id"] == ids["tape"]]
    assert len(found) >= 4, "the tape listing should appear on the product page, as an other offer, in search and on the shop page"
    return found


def test_public_responses_capped_without_opt_in(client, ids):
    with tape_stock(ids, EXACT):
        for offer in _public_offers(client, ids):
            assert offer["quantity"] == CAP and offer["stock_exact"] is False
            assert offer["stock_status"] == "in_stock"  # the band comes from the real count, unchanged
        # The shop page says whether the shop shows exact counts.
        assert client.get("/api/shops/shop-a").json()["show_exact_stock"] is False


def test_public_responses_exact_with_opt_in(client, ids):
    with tape_stock(ids, EXACT, exact=True):
        for offer in _public_offers(client, ids):
            assert offer["quantity"] == EXACT and offer["stock_exact"] is True


def test_quantity_below_cap_is_shown_as_is(client, ids):
    with tape_stock(ids, 4):
        detail = client.get(f"/api/products/{ids['tape']}").json()
        assert detail["quantity"] == 4 and detail["stock_status"] == "in_stock" and detail["stock_exact"] is False
    with tape_stock(ids, 0):
        detail = client.get(f"/api/products/{ids['tape']}").json()
        assert detail["quantity"] == 0 and detail["stock_status"] == "out_of_stock"


def test_owner_and_admin_responses_stay_exact(owner_a, admin, ids):
    db = Database()
    with tape_stock(ids, EXACT):
        items = owner_a.get("/api/owner/products", params={"q": "tape"}).json()["items"]
        assert [i["quantity"] for i in items if i["id"] == ids["tape"]] == [EXACT]
        assert "stock_exact" not in items[0]
        anomaly = db.price_anomalies.insert(PriceAnomaly(
            product_id=ids["tape"], shop_id=ids["shop_a"], catalog_item_id=db.products.get(ids["tape"]).catalog_item_id,
            price=40, reference_price=39, deviation_pct=2.5, score=0.1, direction="high", peer_count=3, model_run_id=0))
        try:
            rows = admin.get("/api/admin/anomalies").json()
            assert [r["product"]["quantity"] for r in rows if r["id"] == anomaly.id] == [EXACT]
        finally:
            db.price_anomalies.delete_many({"_id": anomaly.id})


def test_customer_reservation_view_capped_owner_view_exact(customer, owner_a, ids):
    with tape_stock(ids, EXACT):
        r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS)
        assert r.status_code == 200, r.text
        r = r.json()
        try:
            assert r["product"]["quantity_in_stock"] == CAP and r["product"]["stock_exact"] is False
            mine = customer.get(f"/api/reservations/{r['id']}").json()
            assert mine["product"]["quantity_in_stock"] == CAP
            queue = owner_a.get("/api/owner/reservations").json()
            rows = queue if isinstance(queue, list) else queue["items"]
            assert [x["product"]["quantity_in_stock"] for x in rows if x["id"] == r["id"]] == [EXACT]
        finally:
            customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)


def _order(customer, ids, *quantities: int):
    """A delivery order for Shop A's tape, one line per quantity given."""
    body = {"items": [{"product_id": ids["tape"], "quantity": q} for q in quantities], "lat": CENTER[0], "lng": CENTER[1],
            "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    return customer.post("/api/orders", json=body, headers=HEADERS)


def test_cap_enforced_on_reservations_and_order_lines(customer, ids):
    with tape_stock(ids, EXACT):
        res = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": CAP + 1}, headers=HEADERS)
        assert res.status_code == 422 and res.json()["error"]["code"] == "quantity_above_cap"
        assert "call the shop" in res.json()["error"]["message"].lower()
        order = _order(customer, ids, CAP + 1)
        assert order.status_code == 422 and order.json()["error"]["code"] == "quantity_above_cap"
        # Splitting the quantity over several lines of the same product does not get past the cap.
        split = _order(customer, ids, CAP, CAP)
        assert split.status_code == 422 and split.json()["error"]["code"] == "quantity_above_cap"


def test_split_order_lines_count_together_for_stock(customer, ids):
    with tape_stock(ids, 4):
        order = _order(customer, ids, 3, 3)  # each line fits the 4 in stock; together they do not
        assert order.status_code == 409 and order.json()["error"]["code"] == "insufficient_stock"
        assert order.json()["error"]["message"] == "Not enough in stock for that quantity"


def test_shop_settings_reject_null_exact_stock(owner_a, ids):
    res = owner_a.patch("/api/owner/shop", json={"show_exact_stock": None}, headers=HEADERS)
    assert res.status_code == 422
    assert Database().shops.get(ids["shop_a"]).show_exact_stock is False


def test_opted_in_shop_keeps_todays_limits(customer, owner_a, ids):
    with tape_stock(ids, EXACT, exact=True):
        res = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": CAP + 1}, headers=HEADERS)
        assert res.status_code == 200, res.text
        customer.post(f"/api/reservations/{res.json()['id']}/cancel", json={}, headers=HEADERS)
        order = _order(customer, ids, CAP + 5)
        assert order.status_code == 200, order.text
        owner_a.post(f"/api/owner/orders/{order.json()['id']}/cancel", json={"reason": "test"}, headers=HEADERS)
        # The request schemas' limits still apply: 20 a reservation, 50 an order line.
        assert customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 21},
                             headers=HEADERS).status_code == 422


def test_shortage_errors_state_no_number_without_opt_in(customer, ids):
    with tape_stock(ids, 4):
        res = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 6}, headers=HEADERS)
        order = _order(customer, ids, 6)
        for r in (res, order):
            assert r.status_code == 409 and r.json()["error"]["code"] == "insufficient_stock"
            assert r.json()["error"]["message"] == "Not enough in stock for that quantity"
            assert not re.search(r"\d", r.json()["error"]["message"])


def test_shortage_errors_state_the_count_for_opted_in_shop(customer, ids):
    with tape_stock(ids, 4, exact=True):
        res = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 6}, headers=HEADERS)
        order = _order(customer, ids, 6)
        assert res.json()["error"]["message"] == "Only 4 in stock"
        assert order.json()["error"]["message"].startswith("Only 4 of ")


def test_owner_turns_on_exact_stock_in_settings(owner_a, client, ids):
    try:
        assert owner_a.get("/api/owner/shop").json()["show_exact_stock"] is False
        res = owner_a.patch("/api/owner/shop", json={"show_exact_stock": True}, headers=HEADERS)
        assert res.status_code == 200 and res.json()["show_exact_stock"] is True
        assert Database().shops.get(ids["shop_a"]).show_exact_stock is True
        assert client.get(f"/api/products/{ids['tape']}").json()["stock_exact"] is True
        # Still verified: the setting is not one of the fields that send a shop back to review.
        assert res.json()["review_started"] is False
    finally:
        owner_a.patch("/api/owner/shop", json={"show_exact_stock": False}, headers=HEADERS)
    assert Database().shops.get(ids["shop_a"]).show_exact_stock is False


def test_search_order_unchanged_by_bands(ids, monkeypatch):
    """Ranking reads the real stock, never the public count: groups, offers and pins come out in the same order and
    with the same scores as before the change (search serialising offers with the exact-count listing), and whether or
    not the shop shows exact counts."""
    db = Database()

    def snapshot():
        out = []
        for q in ("teflon tape", "charger", ""):
            res = run_search(db, SearchParams(q=q, lat=CENTER[0], lng=CENTER[1], radius_km=50), log=False)
            out.append(([(g["key"], g["score"], g["best_offer"]["id"], [o["id"] for o in g["offers"]])
                         for g in res["groups"]],
                        [(s["id"], s["match_count"], s["in_stock_count"]) for s in res["shops"]]))
        return out

    with tape_stock(ids, EXACT):
        capped = snapshot()
        # Before P10.1, search serialised every offer with listing (exact counts); replay that code path.
        with monkeypatch.context() as m:
            m.setattr(search_module, "public_listing", lambda p, shop=None, now=None, *, seller=None: listing(p, shop, now))
            before = snapshot()
    with tape_stock(ids, EXACT, exact=True):
        exact = snapshot()
    assert capped == before
    assert capped == exact
    assert any(groups for groups, _ in capped)
    # Pins count listings, not units: Shop A's one tape listing with 37 units counts once.
    tape_pins = capped[0][1]
    shop_a_pin = next(p for p in tape_pins if p[0] == ids["shop_a"])
    assert shop_a_pin[1] == 1 and shop_a_pin[2] == 1
