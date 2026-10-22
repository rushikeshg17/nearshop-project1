from datetime import timedelta

from app.core.database import Database, utcnow
from app.services.reservations import expire_due
from tests.conftest import CENTER, HEADERS


def stock(product_id: int) -> int:
    return Database().products.get(product_id).quantity


def test_reservation_happy_path_holds_stock_and_records_sale(customer, owner_a, ids):
    before = stock(ids["tape"])
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 2}, headers=HEADERS).json()
    assert r["status"] == "REQUESTED"
    assert stock(ids["tape"]) == before  # nothing held until the shop confirms

    r = owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS).json()
    assert r["status"] == "CONFIRMED"
    assert stock(ids["tape"]) == before - 2

    r = owner_a.post(f"/api/owner/reservations/{r['id']}/ready", json={}, headers=HEADERS).json()
    assert r["status"] == "READY_FOR_PICKUP"
    # Completing needs the code the customer shows (P3.5; tests/test_reviews.py covers wrong codes) and how the
    # customer paid (P6.1; tests/test_payment_direct.py).
    r = owner_a.post(f"/api/owner/reservations/{r['id']}/complete", json={"code": r["code"], "payment_method": "cod"},
                     headers=HEADERS).json()
    assert r["status"] == "COMPLETED"
    assert stock(ids["tape"]) == before - 2  # the held units left with the customer

    assert Database().sales_history.count({"basket_id": r["code"], "is_demo": False}) == 1

    # One review per completed reservation, from an account created at least 3 days before the purchase (P3.5).
    # The shared customer's created_at is put back afterwards, so later tests see the account as seeded.
    db = Database()
    created_at = db.users.find_raw({"email": "cust@example.com"}, {"created_at": 1})[0]["created_at"]
    db.users.update_one({"email": "cust@example.com"}, {"$set": {"created_at": utcnow() - timedelta(days=30)}})
    try:
        ok = customer.post("/api/reviews", json={"reservation_id": r["id"], "rating": 5}, headers=HEADERS)
        assert ok.status_code == 200
        dup = customer.post("/api/reviews", json={"reservation_id": r["id"], "rating": 4}, headers=HEADERS)
        assert dup.status_code == 409
    finally:
        db.users.update_one({"email": "cust@example.com"}, {"$set": {"created_at": created_at}})


def test_cannot_review_unfinished_purchase(customer, ids):
    r = customer.post("/api/reservations", json={"product_id": ids["charger"], "quantity": 1}, headers=HEADERS).json()
    res = customer.post("/api/reviews", json={"reservation_id": r["id"], "rating": 5}, headers=HEADERS)
    assert res.status_code == 409
    customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)


def test_duplicate_and_oversized_reservations_rejected(customer, ids):
    too_many = customer.post("/api/reservations", json={"product_id": ids["charger"], "quantity": 10}, headers=HEADERS)
    assert too_many.status_code == 409
    assert too_many.json()["error"]["code"] == "insufficient_stock"

    first = customer.post("/api/reservations", json={"product_id": ids["charger"], "quantity": 1}, headers=HEADERS)
    again = customer.post("/api/reservations", json={"product_id": ids["charger"], "quantity": 1}, headers=HEADERS)
    assert again.status_code == 409
    customer.post(f"/api/reservations/{first.json()['id']}/cancel", json={}, headers=HEADERS)


def test_cancel_after_confirm_releases_stock(customer, owner_a, ids):
    before = stock(ids["charger"])
    r = customer.post("/api/reservations", json={"product_id": ids["charger"], "quantity": 1}, headers=HEADERS).json()
    owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
    assert stock(ids["charger"]) == before - 1
    res = customer.post(f"/api/reservations/{r['id']}/cancel", json={"reason": "changed my mind"}, headers=HEADERS)
    assert res.json()["status"] == "CANCELLED"
    assert stock(ids["charger"]) == before


def test_other_shop_owner_cannot_act(customer, owner_b, ids):
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    res = owner_b.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
    assert res.status_code == 404  # scoped to the owner's own shop; not even visible
    customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)


def test_invalid_transition_rejected(customer, owner_a, ids):
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    res = owner_a.post(f"/api/owner/reservations/{r['id']}/complete", json={}, headers=HEADERS)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "invalid_transition"
    customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)


def test_expiry_releases_held_stock(customer, owner_a, ids):
    before = stock(ids["tape"])
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 3}, headers=HEADERS).json()
    owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS)
    assert stock(ids["tape"]) == before - 3
    db = Database()
    db.reservations.set(db.reservations.get(r["id"]), expires_at=utcnow() - timedelta(minutes=1))
    expire_due(db)
    assert db.reservations.get(r["id"]).status.value == "EXPIRED"
    assert stock(ids["tape"]) == before


def test_delivery_order_lifecycle(customer, owner_a, ids):
    before = stock(ids["tape"])
    body = {"items": [{"product_id": ids["tape"], "quantity": 2}], "lat": CENTER[0], "lng": CENTER[1],
            "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    quote = customer.post("/api/orders/quote", json={k: body[k] for k in ("items", "lat", "lng")}, headers=HEADERS).json()
    assert quote["eligible"] and quote["delivery_fee"] == 30

    o = customer.post("/api/orders", json=body, headers=HEADERS).json()
    assert o["status"] == "PENDING" and o["total"] == 80 + 30
    for action, expected in [("confirm", "SHOP_CONFIRMED"), ("prepare", "PREPARING"), ("dispatch", "OUT_FOR_DELIVERY"),
                             ("deliver", "DELIVERED")]:
        # Delivering needs the 4-digit code only the customer sees (P3.5) and how the customer paid (P6.1).
        body = {"code": Database().orders.get(o["id"]).delivery_code, "payment_method": "cod"} \
            if action == "deliver" else {}
        o = owner_a.post(f"/api/owner/orders/{o['id']}/{action}", json=body, headers=HEADERS).json()
        assert o["status"] == expected
    assert (o["payment_method"], o["payment_status"]) == ("cod", "paid")
    assert stock(ids["tape"]) == before - 2


def test_delivery_outside_radius_refused(customer, ids):
    body = {"items": [{"product_id": ids["tape"], "quantity": 1}], "lat": CENTER[0] + 0.1, "lng": CENTER[1],
            "address": "Somewhere far away", "phone": "9876543210"}
    res = customer.post("/api/orders", json=body, headers=HEADERS)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "delivery_unavailable"


def test_failed_delivery_returns_stock(customer, owner_a, ids):
    before = stock(ids["tape"])
    body = {"items": [{"product_id": ids["tape"], "quantity": 1}], "lat": CENTER[0], "lng": CENTER[1],
            "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    o = customer.post("/api/orders", json=body, headers=HEADERS).json()
    for action in ("confirm", "prepare", "dispatch"):
        owner_a.post(f"/api/owner/orders/{o['id']}/{action}", json={}, headers=HEADERS)
    assert stock(ids["tape"]) == before - 1
    owner_a.post(f"/api/owner/orders/{o['id']}/fail", json={"reason": "Not reachable"}, headers=HEADERS)
    o = owner_a.post(f"/api/owner/orders/{o['id']}/return", json={}, headers=HEADERS).json()
    assert o["status"] == "RETURNED_TO_SHOP"
    assert stock(ids["tape"]) == before
