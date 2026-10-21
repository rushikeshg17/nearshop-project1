"""Review reports, moderation and shop replies (P3.6, plan section 8.2): reports from 3 distinct users hide a
review, the report limit, the admin queue's keep, hide and remove, the monthly SLA, the shop's one reply and what
the public review lists show. Each test builds its own verified shop, owner and customers."""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import ratelimit
from app.core.config import settings
from app.core.database import Database, utcnow
from app.main import app
from app.models import Product, Role, Shop, User
from app.models.activity import Review, ReviewReply
from app.models.core import VerificationStatus, months_after
from app.services import privacy
from app.services import reviews as review_service
from tests.conftest import CENTER, HEADERS
from tests.test_reviews import PW_HASH, client_for, completed, make_customer, regular

REASON = {"reason": "fake"}


@pytest.fixture
def world(db, ids, monkeypatch):
    """A verified shop with its own owner and one listing; everything it made is removed afterwards."""
    monkeypatch.setattr(settings, "signups_open", True)
    tag = uuid.uuid4().hex[:8]
    owner = db.users.insert(User(name="Mod Owner", email=f"mod-owner-{tag}@example.com", password_hash=PW_HASH,
                                 role=Role.OWNER))
    now = utcnow()
    shop = db.shops.insert(Shop(
        owner_id=owner.id, name=f"Mod Shop {tag}", slug=f"mod-shop-{tag}", address_line="7 Mod Road", city="Mysuru",
        lat=CENTER[0] + 0.004, lng=CENTER[1], category_ids=[], verification_status=VerificationStatus.VERIFIED,
        is_verified=True, kyc_type="udyam", kyc_number="UDYAM-KR-23-0000098", phone_verified_at=now,
        submitted_at=now, decided_at=now, verified_until=months_after(now)))
    product = db.products.insert(Product(shop_id=shop.id, category_id=1, name="Moderation Test Lamp", price=100,
                                         quantity=500))
    data = {"tag": tag, "owner": owner, "shop": shop, "product": product, "other_shop": ids["shop_b"],
            "other_product": ids["tape_far"], "users": [owner.id]}
    yield data
    users = data["users"]
    review_ids = [r["_id"] for r in db.reviews.find_raw({"shop_id": shop.id}, {"_id": 1})]
    db.mongo["review_reports"].delete_many({"review_id": {"$in": review_ids}})
    for coll in ("reservations", "orders", "reviews", "status_events", "sales_history"):
        db.mongo[coll].delete_many({"customer_id": {"$in": users}})
        db.mongo[coll].delete_many({"shop_id": shop.id})
    db.products.delete_many({"shop_id": shop.id})
    db.shops.delete_many({"_id": shop.id})
    db.users.delete_many({"_id": {"$in": users}})


def add_review(db: Database, world: dict, customer: User, rating: int = 5, **fields) -> Review:
    """A review of a completed pickup at the world's shop, written directly, then the shop recomputed."""
    res = completed(db, customer, world["shop"].id, world["product"].id)
    r = db.reviews.insert(Review(customer_id=customer.id, shop_id=world["shop"].id, product_id=world["product"].id,
                                 reservation_id=res.id, rating=rating, comment=f"review by {customer.name}",
                                 **fields))
    review_service.recompute_shop(db.mongo, world["shop"].id)
    return r


def rated_shop(db: Database, world: dict, n: int = 5) -> list[Review]:
    """n reviews from regular customers (they shop elsewhere too), so the shop shows a rating from 5."""
    return [add_review(db, world, regular(db, world, f"r{i}")) for i in range(n)]


def reporters(db: Database, world: dict, n: int) -> list:
    return [client_for(make_customer(db, world, f"rep{i}").email) for i in range(n)]


def public_ids(world: dict, client) -> tuple[list[int], list[int]]:
    """The review ids on the shop's reviews list and on the product page."""
    shop = client.get(f"/api/shops/{world['shop'].slug}/reviews", headers=HEADERS).json()["reviews"]
    product = client.get(f"/api/products/{world['product'].id}", headers=HEADERS).json()["reviews"]
    return [r["id"] for r in shop], [r["id"] for r in product]


