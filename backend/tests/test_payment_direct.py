"""Pay the shop directly by UPI (P6.1, plan section 8.8): the shop's own UPI ID in its settings, the owner recording
cash or UPI when completing a pickup or delivering an order, and the UPI ID only in the customer's own orders and
reservations. NearShop never holds customer money: no gateway and no NearShop account are involved."""
import pytest
from fastapi.testclient import TestClient

from app.core.database import Database
from app.main import app
from tests.conftest import CENTER, HEADERS, login

UPI = "shopa.test@okhdfcbank"


@pytest.fixture
def shop_upi(owner_a, ids):
    """Shop A with a UPI ID and payee name for the test, removed afterwards: the other tests see it without one."""
    res = owner_a.patch("/api/owner/shop", json={"upi_id": UPI, "upi_payee_name": "Shop A Mobiles"}, headers=HEADERS)
    assert res.status_code == 200, res.text
    yield UPI
    Database().shops.update_one({"_id": ids["shop_a"]}, {"$set": {"upi_id": None, "upi_payee_name": None}})


def confirmed_reservation(customer, owner_a, ids) -> dict:
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    assert owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS).status_code == 200
    return r


def out_for_delivery(customer, owner_a, ids) -> dict:
    body = {"items": [{"product_id": ids["tape"], "quantity": 1}], "lat": CENTER[0], "lng": CENTER[1],
            "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    o = customer.post("/api/orders", json=body, headers=HEADERS).json()
    for action in ("confirm", "prepare", "dispatch"):
        assert owner_a.post(f"/api/owner/orders/{o['id']}/{action}", json={}, headers=HEADERS).status_code == 200
    return o


def delivery_code(order_id: int) -> str:
    return Database().orders.get(order_id).delivery_code


# ---------------------------------------------------------------- the shop's UPI ID

@pytest.mark.parametrize("bad", [
    "shopa",                       # no at sign
    "@okhdfcbank",                 # no handle
    "shopa@",                      # no provider
    "shop a@okhdfcbank",           # a space
    "shop@a@okhdfcbank",           # two at signs
    "shopa@ok-hdfc",               # a hyphen in the provider
    "shopa@9bank",                 # a provider that does not start with a letter
    ".shopa@okhdfcbank",           # a handle that does not start with a letter or digit
    "a@",                          # too short and no provider
    "s" * 40 + "@okhdfcbank",      # 51 characters
    "shopa@okhdfcbank<script>",
])
def test_invalid_upi_ids_are_rejected(owner_a, ids, bad):
    res = owner_a.patch("/api/owner/shop", json={"upi_id": bad}, headers=HEADERS)
    assert res.status_code == 422 and res.json()["error"]["field"] == "upi_id", res.text
    assert Database().shops.get(ids["shop_a"]).upi_id is None


def test_upi_id_is_saved_normalised_and_can_be_removed(owner_a, ids):
    db = Database()
    try:
        res = owner_a.patch("/api/owner/shop", json={"upi_id": "  ShopA.Test@OKHDFCBANK ",
                                                      "upi_payee_name": "  Shop   A  "}, headers=HEADERS).json()
        assert res["upi_id"] == "shopa.test@okhdfcbank" and res["upi_payee_name"] == "Shop A"
        # Not an address or phone change: a verified shop is not sent back to review (P3.3).
        assert res["review_started"] is False and res["verification"]["status"] == "verified"
        assert owner_a.get("/api/owner/shop", headers=HEADERS).json()["upi_id"] == "shopa.test@okhdfcbank"
        for shortest, longest in [("a@b", "s" * 39 + "@okhdfcbank")]:  # 3 and 50 characters
            assert owner_a.patch("/api/owner/shop", json={"upi_id": shortest}, headers=HEADERS).status_code == 200
            assert owner_a.patch("/api/owner/shop", json={"upi_id": longest}, headers=HEADERS).status_code == 200
        for bad_name in ("A", "x" * 61, "Shop\u0000A"):
            res = owner_a.patch("/api/owner/shop", json={"upi_payee_name": bad_name}, headers=HEADERS)
            assert res.status_code == 422 and res.json()["error"]["field"] == "upi_payee_name"
        for cleared in (None, ""):
            res = owner_a.patch("/api/owner/shop", json={"upi_id": cleared, "upi_payee_name": cleared},
                                headers=HEADERS).json()
            assert res["upi_id"] is None and res["upi_payee_name"] is None
            assert db.shops.get(ids["shop_a"]).upi_id is None
    finally:
        db.shops.update_one({"_id": ids["shop_a"]}, {"$set": {"upi_id": None, "upi_payee_name": None}})


# ---------------------------------------------------------------- the owner records how the customer paid

def test_complete_requires_a_payment_method(customer, owner_a, ids):
    r = confirmed_reservation(customer, owner_a, ids)
    url = f"/api/owner/reservations/{r['id']}/complete"
    missing = owner_a.post(url, json={"code": r["code"]}, headers=HEADERS)
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "payment_method_required"
    for unknown in ("card", "upi", "prepaid", 1):
        res = owner_a.post(url, json={"code": r["code"], "payment_method": unknown}, headers=HEADERS)
        assert res.status_code == 422 and res.json()["error"]["field"] == "payment_method"
    saved = Database().reservations.get(r["id"])
    assert saved.status.value == "CONFIRMED" and saved.payment_status is None and saved.payment_method is None
    done = owner_a.post(url, json={"code": r["code"], "payment_method": "cod"}, headers=HEADERS)
    assert done.status_code == 200 and done.json()["status"] == "COMPLETED"


def test_deliver_requires_a_payment_method_and_a_missing_one_is_not_a_wrong_code(customer, owner_a, ids):
    o = out_for_delivery(customer, owner_a, ids)
    url = f"/api/owner/orders/{o['id']}/deliver"
    code = delivery_code(o["id"])
    for _ in range(6):  # more than the 5 wrong codes that lock an order (P3.5): a missing method never counts
        res = owner_a.post(url, json={"code": code}, headers=HEADERS)
        assert res.status_code == 422 and res.json()["error"]["code"] == "payment_method_required"
    saved = Database().orders.get(o["id"])
    assert saved.status.value == "OUT_FOR_DELIVERY" and saved.payment_status == "unpaid"
    assert saved.code_failures == 0 and saved.code_locked_until is None
    res = owner_a.post(url, json={"code": code, "payment_method": "cheque"}, headers=HEADERS)
    assert res.status_code == 422 and res.json()["error"]["field"] == "payment_method"
    done = owner_a.post(url, json={"code": code, "payment_method": "cod"}, headers=HEADERS)
    assert done.status_code == 200 and done.json()["status"] == "DELIVERED"


def test_upi_direct_is_refused_when_the_shop_has_no_upi_id(customer, owner_a, ids):
    assert Database().shops.get(ids["shop_a"]).upi_id is None
    r = confirmed_reservation(customer, owner_a, ids)
    res = owner_a.post(f"/api/owner/reservations/{r['id']}/complete",
                       json={"code": r["code"], "payment_method": "upi_direct"}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["error"]["code"] == "upi_not_set_up"
    assert Database().reservations.get(r["id"]).status.value == "CONFIRMED"
    owner_a.post(f"/api/owner/reservations/{r['id']}/complete", json={"code": r["code"], "payment_method": "cod"},
                 headers=HEADERS)

    o = out_for_delivery(customer, owner_a, ids)
    url = f"/api/owner/orders/{o['id']}/deliver"
    res = owner_a.post(url, json={"code": delivery_code(o["id"]), "payment_method": "upi_direct"}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["error"]["code"] == "upi_not_set_up"
    saved = Database().orders.get(o["id"])
    assert saved.status.value == "OUT_FOR_DELIVERY" and saved.payment_status == "unpaid"
    owner_a.post(url, json={"code": delivery_code(o["id"]), "payment_method": "cod"}, headers=HEADERS)


def test_cash_marks_the_reservation_and_the_order_paid_by_cash(customer, owner_a, ids):
    r = confirmed_reservation(customer, owner_a, ids)
    assert Database().reservations.get(r["id"]).payment_status is None  # set only at completion
    done = owner_a.post(f"/api/owner/reservations/{r['id']}/complete",
                        json={"code": r["code"], "payment_method": "cod"}, headers=HEADERS).json()
    assert (done["payment_method"], done["payment_status"]) == ("cod", "paid")
    saved = Database().reservations.get(r["id"])
    assert (saved.payment_method, saved.payment_status) == ("cod", "paid")
    mine = customer.get(f"/api/reservations/{r['id']}", headers=HEADERS).json()
    assert (mine["payment_method"], mine["payment_status"]) == ("cod", "paid")

    o = out_for_delivery(customer, owner_a, ids)
    assert Database().orders.get(o["id"]).payment_status == "unpaid"
    done = owner_a.post(f"/api/owner/orders/{o['id']}/deliver",
                        json={"code": delivery_code(o["id"]), "payment_method": "cod"}, headers=HEADERS).json()
    assert (done["status"], done["payment_method"], done["payment_status"]) == ("DELIVERED", "cod", "paid")
    saved = Database().orders.get(o["id"])
    assert (saved.payment_method, saved.payment_status) == ("cod", "paid")


def test_upi_marks_the_reservation_and_the_order_paid_by_upi(customer, owner_a, ids, shop_upi):
    r = confirmed_reservation(customer, owner_a, ids)
    done = owner_a.post(f"/api/owner/reservations/{r['id']}/complete",
                        json={"code": r["code"], "payment_method": "upi_direct"}, headers=HEADERS).json()
    assert (done["status"], done["payment_method"], done["payment_status"]) == ("COMPLETED", "upi_direct", "paid")
    saved = Database().reservations.get(r["id"])
    assert (saved.payment_method, saved.payment_status) == ("upi_direct", "paid")

    o = out_for_delivery(customer, owner_a, ids)
    done = owner_a.post(f"/api/owner/orders/{o['id']}/deliver",
                        json={"code": delivery_code(o["id"]), "payment_method": "upi_direct"}, headers=HEADERS).json()
    assert (done["status"], done["payment_method"], done["payment_status"]) == ("DELIVERED", "upi_direct", "paid")
    saved = Database().orders.get(o["id"])
    assert (saved.payment_method, saved.payment_status) == ("upi_direct", "paid")
    mine = customer.get(f"/api/orders/{o['id']}", headers=HEADERS).json()
    assert (mine["payment_method"], mine["payment_status"]) == ("upi_direct", "paid")


def test_other_actions_ignore_a_payment_method(customer, owner_a, ids):
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    res = owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={"payment_method": "cod"}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["payment_status"] is None
    customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)


# ---------------------------------------------------------------- who sees the UPI ID

def test_upi_id_is_in_the_customers_own_orders_and_reservations_only(customer, owner_a, ids, shop_upi):
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    body = {"items": [{"product_id": ids["tape"], "quantity": 1}], "lat": CENTER[0], "lng": CENTER[1],
            "address": "12 Temple Street, Agrahara", "phone": "9876543210"}
    o = customer.post("/api/orders", json=body, headers=HEADERS).json()
    try:
        own = [r, o, customer.get(f"/api/reservations/{r['id']}", headers=HEADERS).json(),
               customer.get(f"/api/orders/{o['id']}", headers=HEADERS).json(),
               next(x for x in customer.get("/api/reservations", headers=HEADERS).json() if x["id"] == r["id"]),
               next(x for x in customer.get("/api/orders", headers=HEADERS).json() if x["id"] == o["id"])]
        for view in own:
            assert view["shop"]["upi_id"] == UPI and view["shop"]["upi_payee_name"] == "Shop A Mobiles"

        # Without a payee name, the link uses the shop's name.
        Database().shops.update_one({"_id": ids["shop_a"]}, {"$set": {"upi_payee_name": None}})
        assert customer.get(f"/api/orders/{o['id']}", headers=HEADERS).json()["shop"]["upi_payee_name"] == "Shop A"

        # No public shop response carries it: shop pages, lists, products, recommendations, search and the sitemap.
        lat, lng = CENTER
        with TestClient(app) as public:
            responses = [(url, public.get(url, headers=HEADERS)) for url in ("/api/shops/shop-a", f"/api/shops?lat={lat}&lng={lng}", "/api/shops/shop-a/products",
                    "/api/shops/shop-a/reviews", f"/api/products/{ids['tape']}?lat={lat}&lng={lng}",
                    f"/api/products/{ids['tape']}/recommendations?lat={lat}&lng={lng}",
                    f"/api/search?q=teflon+tape&lat={lat}&lng={lng}", f"/api/search/discover?lat={lat}&lng={lng}",
                    "/api/search/suggest?q=tef", "/api/sitemap", "/api/meta")]
        for url, res in responses:
            assert res.status_code == 200, (url, res.text)
            assert UPI not in res.text and "upi_id" not in res.text and "upi_payee_name" not in res.text, url
        # Nor the owner's view of the customer's order and reservation (the owner has it in settings), nor another
        # customer, who cannot see the order at all.
        assert UPI not in owner_a.get(f"/api/owner/orders/{o['id']}", headers=HEADERS).text
        assert UPI not in owner_a.get(f"/api/owner/reservations/{r['id']}", headers=HEADERS).text
        with TestClient(app) as c:
            other = login(c, "other@example.com")
            for url in (f"/api/orders/{o['id']}", f"/api/reservations/{r['id']}"):
                assert other.get(url, headers=HEADERS).status_code == 404
            assert UPI not in other.get("/api/orders", headers=HEADERS).text
    finally:
        customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)
        customer.post(f"/api/orders/{o['id']}/cancel", json={}, headers=HEADERS)


def test_without_a_upi_id_the_customers_views_carry_none(customer, ids):
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    try:
        assert r["shop"]["upi_id"] is None and r["shop"]["upi_payee_name"] is None
    finally:
        customer.post(f"/api/reservations/{r['id']}/cancel", json={}, headers=HEADERS)
