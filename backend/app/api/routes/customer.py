"""Customer: reservations, delivery orders, reviews, dashboard, notifications."""
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.ai.recommend import bundle_for
from app.core.database import Database, utcnow
from app.core.deps import DB, CurrentUser, CustomerUser
from app.core.errors import Conflict, NotFound
from app.models import Order, OrderStatus, Reservation, ReservationStatus, Review, Shop, User
from app.schemas.requests import NotificationsReadIn, OrderIn, OrderQuoteIn, ReservationIn, ReviewIn, TransitionIn
from app.schemas.serializers import notification_out, order_out, public_listing, reservation_out, shop_brief
from app.services import boost, search_events
from app.services import orders as order_service
from app.services import reservations as reservation_service
from app.services import reviews as review_service
from app.services.loaders import hydrate_orders, hydrate_reservations, timelines, with_shops
from app.services.search import recent_searches
from app.utils.geo import haversine_km

router = APIRouter(tags=["customer"])

RES_ACTIVE = list(reservation_service.ACTIVE)
ORDER_ACTIVE = list(order_service.ACTIVE)


def _reviewed_ids(db: Database, customer_id: int) -> tuple[set[int], set[int]]:
    rows = db.reviews.find_raw({"customer_id": customer_id}, {"reservation_id": 1, "order_id": 1})
    return ({r["reservation_id"] for r in rows if r.get("reservation_id")},
            {r["order_id"] for r in rows if r.get("order_id")})


def _pay_to(out: dict, shop: Shop) -> dict:
    """Add the shop's own UPI ID and payee name (P6.1, plan section 8.8) to a customer's own order or reservation,
    for the payment link and QR code (P6.2): the customer pays the shop directly. Both None while the shop has no
    UPI ID. Added here, and never by the serializers, so public shop responses never carry them."""
    out["shop"] = {**out["shop"], "upi_id": shop.upi_id,
                   "upi_payee_name": (shop.upi_payee_name or shop.name) if shop.upi_id else None}
    return out


def _res_json(db: Database, rows: list[Reservation], reviewed: set[int], with_events: bool = False) -> list[dict]:
    hydrate_reservations(db, rows)
    events = timelines(db, "reservation", [r.id for r in rows]) if with_events else {}
    return [_pay_to({**reservation_out(r, actions=reservation_service.allowed_actions(r, "customer"),
                                       events=events.get(r.id) if with_events else None, reviewed=r.id in reviewed),
                     "payment_method": r.payment_method, "payment_status": r.payment_status}, r.shop)
            for r in rows]


def _order_json(db: Database, rows: list[Order], reviewed: set[int], with_events: bool = False) -> list[dict]:
    hydrate_orders(db, rows)
    events = timelines(db, "order", [o.id for o in rows]) if with_events else {}
    # The delivery code (P3.5) is the customer's alone: added here, to their own orders, and never by order_out,
    # which the owner's responses use too.
    return [_pay_to({**order_out(o, actions=order_service.allowed_actions(o, "customer"),
                                 events=events.get(o.id) if with_events else None, reviewed=o.id in reviewed),
                     "delivery_code": o.delivery_code if o.status in order_service.ACTIVE else None}, o.shop)
            for o in rows]


def _scope(field_active: list, scope: str) -> dict:
    if scope == "active":
        return {"status": {"$in": field_active}}
    if scope == "past":
        return {"status": {"$nin": field_active}}
    return {}


# ------------------------------------------------------------------ reservations

@router.post("/reservations")
def create_reservation(body: ReservationIn, request: Request, user: CustomerUser, db: DB):
    r = reservation_service.create_reservation(db, user, body.product_id, body.quantity, body.note)
    # Search attribution (P5.6): off the request path, only with the search-measurement consent; never fails this.
    search_events.attribute(db, request, user, "reservation", [r.product_id])
    boost.attribute(db, request, [r.product_id])  # Boost (P10.12): same browser session and IST day; never fails this
    return _res_json(db, [r], set(), with_events=True)[0]