# ---------------------------------------------------------------- reports

def test_three_distinct_reporters_hide_a_review_two_do_not_and_repeats_count_once(db, world, client, admin):
    reviews = rated_shop(db, world)
    target = reviews[0]
    url = f"/api/reviews/{target.id}/report"
    first, second, third = reporters(db, world, 3)
    assert first.post(url, json={"reason": "fake", "note": "never bought"}, headers=HEADERS).json() == {
        "ok": True, "already_reported": False}
    again = first.post(url, json={"reason": "abusive"}, headers=HEADERS)
    assert again.status_code == 200 and again.json()["already_reported"] is True
    assert second.post(url, json={"reason": "off_topic"}, headers=HEADERS).status_code == 200
    r = db.reviews.get(target.id)
    assert (r.status, r.report_count) == ("visible", 2) and r.reported_at is not None
    assert db.shops.get(world["shop"].id).rating_avg == 5.0
    assert target.id in public_ids(world, client)[0]

    assert third.post(url, json={"reason": "personal_info"}, headers=HEADERS).status_code == 200
    r = db.reviews.get(target.id)
    assert (r.status, r.report_count) == ("hidden", 3) and r.decided_at is None
    shop = db.shops.get(world["shop"].id)
    assert (shop.rating_count, shop.rating_avg) == (4, None)  # recomputed: 4 customers, so "New shop"
    shop_list, product_page = public_ids(world, client)
    assert target.id not in shop_list and target.id not in product_page

    queue = admin.get("/api/admin/reviews/queue", headers=HEADERS).json()["items"]
    item = next(i for i in queue if i["review_id"] == target.id and i["target"] == "review")
    assert item["flags"] == ["reported", "hidden_by_reports"] and item["reports"]["count"] == 3
    assert item["reports"]["reasons"] == {"fake": 1, "off_topic": 1, "personal_info": 1}
    assert item["reports"]["notes"] == ["never bought"] and item["shop"]["id"] == world["shop"].id
    assert item["actions"] == ["keep", "hide", "remove"] and item["age_hours"] >= 0


def test_reports_are_validated_and_the_author_cannot_report_their_own_review(db, world):
    author = regular(db, world, "author")
    r = add_review(db, world, author)
    c = client_for(author.email)
    own = c.post(f"/api/reviews/{r.id}/report", json=REASON, headers=HEADERS)
    assert own.status_code == 409 and own.json()["error"]["code"] == "own_review"
    other = reporters(db, world, 1)[0]
    assert other.post(f"/api/reviews/{r.id}/report", json={"reason": "spam"}, headers=HEADERS).status_code == 422
    assert other.post("/api/reviews/999999999/report", json=REASON, headers=HEADERS).status_code == 404
    assert other.post(f"/api/reviews/{r.id}/reply/report", json=REASON, headers=HEADERS).status_code == 404
    assert TestClient(app).post(f"/api/reviews/{r.id}/report", json=REASON, headers=HEADERS).status_code == 401


def test_the_eleventh_report_in_a_day_gets_429(db, world, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_mode", "enforce")
    db.mongo[ratelimit.COLLECTION].delete_many({})
    try:
        reviews = rated_shop(db, world, 2)
        c = reporters(db, world, 1)[0]
        for n in range(10):
            res = c.post(f"/api/reviews/{reviews[n % 2].id}/report", json=REASON, headers=HEADERS)
            assert res.status_code == 200, (n, res.text)
        limited = c.post(f"/api/reviews/{reviews[0].id}/reply/report", json=REASON, headers=HEADERS)
        assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) > 0
    finally:
        db.mongo[ratelimit.COLLECTION].delete_many({})


# ---------------------------------------------------------------- moderation

