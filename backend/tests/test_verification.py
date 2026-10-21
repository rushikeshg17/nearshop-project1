"""Shop verification (P3.3, plan section 8.1): customers see and buy only from shops whose KYC an admin approved
within the last 12 months; owners submit KYC, admins decide, approvals expire and are renewed.

Each test gets its own third shop, Shop C (owner c-verify@example.com, one listing of the seeded tape near the
test centre), in whatever verification state it needs, and removes it afterwards, so the shared Shop A and
Shop B (both verified, conftest.py) are untouched.
"""
import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pymongo.errors import WriteError

from app.ai.semantic_index import index
from app.api.routes import catalog
from app.api.routes.admin import working_days_between
from app.core.database import Database, utcnow
from app.core.database import client as mongo_client
from app.core.security import hash_password
from app.main import app
from app.models import Product, Role, Shop, User
from app.models.core import (
    KycType,
    VerificationStatus,
    gstin_check_char,
    live_shop_filter,
    months_after,
    validate_kyc_number,
)
from app.services import media
from app.services.search import reset_vocabulary
from database.migrations import m0005_shop_verification
from tests.conftest import CENTER, HEADERS, PASSWORD, login

ME = {"lat": CENTER[0], "lng": CENTER[1]}
C_EMAIL = "c-verify@example.com"
C_SLUG = "verify-shop-c"
VALID_GSTIN = "27AAPFU0939F1ZV"