@router.get("/reservations")
def my_reservations(user: CustomerUser, db: DB, scope: Literal["active", "past", "all"] = "all"):
    reservation_service.expire_due(db)
    rows, (reviewed, _) = db.gather(
        lambda: db.reservations.find({"customer_id": user.id, **_scope(RES_ACTIVE, scope)},
                                     sort=[("created_at", -1)], limit=100),
        lambda: _reviewed_ids(db, user.id),
    )
    return _res_json(db, rows, reviewed)


def _own_reservation(db: Database, user: User, reservation_id: int) -> Reservation:
    r = db.reservations.get(reservation_id)
    if r is None or r.customer_id != user.id:
        raise NotFound("Reservation not found")
    return r


@router.get("/reservations/{reservation_id}")
def reservation_detail(reservation_id: int, user: CustomerUser, db: DB):
    reservation_service.expire_due(db)
    r = _own_reservation(db, user, reservation_id)
    reviewed, _ = _reviewed_ids(db, user.id)
    return _res_json(db, [r], reviewed, with_events=True)[0]


@router.post("/reservations/{reservation_id}/cancel")
def cancel_reservation(reservation_id: int, body: TransitionIn, user: CustomerUser, db: DB):
    r = _own_reservation(db, user, reservation_id)
    r = reservation_service.transition(db, r.id, "cancel", user, body.reason)
    return _res_json(db, [r], set(), with_events=True)[0]


# ------------------------------------------------------------------ delivery orders

@router.post("/orders/quote")
def quote(body: OrderQuoteIn, db: DB):
    found = db.products.by_ids(i.product_id for i in body.items)
    if any(i.product_id not in found for i in body.items):
        raise NotFound("One of the items is no longer listed")
    subtotal = sum(found[i.product_id].price * i.quantity for i in body.items)
    shop = db.shops.get(found[body.items[0].product_id].shop_id)
    return {**order_service.quote_delivery(shop, body.lat, body.lng, subtotal), "subtotal": round(subtotal, 2)}


@router.post("/orders")
def create_order(body: OrderIn, request: Request, user: CustomerUser, db: DB):
    o = order_service.create_order(db, user, [(i.product_id, i.quantity) for i in body.items], body.address,
                                   body.lat, body.lng, body.phone, body.note)
    search_events.attribute(db, request, user, "order", [i.product_id for i in body.items])  # as reservations
    boost.attribute(db, request, [i.product_id for i in body.items])  # Boost (P10.12), as reservations
    return _order_json(db, [o], set(), with_events=True)[0]


@router.get("/orders")
def my_orders(user: CustomerUser, db: DB, scope: Literal["active", "past", "all"] = "all"):
    order_service.cancel_stale_pending(db)  # lazily, as reservations expire: right even between sweeps
    rows, (_, reviewed) = db.gather(
        lambda: db.orders.find({"customer_id": user.id, **_scope(ORDER_ACTIVE, scope)},
                               sort=[("created_at", -1)], limit=100),
        lambda: _reviewed_ids(db, user.id),
    )
    return _order_json(db, rows, reviewed)


def _own_order(db: Database, user: User, order_id: int) -> Order:
    o = db.orders.get(order_id)
    if o is None or o.customer_id != user.id:
        raise NotFound("Order not found")
    return o


@router.get("/orders/{order_id}")
def order_detail(order_id: int, user: CustomerUser, db: DB):
    order_service.cancel_stale_pending(db)
    o = _own_order(db, user, order_id)
    _, reviewed = _reviewed_ids(db, user.id)
    return _order_json(db, [o], reviewed, with_events=True)[0]


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, body: TransitionIn, user: CustomerUser, db: DB):
    o = _own_order(db, user, order_id)
    o = order_service.transition(db, o.id, "cancel", user, body.reason)
    return _order_json(db, [o], set(), with_events=True)[0]


# ------------------------------------------------------------------ reviews