@pytest.mark.parametrize("decision,status", [("keep", "visible"), ("hide", "hidden"), ("remove", "removed")])
def test_each_decision_sets_decided_at_and_recomputes_the_rating(db, world, admin, decision, status):
    reviews = rated_shop(db, world)
    target = reviews[-1]
    for c in reporters(db, world, 3):
        c.post(f"/api/reviews/{target.id}/report", json=REASON, headers=HEADERS)
    assert db.shops.get(world["shop"].id).rating_count == 4

    triaged = admin.post(f"/api/admin/reviews/{target.id}/triage", json={}, headers=HEADERS)
    assert triaged.status_code == 200 and triaged.json()["triaged_at"] and triaged.json()["decided_at"] is None
    res = admin.post(f"/api/admin/reviews/{target.id}/decision", json={"decision": decision, "note": "checked"},
                     headers=HEADERS)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == status and body["decision"] == decision and body["decided_at"]
    r = db.reviews.get(target.id)
    assert r.decided_at is not None and r.triaged_at <= r.decided_at and r.decision_note == "checked"
    shop = db.shops.get(world["shop"].id)
    assert shop.rating_count == (5 if decision == "keep" else 4)
    assert shop.rating_avg == (5.0 if decision == "keep" else None)
    queue = admin.get("/api/admin/reviews/queue", headers=HEADERS).json()["items"]
    assert all(i["review_id"] != target.id for i in queue)


def test_held_self_dealing_reviews_reach_the_queue_and_keep_releases_them(db, world, admin):
    loner = make_customer(db, world, "loner")  # bought only here: the self-dealing rule holds the review
    r = add_review(db, world, loner)
    assert db.reviews.get(r.id).held and db.shops.get(world["shop"].id).rating_count == 0
    queue = admin.get("/api/admin/reviews/queue", headers=HEADERS).json()["items"]
    item = next(i for i in queue if i["review_id"] == r.id)
    assert item["flags"] == ["held_self_dealing"] and item["reports"]["count"] == 0

    assert admin.post(f"/api/admin/reviews/{r.id}/decision", json={"decision": "keep"},
                      headers=HEADERS).status_code == 200
    assert not db.reviews.get(r.id).held and db.shops.get(world["shop"].id).rating_count == 1
    review_service.recompute_shop(db.mongo, world["shop"].id)  # a kept review is never held again
    assert not db.reviews.get(r.id).held and db.shops.get(world["shop"].id).rating_count == 1


def test_a_review_kept_after_reports_is_still_held_for_self_dealing_later(db, world, admin):
    rated_shop(db, world)  # five regular customers: one single-shop reviewer (1 in 6) is under the 30% share
    first = add_review(db, world, make_customer(db, world, "loner0"))
    assert not db.reviews.get(first.id).held
    reporters(db, world, 1)[0].post(f"/api/reviews/{first.id}/report", json=REASON, headers=HEADERS)
    assert admin.post(f"/api/admin/reviews/{first.id}/decision", json={"decision": "keep"},
                      headers=HEADERS).status_code == 200
    kept = db.reviews.get(first.id)
    assert kept.decision == "keep" and kept.decided_at is not None and not kept.kept_held

    for i in (1, 2):  # 3 of 8 recent reviewers bought only here: the rule fires for the shop
        add_review(db, world, make_customer(db, world, f"loner{i}"))
    held = db.reviews.get(first.id)
    assert held.held and held.held_at is not None, "keeping a reported review is no self-dealing decision"
    queue = admin.get("/api/admin/reviews/queue", headers=HEADERS).json()["items"]
    item = next(i for i in queue if i["review_id"] == first.id)
    assert "held_self_dealing" in item["flags"]
    assert item["entered_at"].startswith(held.held_at.isoformat()[:19])  # aged from the hold, not the report

    assert admin.post(f"/api/admin/reviews/{first.id}/decision", json={"decision": "keep"},
                      headers=HEADERS).status_code == 200
    review_service.recompute_shop(db.mongo, world["shop"].id)
    released = db.reviews.get(first.id)
    assert not released.held and released.kept_held and released.decided_at == kept.decided_at
    queue = admin.get("/api/admin/reviews/queue", headers=HEADERS).json()["items"]
    assert all(i["review_id"] != first.id for i in queue)


def _plans(plan: dict) -> list[str]:
    """Every stage name in an explain plan."""
    out = [plan.get("stage", "")]
    for key in ("inputStage", "inputStages", "queryPlan"):
        child = plan.get(key)
        for c in (child if isinstance(child, list) else [child] if child else []):
            out += _plans(c)
    return out