def png(color=(120, 90, 60), size=(400, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def verified_fields(now: datetime | None = None, **over) -> dict:
    """A verified shop decided a day after it submitted, approval in force for 12 months."""
    now = now or utcnow()
    return {"verification_status": "verified", "is_verified": True, "kyc_type": "udyam",
            "kyc_number": "UDYAM-KR-23-0000003", "phone_verified_at": now, "submitted_at": now - timedelta(days=1),
            "decided_at": now, "verified_until": months_after(now), **over}


def set_state(shop_id: int, **fields) -> None:
    Database().shops.update_one({"_id": shop_id}, {"$set": fields})


@pytest.fixture
def shop_c(db, ids):
    tape = db.products.get(ids["tape"])
    owner = db.users.insert(User(name="Owner C", email=C_EMAIL, password_hash=hash_password(PASSWORD),
                                 role=Role.OWNER))
    shop = db.shops.insert(Shop(
        owner_id=owner.id, name="Verify Shop C", slug=C_SLUG, address_line="3 Temple Street", locality="Agrahara",
        city="Mysuru", phone="9123456780", lat=CENTER[0] + 0.003, lng=CENTER[1], offers_delivery=True,
        delivery_radius_km=5, delivery_fee=20, category_ids=[tape.category_id]))
    product = db.products.insert(Product(
        shop_id=shop.id, catalog_item_id=tape.catalog_item_id, category_id=tape.category_id, name=tape.name,
        brand=tape.brand, keywords=tape.keywords, icon=tape.icon, price=35, mrp=tape.mrp, quantity=20,
        low_stock_threshold=2))
    index.mark_dirty()
    reset_vocabulary()
    catalog.reset_sitemap_cache()
    yield {"owner": owner.id, "shop": shop.id, "product": product.id}
    db.reservations.delete_many({"shop_id": shop.id})
    db.orders.delete_many({"shop_id": shop.id})
    db.products.delete_many({"shop_id": shop.id})
    db.shops.delete_many({"_id": shop.id})
    db.notifications.delete_many({"user_id": owner.id})
    db.mongo["admin_audit"].delete_many({"target_shop_id": shop.id})
    db.users.delete_many({"_id": owner.id})
    index.mark_dirty()
    reset_vocabulary()
    catalog.reset_sitemap_cache()


@pytest.fixture
def owner_c(shop_c):
    with TestClient(app) as c:
        yield login(c, C_EMAIL)


def shop(shop_c) -> Shop:
    return Database().shops.get(shop_c["shop"])


def visibility(client, ids, shop_c) -> dict[str, bool]:
    """Whether Shop C shows up in each customer read the decision lists (plan section 8.1)."""
    catalog.reset_sitemap_cache()
    pid = shop_c["product"]
    sitemap = client.get("/api/sitemap").json()
    search = client.get("/api/search", params={"q": "teflon tape", **ME, "radius_km": 5}).json()
    return {
        "search": any(o["id"] == pid for g in search["groups"] for o in g["offers"]),
        "map_and_shop_list": any(s["slug"] == C_SLUG for s in client.get("/api/shops", params=ME).json()["shops"]),
        "shop_page": client.get(f"/api/shops/{C_SLUG}").status_code == 200,
        "shop_products": client.get(f"/api/shops/{C_SLUG}/products").status_code == 200,
        "product_page": client.get(f"/api/products/{pid}").status_code == 200,
        "recommendations": client.get(f"/api/products/{pid}/recommendations").status_code == 200,
        "other_offers": any(o["shop"]["slug"] == C_SLUG for o in
                            client.get(f"/api/products/{ids['tape']}", params=ME).json()["other_offers"]),
        "sitemap_shop": any(s["slug"] == C_SLUG for s in sitemap["shops"]),
        "sitemap_listing": any(p["id"] == pid for p in sitemap["listings"]),
    }


# ---------------------------------------------------------------- visibility and blocking

def test_new_shop_starts_as_draft_and_hidden():
    s = Shop(owner_id=1, name="New", slug="new", address_line="1 Road", city="Mysuru", lat=CENTER[0], lng=CENTER[1])
    assert s.verification_status == VerificationStatus.DRAFT and not s.is_verified and not s.is_live


@pytest.mark.parametrize("state", ["draft", "submitted", "rejected", "suspended"])
def test_unverified_shop_is_in_no_customer_read(client, ids, shop_c, state):
    set_state(shop_c["shop"], verification_status=state, is_verified=False)
    seen = visibility(client, ids, shop_c)
    assert not any(seen.values()), seen


def test_verified_shop_appears_in_every_customer_read(client, ids, shop_c):
    set_state(shop_c["shop"], **verified_fields())
    seen = visibility(client, ids, shop_c)
    assert all(seen.values()), seen


def test_meta_counts_only_live_shops(client, db, shop_c):
    hidden = client.get("/api/meta").json()["stats"]["shops"]
    assert hidden == db.shops.count(live_shop_filter())
    set_state(shop_c["shop"], **verified_fields())
    assert client.get("/api/meta").json()["stats"]["shops"] == hidden + 1


def test_reservations_quotes_and_orders_for_an_unverified_shop_return_409(customer, shop_c):
    pid = shop_c["product"]
    order = {"items": [{"product_id": pid, "quantity": 1}], **ME, "address": "12 Temple Street, Agrahara",
             "phone": "9876543210"}
    for path, body in (("/api/reservations", {"product_id": pid, "quantity": 1}),
                       ("/api/orders/quote", {"items": order["items"], **ME}),
                       ("/api/orders", order)):
        res = customer.post(path, json=body, headers=HEADERS)
        assert res.status_code == 409, (path, res.text)
        assert res.json()["error"]["code"] == "shop_not_verified"


def test_a_verified_shop_takes_reservations_quotes_and_orders(customer, shop_c):
    set_state(shop_c["shop"], **verified_fields())
    pid = shop_c["product"]
    r = customer.post("/api/reservations", json={"product_id": pid, "quantity": 1}, headers=HEADERS)
    assert r.status_code == 200, r.text
    quote = customer.post("/api/orders/quote", json={"items": [{"product_id": pid, "quantity": 1}], **ME},
                          headers=HEADERS)
    assert quote.status_code == 200 and quote.json()["eligible"]
    o = customer.post("/api/orders", json={"items": [{"product_id": pid, "quantity": 1}], **ME,
                                           "address": "12 Temple Street, Agrahara", "phone": "9876543210"},
                      headers=HEADERS)
    assert o.status_code == 200, o.text


def test_owner_can_still_set_up_while_pending(owner_c, shop_c):
    assert owner_c.get("/api/owner/shop").json()["verification"]["status"] == "draft"
    res = owner_c.patch("/api/owner/shop", json={"tagline": "Pipes and fittings"}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["review_started"] is False
    assert owner_c.get("/api/owner/products").status_code == 200


# ---------------------------------------------------------------- KYC number validation

def test_gstin_with_a_valid_check_character_is_accepted():
    assert validate_kyc_number(KycType.GSTIN, " 27aapfu0939f1zv ") == VALID_GSTIN
    assert gstin_check_char(VALID_GSTIN[:14]) == "V"


def test_gstin_with_a_bad_check_character_is_refused():
    with pytest.raises(ValueError, match="last character"):
        validate_kyc_number(KycType.GSTIN, VALID_GSTIN[:14] + "A")
    with pytest.raises(ValueError, match="15 characters"):
        validate_kyc_number(KycType.GSTIN, "27AAPFU0939F1Z")


def test_the_gstin_format_example_shown_to_owners_is_itself_valid():
    with pytest.raises(ValueError) as err:
        validate_kyc_number(KycType.GSTIN, "nonsense")
    example = str(err.value).rsplit(" ", 1)[-1]
    assert validate_kyc_number(KycType.GSTIN, example) == example


@pytest.mark.parametrize("kyc_type", list(KycType))
@pytest.mark.parametrize("number", ["234567890123", "2345 6789 0123", "2345-6789-0123", "TL 234567890123"])
def test_an_aadhaar_like_number_is_refused_whatever_the_type(kyc_type, number):
    with pytest.raises(ValueError, match="Aadhaar"):
        validate_kyc_number(kyc_type, number)


def test_udyam_and_trade_licence_formats():
    assert validate_kyc_number("udyam", "udyam-kr-03-0012345") == "UDYAM-KR-03-0012345"
    for bad in ("UDYAM-KR-3-0012345", "UDYAM-KR-03-001234", "KR-03-0012345"):
        with pytest.raises(ValueError):
            validate_kyc_number("udyam", bad)
    assert validate_kyc_number("trade_licence", "MCC/TL/2024/00017") == "MCC/TL/2024/00017"
    for bad in ("AB1", "X" * 41, "TL#2024"):
        with pytest.raises(ValueError):
            validate_kyc_number("trade_licence", bad)


def test_submission_with_an_invalid_number_is_refused_and_stores_nothing(owner_c, shop_c):
    res = owner_c.post("/api/owner/verification", headers=HEADERS,
                       data={"kyc_type": "gstin", "kyc_number": VALID_GSTIN[:14] + "A"},
                       files={"storefront_photo": ("front.png", png(), "image/png")})
    assert res.status_code == 422 and res.json()["error"]["code"] == "invalid_kyc_number"
    s = shop(shop_c)
    assert s.verification_status == VerificationStatus.DRAFT and s.kyc_storefront_key is None


# ---------------------------------------------------------------- the full flow

def submit(owner, number=VALID_GSTIN, kyc_type="gstin", document=False, color=(120, 90, 60)):
    files = {"storefront_photo": ("front.png", png(color), "image/png")}
    if document:
        files["document_photo"] = ("licence.png", png((240, 240, 230)), "image/png")
    return owner.post("/api/owner/verification", headers=HEADERS, data={"kyc_type": kyc_type, "kyc_number": number},
                      files=files)


def audit_actions(db, shop_id) -> list[str]:
    return [r["action"] for r in db.mongo["admin_audit"].find({"target_shop_id": shop_id}).sort("_id", 1)]


def test_submit_phone_check_and_approve_makes_the_shop_visible(owner_c, admin, client, db, ids, shop_c):
    sid = shop_c["shop"]
    res = submit(owner_c)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "submitted" and res.json()["visible_to_customers"] is False
    s = shop(shop_c)
    assert s.kyc_storefront_key.startswith("kyc/storefronts/") and s.kyc_number == VALID_GSTIN
    assert media.read_private(s.kyc_storefront_key)  # stored privately, re-encoded
    assert not any(visibility(client, ids, shop_c).values())

    refused = admin.post(f"/api/admin/shops/{sid}/verification/approve", headers=HEADERS)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "phone_not_verified"

    assert admin.post(f"/api/admin/shops/{sid}/verification/phone-verified", headers=HEADERS).status_code == 200
    res = admin.post(f"/api/admin/shops/{sid}/verification/approve", headers=HEADERS)
    assert res.status_code == 200, res.text
    s = shop(shop_c)
    assert s.verification_status == VerificationStatus.VERIFIED and s.is_verified and s.decided_by is not None
    assert abs(s.verified_until - months_after(s.decided_at)) < timedelta(seconds=1)
    assert all(visibility(client, ids, shop_c).values())
    assert audit_actions(db, sid) == ["shop_phone_verified", "shop_verification_approve"]
    assert db.notifications.count({"user_id": shop_c["owner"], "kind": "shop_verified"}) == 1


def test_reject_needs_a_reason_and_hides_the_shop(owner_c, admin, db, shop_c):
    sid = shop_c["shop"]
    submit(owner_c)
    short = admin.post(f"/api/admin/shops/{sid}/verification/reject", json={"reason": "blurry"}, headers=HEADERS)
    assert short.status_code == 422
    res = admin.post(f"/api/admin/shops/{sid}/verification/reject",
                     json={"reason": "The signboard is not visible in the storefront photo."}, headers=HEADERS)
    assert res.status_code == 200, res.text
    s = shop(shop_c)
    assert s.verification_status == VerificationStatus.REJECTED and not s.is_live
    assert owner_c.get("/api/owner/verification").json()["rejection_reason"].startswith("The signboard")
    assert audit_actions(db, sid) == ["shop_verification_reject"]


def test_suspend_a_verified_shop(admin, client, ids, db, shop_c):
    sid = shop_c["shop"]
    set_state(sid, **verified_fields())
    res = admin.post(f"/api/admin/shops/{sid}/verification/suspend",
                     json={"reason": "Customers report the shop has closed."}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert shop(shop_c).verification_status == VerificationStatus.SUSPENDED
    assert not any(visibility(client, ids, shop_c).values())
    again = admin.post(f"/api/admin/shops/{sid}/verification/suspend",
                       json={"reason": "Customers report the shop has closed."}, headers=HEADERS)
    assert again.status_code == 409
    assert audit_actions(db, sid) == ["shop_verification_suspend"]


def test_the_old_is_verified_switch_follows_the_same_rules(admin, db, shop_c):
    sid = shop_c["shop"]
    res = admin.patch(f"/api/admin/shops/{sid}", json={"is_verified": True}, headers=HEADERS)
    assert res.status_code == 409 and not shop(shop_c).is_verified  # no evidence: nothing to approve
    set_state(sid, **verified_fields())
    res = admin.patch(f"/api/admin/shops/{sid}", json={"is_verified": False}, headers=HEADERS)
    assert res.status_code == 200, res.text
    s = shop(shop_c)
    assert s.verification_status == VerificationStatus.SUSPENDED and not s.is_verified
    assert audit_actions(db, sid) == ["shop_verification_suspend"]


# ---------------------------------------------------------------- queue, signed links, admin-only access

def test_queue_is_oldest_submission_first_with_working_day_ages(admin, db, ids, shop_c):
    now = utcnow()
    sid = shop_c["shop"]
    set_state(sid, verification_status="submitted", kyc_type="udyam", kyc_number="UDYAM-KR-23-0000003",
              submitted_at=now - timedelta(days=1))
    # Shop B, for the length of this test, is a renewal submitted earlier.
    b = db.shops.get(ids["shop_b"])
    before = {k: getattr(b, k) for k in ("submitted_at", "decided_at")}
    set_state(ids["shop_b"], submitted_at=now - timedelta(days=3), decided_at=now - timedelta(days=30))
    try:
        rows = admin.get("/api/admin/verification").json()
        order = [r["id"] for r in rows]
        assert order.index(ids["shop_b"]) < order.index(sid)
        assert [r["submitted_at"] for r in rows] == sorted(r["submitted_at"] for r in rows)
        by_id = {r["id"]: r for r in rows}
        assert by_id[ids["shop_b"]]["kind"] == "renewal" and by_id[sid]["kind"] == "new"
        assert by_id[sid]["age_working_days"] == working_days_between(now - timedelta(days=1), now)
        assert ids["shop_a"] not in by_id  # verified, nothing waiting
    finally:
        set_state(ids["shop_b"], **before)


def test_working_days_skip_weekends():
    friday = datetime(2026, 10, 16, 6, 0)  # 11:30 India time, a Friday
    assert working_days_between(friday, friday + timedelta(hours=3)) == 0
    assert working_days_between(friday, friday + timedelta(days=3)) == 1  # Monday
    assert working_days_between(friday, friday + timedelta(days=7)) == 5


def test_signed_links_open_documents_for_five_minutes_only(owner_c, admin, shop_c, monkeypatch):
    submit(owner_c, number="MCC/TL/2024/00017", kyc_type="trade_licence", document=True)
    row = next(r for r in admin.get("/api/admin/verification").json() if r["id"] == shop_c["shop"])
    for link in (row["document_photo_url"], row["storefront_photo_url"]):
        res = admin.get(link)
        assert res.status_code == 200 and res.headers["content-type"] == "image/webp"
        assert "no-store" in res.headers["cache-control"]
    link = row["storefront_photo_url"]
    tampered = link[:-4] + ("0000" if not link.endswith("0000") else "1111")
    assert admin.get(tampered).status_code == 403
    real_time = media.time.time
    monkeypatch.setattr(media.time, "time", lambda: real_time() + media.SIGNED_LINK_SECONDS + 1)
    expired = admin.get(link)
    assert expired.status_code == 403 and expired.json()["error"]["code"] == "link_expired"


def test_signed_link_check_is_bound_to_key_and_time():
    key = "kyc/storefronts/abc.webp"
    url = media.PrivateLocalStorage(media.settings.kyc_media_dir).signed_url(key, 300)
    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]
    assert media.check_signed_link(key, expires, signature, now=expires - 1)
    assert not media.check_signed_link(key, expires, signature, now=expires + 1)
    assert not media.check_signed_link("kyc/storefronts/other.webp", expires, signature, now=expires - 1)
    assert not media.check_signed_link("products/abc.webp", expires, signature, now=expires - 1)


ADMIN_CALLS = [
    ("get", "/api/admin/verification", None),
    ("post", "/api/admin/shops/{sid}/verification/phone-verified", None),
    ("post", "/api/admin/shops/{sid}/verification/approve", None),
    ("post", "/api/admin/shops/{sid}/verification/reject", {"reason": "Not a real shop at all."}),
    ("post", "/api/admin/shops/{sid}/verification/suspend", {"reason": "Not a real shop at all."}),
    ("get", "/api/admin/kyc-files/kyc/storefronts/x.webp?expires=9999999999&signature=x", None),
]


@pytest.mark.parametrize("method, path, body", ADMIN_CALLS)
def test_verification_endpoints_are_admin_only(client, customer, owner_c, shop_c, method, path, body):
    path = path.format(sid=shop_c["shop"])
    kwargs = {"headers": HEADERS, **({"json": body} if body else {})}
    for who in (customer, owner_c):
        assert getattr(who, method)(path, **kwargs).status_code == 403
    with TestClient(app) as anonymous:
        assert getattr(anonymous, method)(path, **kwargs).status_code == 401
    assert shop(shop_c).verification_status == VerificationStatus.DRAFT


# ---------------------------------------------------------------- expiry and renewal

def test_a_shop_whose_approval_has_ended_is_hidden(client, customer, ids, shop_c):
    past = utcnow() - timedelta(days=400)
    set_state(shop_c["shop"], **verified_fields(past))  # verified_until about a month ago
    assert not any(visibility(client, ids, shop_c).values())
    res = customer.post("/api/reservations", json={"product_id": shop_c["product"], "quantity": 1}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["error"]["code"] == "shop_not_verified"


def test_renewal_opens_30_days_before_the_approval_ends(owner_c, shop_c):
    set_state(shop_c["shop"], **verified_fields())
    early = submit(owner_c)
    assert early.status_code == 409 and early.json()["error"]["code"] == "renewal_not_open"
    assert owner_c.get("/api/owner/verification").json()["can_submit"] is False


def test_a_renewal_keeps_the_shop_visible_until_decided_and_approval_moves_12_months_on(
        owner_c, admin, client, ids, db, shop_c):
    sid = shop_c["shop"]
    until = utcnow() + timedelta(days=10)
    set_state(sid, **verified_fields(until - timedelta(days=365), verified_until=until))
    res = submit(owner_c)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "verified" and res.json()["renewal_pending"] is True
    assert all(visibility(client, ids, shop_c).values())
    row = next(r for r in admin.get("/api/admin/verification").json() if r["id"] == sid)
    assert row["kind"] == "renewal"

    # The approval ends while the renewal waits: the shop stays visible until an admin decides.
    set_state(sid, verified_until=utcnow() - timedelta(hours=1))
    assert all(visibility(client, ids, shop_c).values())

    res = admin.post(f"/api/admin/shops/{sid}/verification/approve", headers=HEADERS)
    assert res.status_code == 200, res.text
    s = shop(shop_c)
    assert not s.renewal_pending and s.previous_kyc is None
    assert abs(s.verified_until - months_after(s.decided_at)) < timedelta(seconds=1)
    assert all(visibility(client, ids, shop_c).values())


def test_renewal_approved_before_the_end_extends_from_the_end(owner_c, admin, shop_c):
    sid = shop_c["shop"]
    until = utcnow() + timedelta(days=10)
    set_state(sid, **verified_fields(until - timedelta(days=365), verified_until=until))
    submit(owner_c)
    assert admin.post(f"/api/admin/shops/{sid}/verification/approve", headers=HEADERS).status_code == 200
    assert abs(shop(shop_c).verified_until - months_after(until)) < timedelta(seconds=1)


def test_a_rejected_renewal_leaves_the_approval_and_its_evidence_in_force(owner_c, admin, client, ids, db, shop_c):
    sid = shop_c["shop"]
    # The first approval, with real evidence.
    assert submit(owner_c, color=(10, 10, 10)).status_code == 200
    admin.post(f"/api/admin/shops/{sid}/verification/phone-verified", headers=HEADERS)
    assert admin.post(f"/api/admin/shops/{sid}/verification/approve", headers=HEADERS).status_code == 200
    first = shop(shop_c)
    until = utcnow() + timedelta(days=5)
    set_state(sid, verified_until=until)

    assert submit(owner_c, number="UDYAM-KR-03-0012345", kyc_type="udyam", color=(250, 0, 0)).status_code == 200
    renewal = shop(shop_c)
    assert renewal.kyc_storefront_key != first.kyc_storefront_key
    assert media.read_private(first.kyc_storefront_key)  # kept while the renewal waits

    res = admin.post(f"/api/admin/shops/{sid}/verification/reject",
                     json={"reason": "The Udyam number does not match the shop name."}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "verified" and res.json()["visible_to_customers"] is True
    s = shop(shop_c)
    assert s.verification_status == VerificationStatus.VERIFIED and not s.renewal_pending
    assert abs(s.verified_until - until) < timedelta(seconds=1)
    assert (s.kyc_type, s.kyc_number, s.kyc_storefront_key) == (KycType.GSTIN, VALID_GSTIN, first.kyc_storefront_key)
    assert s.previous_kyc is None
    assert media.read_private(first.kyc_storefront_key) and media.read_private(renewal.kyc_storefront_key) is None
    assert all(visibility(client, ids, shop_c).values())
    out = owner_c.get("/api/owner/verification").json()
    assert out["rejection_reason"].startswith("The Udyam") and out["can_submit"] is True
    assert "shop_verification_reject_renewal" in audit_actions(db, sid)


# ---------------------------------------------------------------- address and phone changes

def test_an_address_change_sends_a_verified_shop_back_to_review(owner_c, client, ids, shop_c):
    set_state(shop_c["shop"], **verified_fields())
    before = owner_c.get("/api/owner/shop").json()["verification"]
    assert before["changes_need_review"] is True
    res = owner_c.patch("/api/owner/shop", json={"address_line": "44 New Market Road"}, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["review_started"] is True and res.json()["verification"]["status"] == "submitted"
    s = shop(shop_c)
    assert s.verification_status == VerificationStatus.SUBMITTED and s.phone_verified_at is not None
    assert not any(visibility(client, ids, shop_c).values())


def test_a_phone_change_sends_it_back_and_clears_the_phone_check(owner_c, admin, shop_c):
    sid = shop_c["shop"]
    set_state(sid, **verified_fields(), kyc_storefront_key="kyc/storefronts/seeded.webp")
    res = owner_c.patch("/api/owner/shop", json={"phone": "9000000001"}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["review_started"] is True
    s = shop(shop_c)
    assert s.verification_status == VerificationStatus.SUBMITTED and s.phone_verified_at is None
    refused = admin.post(f"/api/admin/shops/{sid}/verification/approve", headers=HEADERS)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "phone_not_verified"


def test_other_setting_changes_keep_a_verified_shop_visible(owner_c, shop_c):
    set_state(shop_c["shop"], **verified_fields())
    res = owner_c.patch("/api/owner/shop", json={"tagline": "Since 1998", "address_line": "3 Temple Street"},
                        headers=HEADERS)
    assert res.status_code == 200 and res.json()["review_started"] is False
    assert shop(shop_c).is_live


# ---------------------------------------------------------------- migration 5

def test_migration_backfills_existing_shops(scratch_db):
    scratch_db.shops.insert_many([{"_id": 1, "is_verified": True}, {"_id": 2, "is_verified": False}, {"_id": 3},
                                  {"_id": 4, "verification_status": "rejected", "is_verified": False}])
    m0005_shop_verification.apply(scratch_db)
    m0005_shop_verification.apply(scratch_db)  # idempotent
    rows = {d["_id"]: d for d in scratch_db.shops.find()}
    assert rows[1]["verification_status"] == "verified" and rows[1]["verified_until"] > utcnow()
    assert rows[2]["verification_status"] == "draft" and rows[3]["verification_status"] == "draft"
    assert rows[3]["is_verified"] is False and rows[4]["verification_status"] == "rejected"


@pytest.fixture
def scratch_db(ids):
    name = f"{Database().mongo.name}_v{utcnow():%H%M%S%f}"
    yield mongo_client[name]
    mongo_client.drop_database(name)


def test_admin_audit_accepts_the_shop_actions_and_still_refuses_junk(db, ids):
    audit = db.mongo["admin_audit"]
    row = {"admin_id": 1, "action": "shop_verification_approve", "target_shop_id": ids["shop_a"],
           "reason": "KYC evidence approved", "created_at": utcnow()}
    audit.insert_one({"_id": db.next_id("admin_audit"), **row})
    for bad in ({"action": "delete_everything"}, {"reason": "short"}, {"target_shop_id": None},
                {"action": "reveal_email"}):  # reveal_email needs target_user_id
        doc = {"_id": db.next_id("admin_audit"), **row, **bad}
        if doc.get("target_shop_id") is None:
            doc.pop("target_shop_id")
        with pytest.raises(WriteError):
            audit.insert_one(doc)
    audit.delete_many({"target_shop_id": ids["shop_a"]})


def test_kyc_storage_unavailable_on_s3_without_a_private_bucket():
    cfg = media.settings.model_copy(update={"media_backend": "s3", "kyc_s3_bucket": "", "media_s3_bucket": "pub"})
    assert media.build_private_storage(cfg) is None
    same = cfg.model_copy(update={"kyc_s3_bucket": "pub"})
    assert media.build_private_storage(same) is None  # never the public photo bucket