@router.post("/reviews")
def create_review(body: ReviewIn, user: CustomerUser, db: DB):
    """Review a completed pickup or a delivered order, if the review rules allow it (P3.5, plan section 8.2:
    app/services/reviews.py). The shop's rating is then recomputed from its counted reviews in the same
    transaction."""
    if body.reservation_id:
        r = _own_reservation(db, user, body.reservation_id)
        if r.status != ReservationStatus.COMPLETED:
            raise Conflict("You can review a pickup once it is completed")
        shop_id, product_id = r.shop_id, r.product_id
        purchased_at, completed_at = r.created_at, r.completed_at or r.updated_at
        delivery_rating = None
    else:
        o = _own_order(db, user, body.order_id)
        if o.status != OrderStatus.DELIVERED:
            raise Conflict("You can review a delivery once it has arrived")
        shop_id, product_id = o.shop_id, o.items[0].product_id if o.items else None
        purchased_at, completed_at = o.created_at, o.delivered_at or o.updated_at
        delivery_rating = body.delivery_rating
    review_service.check_eligibility(db, user, db.shops.get(shop_id), purchased_at=purchased_at,
                                     completed_at=completed_at)
    review = Review(customer_id=user.id, shop_id=shop_id, product_id=product_id, reservation_id=body.reservation_id,
                    order_id=body.order_id, rating=body.rating, accuracy_rating=body.accuracy_rating,
                    delivery_rating=delivery_rating, comment=(body.comment or "").strip() or None)

    def save() -> None:
        # The review and the shop's recomputed rating change together; two reviews of one shop at once conflict
        # on the shop document, so the retried one recomputes with both.
        db.reviews.insert(review)
        review_service.recompute(db, shop_id)

    try:
        db.transaction(save)
    except DuplicateKeyError:  # unique index: one review per reservation / order
        raise Conflict("You have already reviewed this purchase", code="duplicate_review") from None
    return {"id": review.id, "ok": True}


class ReviewReportIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    reason: review_service.ReportReason
    note: str | None = Field(default=None, max_length=500)


def _report(db: Database, user: User, review_id: int, target: str, body: ReviewReportIn) -> dict:
    try:
        return review_service.report(db, user, review_id, target, body.reason, body.note)
    except DuplicateKeyError:  # the same user's report raced itself: it counts once
        return {"ok": True, "already_reported": True}


@router.post("/reviews/{review_id}/report")
def report_review(review_id: int, body: ReviewReportIn, user: CurrentUser, db: DB):
    """Report a review (P3.6, plan section 8.2): once per user, at most 10 reports a day (app/core/ratelimit.py).
    Reports from 3 distinct users hide it until an admin decides it."""
    return _report(db, user, review_id, review_service.REVIEW, body)


@router.post("/reviews/{review_id}/reply/report")
def report_reply(review_id: int, body: ReviewReportIn, user: CurrentUser, db: DB):
    """Report the shop's reply to a review, under the same rules as reporting a review."""
    return _report(db, user, review_id, review_service.REPLY, body)


# ------------------------------------------------------------------ dashboard

@router.get("/me/dashboard")
def dashboard(user: CustomerUser, db: DB):
    reservation_service.expire_due(db)
    mine = {"customer_id": user.id}
    (reviewed_res, reviewed_orders), active_res, active_orders, past_res, past_orders, completed, delivered, \
        shops_visited, recent, recommendations = db.gather(
            lambda: _reviewed_ids(db, user.id),
            lambda: db.reservations.find({**mine, "status": {"$in": RES_ACTIVE}}, sort=[("expires_at", 1)]),
            lambda: db.orders.find({**mine, "status": {"$in": ORDER_ACTIVE}}, sort=[("created_at", -1)]),
            lambda: db.reservations.find({**mine, "status": {"$nin": RES_ACTIVE}}, sort=[("updated_at", -1)], limit=6),
            lambda: db.orders.find({**mine, "status": {"$nin": ORDER_ACTIVE}}, sort=[("updated_at", -1)], limit=6),
            lambda: db.reservations.count({**mine, "status": ReservationStatus.COMPLETED}),
            lambda: db.orders.count({**mine, "status": OrderStatus.DELIVERED}),
            lambda: len(db.sales_history.distinct("shop_id", mine)),
            lambda: recent_searches(db, user.id),
            lambda: _recommendations_for(db, user),
        )
    # Resolve product/shop references for all four lists in one batch each.
    res_json = _res_json(db, active_res + past_res, reviewed_res)
    order_json = _order_json(db, active_orders + past_orders, reviewed_orders)
    history = sorted(
        res_json[len(active_res):] + order_json[len(active_orders):],
        key=lambda x: x["closed_at"] or x.get("completed_at") or x.get("delivered_at") or x["created_at"],
        reverse=True,
    )[:8]
    to_review = [h for h in history if not h["reviewed"] and h["status"] in {"COMPLETED", "DELIVERED"}]

    return {
        "active_reservations": res_json[:len(active_res)],
        "active_orders": order_json[:len(active_orders)],
        "history": history,
        "to_review": to_review[:3],
        "recent_searches": recent,
        "recommendations": recommendations,
        "stats": {"completed_pickups": completed, "delivered_orders": delivered, "shops_visited": shops_visited},
    }