def test_the_queue_and_sla_queries_use_indexes(db, world):
    start, end = datetime(2025, 1, 1), datetime(2025, 2, 1)
    review_filter, reply_filter = review_service.sla_filters(start, end)
    for flt in (review_service.QUEUE_FILTER, review_service.REPLY_QUEUE_FILTER, review_filter, reply_filter):
        plan = db.mongo["reviews"].find(flt).explain()["queryPlanner"]["winningPlan"]
        stages = _plans(plan)
        assert "COLLSCAN" not in stages and "IXSCAN" in stages, (flt, stages)


def test_the_queue_and_its_actions_are_admin_only(db, world, client):
    r = rated_shop(db, world, 1)[0]
    customer = reporters(db, world, 1)[0]
    owner = client_for(world["owner"].email)
    calls = [("get", "/api/admin/reviews/queue", None), ("get", "/api/admin/reviews/sla", None),
             ("post", f"/api/admin/reviews/{r.id}/triage", {}),
             ("post", f"/api/admin/reviews/{r.id}/decision", {"decision": "remove"})]
    for method, url, body in calls:
        assert getattr(client, method)(url, headers=HEADERS, **({"json": body} if body is not None else {})
                                       ).status_code == 401
        for who in (customer, owner):
            res = getattr(who, method)(url, headers=HEADERS, **({"json": body} if body is not None else {}))
            assert res.status_code == 403, (url, res.text)
    assert db.reviews.get(r.id).status == "visible"


# ---------------------------------------------------------------- shop replies

def test_a_reply_is_allowed_once_editable_for_24_hours_then_refused(db, world):
    r = rated_shop(db, world, 1)[0]
    owner = client_for(world["owner"].email)
    url = f"/api/owner/reviews/{r.id}/reply"
    first = owner.post(url, json={"text": "  Thank you for visiting!  "}, headers=HEADERS)
    assert first.status_code == 200 and first.json()["text"] == "Thank you for visiting!"
    twice = owner.post(url, json={"text": "Again"}, headers=HEADERS)
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "reply_exists"
    edited = owner.put(url, json={"text": "Thanks, see you soon"}, headers=HEADERS)
    assert edited.status_code == 200 and edited.json()["edited_at"]
    assert db.reviews.get(r.id).reply.text == "Thanks, see you soon"
    assert owner.post(url, json={"text": ""}, headers=HEADERS).status_code == 422

    db.reviews.update_one({"_id": r.id}, {"$set": {"reply.created_at": utcnow() - timedelta(hours=24, minutes=1)}})
    late = owner.put(url, json={"text": "Too late"}, headers=HEADERS)
    assert late.status_code == 409 and late.json()["error"]["code"] == "reply_locked"
    assert db.reviews.get(r.id).reply.text == "Thanks, see you soon"

    other_owner = client_for("b@example.com")
    assert other_owner.post(url, json={"text": "Not my shop"}, headers=HEADERS).status_code == 404
    assert other_owner.put(url, json={"text": "Not my shop"}, headers=HEADERS).status_code == 404
    mine = owner.get("/api/owner/reviews", headers=HEADERS).json()
    row = next(x for x in mine if x["id"] == r.id)
    assert row["status"] == "visible" and row["reply"]["status"] == "visible"