def _recommendations_for(db: Database, user: User) -> list[dict]:
    """Apriori bundles for things this customer bought recently, available near their last shop."""
    bought = db.sales_history.find_raw({"customer_id": user.id, "catalog_item_id": {"$ne": None}},
                                       {"catalog_item_id": 1, "shop_id": 1}, sort=[("sold_at", -1)], limit=10)
    if not bought:
        return []
    owned = {b["catalog_item_id"] for b in bought}
    if user.home_lat and user.home_lng:
        lat, lng = user.home_lat, user.home_lng
    else:
        last_shop = db.shops.get(bought[0]["shop_id"])
        lat, lng = last_shop.lat, last_shop.lng

    # Candidate items (strongest rule first), then one query for every in-stock offer of them.
    candidates: dict[int, dict] = {}
    for item_id in dict.fromkeys(b["catalog_item_id"] for b in bought):
        for rec in bundle_for(db, item_id, limit=3):
            if rec["catalog_item_id"] not in owned:
                candidates.setdefault(rec["catalog_item_id"], rec)
    if not candidates:
        return []
    offers: dict[int, list] = {}
    for o in with_shops(db, db.products.find({"catalog_item_id": {"$in": list(candidates)}, "is_active": True,
                                              "quantity": {"$gt": 0}}), active_only=True):
        offers.setdefault(o.catalog_item_id, []).append(o)

    picks: list[dict] = []
    for cid, rec in candidates.items():
        if cid not in offers:
            continue
        best = min(offers[cid], key=lambda o: haversine_km(lat, lng, o.shop.lat, o.shop.lng))
        picks.append({"confidence": rec["confidence"], "lift": rec["lift"],
                      "offer": public_listing(best, shop_brief(best.shop, haversine_km(lat, lng, best.shop.lat,
                                                                                 best.shop.lng)))})
        if len(picks) >= 4:
            break
    return picks


# ------------------------------------------------------------------ notifications (all roles)

@router.get("/notifications")
def notifications(user: CurrentUser, db: DB, limit: int = Query(30, ge=1, le=100)):
    rows, unread = db.gather(
        lambda: db.notifications.find({"user_id": user.id}, sort=[("created_at", -1), ("_id", -1)], limit=limit),
        lambda: db.notifications.count({"user_id": user.id, "read_at": None}),
    )
    return {"items": [notification_out(n) for n in rows], "unread": unread}


@router.get("/notifications/unread-count")
def unread_count(user: CurrentUser, db: DB):
    # Also the heartbeat that keeps reservation timers honest while someone has the app open.
    reservation_service.expire_due(db)
    return {"unread": db.notifications.count({"user_id": user.id, "read_at": None})}


@router.post("/notifications/read")
def mark_read(body: NotificationsReadIn, user: CurrentUser, db: DB):
    query: dict = {"user_id": user.id, "read_at": None}
    if body.ids:
        query["_id"] = {"$in": body.ids}
    db.notifications.update_many(query, {"$set": {"read_at": utcnow()}})
    return {"ok": True}