def test_a_reply_is_reportable_like_a_review(db, world, client, admin):
    r = rated_shop(db, world, 1)[0]
    owner = client_for(world["owner"].email)
    assert owner.post(f"/api/owner/reviews/{r.id}/reply", json={"text": "Rude words"}, headers=HEADERS).status_code \
        == 200
    own = owner.post(f"/api/reviews/{r.id}/reply/report", json=REASON, headers=HEADERS)
    assert own.status_code == 409 and own.json()["error"]["code"] == "own_reply"
    for c in reporters(db, world, 3):
        assert c.post(f"/api/reviews/{r.id}/reply/report", json={"reason": "abusive"}, headers=HEADERS
                      ).status_code == 200
    stored = db.reviews.get(r.id)
    assert stored.status == "visible" and stored.reply.status == "hidden" and stored.reply.report_count == 3
    shop_list = client.get(f"/api/shops/{world['shop'].slug}/reviews", headers=HEADERS).json()["reviews"]
    assert next(x for x in shop_list if x["id"] == r.id)["reply"] is None
    locked = owner.put(f"/api/owner/reviews/{r.id}/reply", json={"text": "Sorry"}, headers=HEADERS)
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "reply_under_review"

    queue = admin.get("/api/admin/reviews/queue", headers=HEADERS).json()["items"]
    item = next(i for i in queue if i["review_id"] == r.id)
    assert item["target"] == "reply" and item["reply_text"] == "Rude words" and "hidden_by_reports" in item["flags"]
    kept = admin.post(f"/api/admin/reviews/{r.id}/decision", json={"target": "reply", "decision": "keep"},
                      headers=HEADERS)
    assert kept.status_code == 200 and kept.json()["status"] == "visible"
    assert db.reviews.get(r.id).reply.decided_at is not None
    shop_list = client.get(f"/api/shops/{world['shop'].slug}/reviews", headers=HEADERS).json()["reviews"]
    assert next(x for x in shop_list if x["id"] == r.id)["reply"]["text"] == "Rude words"


# ---------------------------------------------------------------- public lists

def test_public_lists_leave_out_hidden_and_removed_reviews_and_show_replies(db, world, client, admin):
    shown, hidden, removed = rated_shop(db, world, 3)
    owner = client_for(world["owner"].email)
    owner.post(f"/api/owner/reviews/{shown.id}/reply", json={"text": "Glad you liked it"}, headers=HEADERS)
    admin.post(f"/api/admin/reviews/{hidden.id}/decision", json={"decision": "hide"}, headers=HEADERS)
    admin.post(f"/api/admin/reviews/{removed.id}/decision", json={"decision": "remove"}, headers=HEADERS)

    shop_list = client.get(f"/api/shops/{world['shop'].slug}/reviews", headers=HEADERS).json()["reviews"]
    product = client.get(f"/api/products/{world['product'].id}", headers=HEADERS).json()["reviews"]
    for rows in (shop_list, product):
        assert [x["id"] for x in rows] == [shown.id]
        assert rows[0]["reply"]["text"] == "Glad you liked it" and "status" not in rows[0]
    mine = {x["id"]: x["status"] for x in owner.get("/api/owner/reviews", headers=HEADERS).json()}
    assert mine == {shown.id: "visible", hidden.id: "hidden", removed.id: "removed"}
    # A removed review cannot be reported or answered any more, and its reply cannot be edited.
    c = reporters(db, world, 1)[0]
    assert c.post(f"/api/reviews/{removed.id}/report", json=REASON, headers=HEADERS).status_code == 404
    assert owner.post(f"/api/owner/reviews/{removed.id}/reply", json={"text": "x"}, headers=HEADERS
                      ).status_code == 404
    owner.post(f"/api/owner/reviews/{hidden.id}/reply", json={"text": "Before removal"}, headers=HEADERS)
    admin.post(f"/api/admin/reviews/{hidden.id}/decision", json={"decision": "remove"}, headers=HEADERS)
    assert owner.put(f"/api/owner/reviews/{hidden.id}/reply", json={"text": "After removal"}, headers=HEADERS
                     ).status_code == 404
    assert db.reviews.get(hidden.id).reply.text == "Before removal"


def test_the_data_export_leaves_out_moderation_bookkeeping_and_deletion_clears_report_notes(db, world, admin):
    customer = regular(db, world, "exporter")
    r = add_review(db, world, customer)
    reporter = make_customer(db, world, "noter")
    review_service.report(db, reporter, r.id, "review", "abusive", "  internal-looking note  ")
    owner = client_for(world["owner"].email)
    owner.post(f"/api/owner/reviews/{r.id}/reply", json={"text": "Thanks"}, headers=HEADERS)
    admin.post(f"/api/admin/reviews/{r.id}/decision", json={"decision": "keep", "note": "admin only"},
               headers=HEADERS)

    exported = next(x for x in privacy.export_data(db, customer)["reviews"] if x["id"] == r.id)
    assert exported["status"] == "visible" and exported["reply"]["text"] == "Thanks"
    for field in ("decided_by", "decision_note", "report_count", "triaged_at", "kept_held"):
        assert field not in exported
    for field in ("author_id", "decided_by", "decision_note", "report_count"):
        assert field not in exported["reply"]

    filed = privacy.export_data(db, reporter)["review_reports"]
    assert [(x["review_id"], x["reason"], x["note"]) for x in filed] == [(r.id, "abusive", "internal-looking note")]
    assert "reporter_id" not in filed[0]
    privacy.anonymise(db, reporter.id)
    row = db.mongo["review_reports"].find_one({"reporter_id": reporter.id})
    assert row["note"] is None and row["reason"] == "abusive"
    assert db.reviews.get(r.id).report_count == 1


# ---------------------------------------------------------------- SLA

def test_sla_shares_on_synthetic_timestamps():
    now = datetime(2026, 3, 31)
    e = datetime(2026, 3, 1)
    h = timedelta(hours=1)
    items = [
        (e, e + 2 * h, e + 10 * h),  # both in time
        (e, e + 30 * h, e + 80 * h),  # both late
        (e, e + 5 * h, e + 73 * h),  # triaged in time, decided late
        (e, None, None),  # never acted on: missed both
        (now - 2 * h, None, None),  # still inside both deadlines: pending, in neither share
        (now - 30 * h, now - 2 * h, None),  # triaged late (after 28 hours); decision still pending
    ]
    out = review_service.sla_shares(items, now)
    assert out["items"] == 6 and out["open"] == 3
    assert (out["triage_counted"], out["triaged_within_24h"], out["triaged_share"]) == (5, 2, 0.4)
    assert (out["decision_counted"], out["decided_within_72h"], out["decided_share"]) == (4, 1, 0.25)
    empty = review_service.sla_shares([], now)
    assert empty["triaged_share"] is None and empty["decided_share"] is None


def test_the_monthly_sla_endpoint_counts_the_items_that_entered_the_queue_that_month(db, world, admin):
    h = timedelta(hours=1)
    jan = datetime(2025, 1, 10)

    def synthetic(name: str, **fields) -> None:
        c = regular(db, world, name)
        res = completed(db, c, world["shop"].id, world["product"].id)
        db.reviews.insert(Review(customer_id=c.id, shop_id=world["shop"].id, reservation_id=res.id, rating=4,
                                 created_at=jan - 24 * h, **fields))

    synthetic("a", reported_at=jan, triaged_at=jan + 2 * h, decided_at=jan + 10 * h, decision="keep")
    synthetic("b", reported_at=jan, triaged_at=jan + 30 * h, decided_at=jan + 80 * h, decision="hide",
              status="hidden")
    synthetic("c", held=True, held_at=jan, triaged_at=jan + 5 * h, decided_at=jan + 73 * h, decision="keep")
    synthetic("d", reply=ReviewReply(text="reply", author_id=world["owner"].id, created_at=jan - h,
                                     reported_at=jan, report_count=1))
    synthetic("e", reported_at=datetime(2024, 12, 20), triaged_at=datetime(2024, 12, 20) + h)  # December
    # Entered the queue in India's January: 1 Jan 2025 00:10 IST is still 31 Dec in UTC.
    synthetic("f", reported_at=datetime(2024, 12, 31, 18, 40), triaged_at=datetime(2024, 12, 31, 19, 0),
              decided_at=datetime(2025, 1, 1), decision="remove", status="removed")

    res = admin.get("/api/admin/reviews/sla", params={"month": "2025-01"}, headers=HEADERS)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["month"] == "2025-01" and out["items"] == 5 and out["open"] == 1
    assert (out["triaged_within_24h"], out["triaged_share"]) == (3, 0.6)
    assert (out["decided_within_72h"], out["decided_share"]) == (2, 0.4)
    assert admin.get("/api/admin/reviews/sla", params={"month": "2025-13"}, headers=HEADERS).status_code == 422
    assert admin.get("/api/admin/reviews/sla", headers=HEADERS).json()["month"] == (
        utcnow() + timedelta(hours=5, minutes=30)).strftime("%Y-%m")
