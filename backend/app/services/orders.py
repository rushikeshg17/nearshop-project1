"""Shop-managed delivery order state machine.

    PENDING -> SHOP_CONFIRMED -> PREPARING -> OUT_FOR_DELIVERY -> DELIVERED
       |            |               |               |
       +------------+---cancel------+               +--fail--> DELIVERY_FAILED --return--> RETURNED_TO_SHOP

Stock is held at SHOP_CONFIRMED and released on cancel or return. The customer pays the shop at
the door, in cash or to the shop's own UPI ID; delivering records which (P6.1, plan section 8.8). NearShop
never holds customer money.

An order that includes a call-only listing (P9.8) is refused with 409 listing_call_only. An owner who cancels a
SHOP_CONFIRMED or PREPARING order with the reason out_of_stock_after_confirm (P9.8, plan section 8.17) records an arrival
failure instead of a release: the holds end without the units going back, and every listing in the order gets an
arrival_failure event with delta 0 and is unconfirmed until the shop next sets its stock (the reason does not say which
line was missing, so the shop's next count settles each one).
"""
import logging
import secrets
from datetime import timedelta

from app.core.config import settings
from app.core.database import Database, transactional, utcnow
from app.core.errors import AppError, Conflict, Forbidden, InvalidTransition, NotFound
from app.models import (
    InventoryReason,
    Order,
    OrderItem,
    Product,
    Role,
    SaleSource,
    Shop,
    User,
)
from app.models import (
    OrderStatus as OS,
)
from app.schemas.serializers import OUT_OF_STOCK_AFTER_CONFIRM
from app.services.fulfillment_common import record_event, record_sales
from app.services.inventory import change_stock
from app.services.notification_text import Term
from app.services.notifications import notify
from app.services.reservations import (
    NOT_ENOUGH_STOCK,
    RESERVED_REASONS,
    SHOP_NOT_VERIFIED,
    check_not_call_only,
    check_payment_method,
    check_public_cap,
    owner_out_of_stock,
    record_arrival_failure,
)
from app.utils.geo import haversine_km
from app.utils.text import short_code

log = logging.getLogger(__name__)

ACTIVE = {OS.PENDING, OS.SHOP_CONFIRMED, OS.PREPARING, OS.OUT_FOR_DELIVERY}
PENDING_TIMEOUT_MINUTES = 60

TRANSITIONS: dict[tuple[OS, str], tuple[OS, set[str]]] = {
    (OS.PENDING, "confirm"): (OS.SHOP_CONFIRMED, {"owner"}),
    (OS.PENDING, "cancel"): (OS.CANCELLED, {"customer", "owner", "system"}),
    (OS.SHOP_CONFIRMED, "prepare"): (OS.PREPARING, {"owner"}),
    (OS.SHOP_CONFIRMED, "cancel"): (OS.CANCELLED, {"customer", "owner"}),
    (OS.PREPARING, "dispatch"): (OS.OUT_FOR_DELIVERY, {"owner"}),
    (OS.PREPARING, "cancel"): (OS.CANCELLED, {"owner"}),
    (OS.OUT_FOR_DELIVERY, "deliver"): (OS.DELIVERED, {"owner"}),
    (OS.OUT_FOR_DELIVERY, "fail"): (OS.DELIVERY_FAILED, {"owner"}),
    (OS.DELIVERY_FAILED, "return"): (OS.RETURNED_TO_SHOP, {"owner"}),
}

# The customer's notification for each status the shop (or the system) moves an order to: a message key of
# app/services/notification_text.py, rendered in the customer's language (P7.11). A cancelled or failed order with a
# reason uses the key's _reason variant; the hour-long timeout has its own words (NO_RESPONSE_REASON).
CUSTOMER_MESSAGES = {
    OS.SHOP_CONFIRMED: "order_shop_confirmed",  # holds the delivery code: the customer's own notification
    OS.PREPARING: "order_preparing",
    OS.OUT_FOR_DELIVERY: "order_out_for_delivery",
    OS.DELIVERED: "order_delivered",
    OS.DELIVERY_FAILED: "order_delivery_failed",
    OS.CANCELLED: "order_cancelled",
}
NO_RESPONSE_REASON = "The shop did not respond within an hour"  # cancel_stale_pending's close reason


def allowed_actions(order: Order, actor: str) -> list[str]:
    return [a for (s, a), (_, actors) in TRANSITIONS.items() if s == order.status and actor in actors]


def quote_delivery(shop: Shop, lat: float, lng: float, subtotal: float) -> dict:
    """Delivery eligibility and fee. Refused (409) for a shop customers cannot see: paused, or not verified
    (P3.3), so POST /orders/quote blocks it like create_order does."""
    if not shop.is_active:
        raise Conflict("This shop is not taking orders right now")
    if not shop.is_live:
        raise Conflict(SHOP_NOT_VERIFIED, code="shop_not_verified")
    distance = haversine_km(shop.lat, shop.lng, lat, lng)
    eligible = shop.offers_delivery and distance <= shop.delivery_radius_km
    fee = shop.delivery_fee
    if shop.free_delivery_above is not None and subtotal >= shop.free_delivery_above:
        fee = 0.0
    reason = None
    if not shop.offers_delivery:
        reason = "This shop offers pickup only"
    elif not eligible:
        reason = f"You are {distance:.1f} km away; {shop.name} delivers within {shop.delivery_radius_km:.0f} km"
    return {
        "eligible": eligible,
        "distance_km": round(distance, 2),
        "delivery_fee": round(fee, 2) if eligible else None,
        "free_delivery_above": shop.free_delivery_above,
        "reason": reason,
    }


@transactional
def create_order(
    db: Database,
    customer: User,
    items: list[tuple[int, int]],
    address: str,
    lat: float,
    lng: float,
    phone: str,
    note: str | None,
) -> Order:
    if not items:
        raise Conflict("Add at least one item")
    found = db.products.by_ids(pid for pid, _ in items)
    products: list[tuple[Product, int]] = []
    for product_id, qty in items:
        p = found.get(product_id)
        if p is None or not p.is_active:
            raise NotFound("One of the items is no longer listed")
        if qty < 1 or qty > 50:
            raise Conflict("Quantity must be between 1 and 50")
        products.append((p, qty))
    shop_ids = {p.shop_id for p, _ in products}
    if len(shop_ids) != 1:
        raise Conflict("A delivery order can contain items from one shop only")
    shop = db.shops.get(products[0][0].shop_id)
    if not shop.is_active:
        raise Conflict("This shop is not taking orders right now")
    if not shop.is_live:  # not verified, or its approval has ended (P3.3, plan section 8.1)
        raise Conflict(SHOP_NOT_VERIFIED, code="shop_not_verified")
    for p, _ in products:
        check_not_call_only(p)
    # Public stock bands (P10.1): without the shop's exact-stock opt-in, a line asks for at most the public cap, and
    # a shortage is reported without a count. Lines for the same product are added up first, so splitting a quantity
    # across several lines neither gets past the cap nor hides a combined shortage.
    wanted: dict[int, tuple[Product, int]] = {}
    for p, qty in products:
        wanted[p.id] = (p, wanted[p.id][1] + qty if p.id in wanted else qty)
    for p, qty in wanted.values():
        check_public_cap(shop, qty)
        if p.quantity < qty:
            message = f"Only {p.quantity} of {p.name} in stock" if shop.show_exact_stock else NOT_ENOUGH_STOCK
            raise Conflict(message, code="insufficient_stock")

    subtotal = round(sum(p.price * q for p, q in products), 2)
    quote = quote_delivery(shop, lat, lng, subtotal)
    if not quote["eligible"]:
        raise Conflict(quote["reason"], code="delivery_unavailable")

    order = db.orders.insert(Order(
        code=short_code("D"),
        customer_id=customer.id,
        shop_id=shop.id,
        status=OS.PENDING,
        delivery_address=address.strip(),
        delivery_lat=lat,
        delivery_lng=lng,
        contact_phone=phone.strip(),
        distance_km=quote["distance_km"],
        subtotal=subtotal,
        delivery_fee=quote["delivery_fee"],
        total=round(subtotal + quote["delivery_fee"], 2),
        note=(note or "").strip() or None,
        items=[OrderItem(product_id=p.id, name=p.name, unit_price=p.price, quantity=q) for p, q in products],
    ))
    record_event(db, entity="order", entity_id=order.id, shop_id=shop.id, from_status=None,
                 to_status=OS.PENDING, actor_role="customer", actor_id=customer.id)
    summary = ", ".join(f"{q} x {p.name}" for p, q in products)
    notify(db, shop.owner_id, "order_new", link="/shop/orders", message="order_new",
           params={"code": order.code, "summary": summary, "address": address[:60],
                   "distance_km": quote["distance_km"], "total": order.total})
    return order


def _actor_for(db: Database, user: User | None, order: Order, shop_owner_id: int) -> str:
    if user is None:
        return "system"
    if user.role == Role.CUSTOMER and order.customer_id == user.id:
        return "customer"
    if user.role == Role.OWNER and shop_owner_id == user.id:
        return "owner"
    if user.role == Role.STAFF:
        from app.services import staff  # imported here: staff -> plans -> app.core.deps

        if staff.is_member(db, user.id, order.shop_id):
            return "staff"  # P9.13: acts as the shop, recorded as staff with the member's user id
    if user.role == Role.ADMIN:
        return "admin"
    raise Forbidden("This order belongs to someone else")


def transition(db: Database, order_id: int, action: str, user: User | None, reason: str | None = None,
               code: str | None = None, payment_method: str | None = None) -> Order:
    """Move an order through its state machine as one all-or-nothing transaction. Confirming creates the
    4-digit delivery code only the customer sees; delivering needs it (P3.5, plan section 8.2) and the payment
    method, cod or upi_direct (P6.1, reservations.check_payment_method). Other actions ignore payment_method."""
    if action in CODE_ACTIONS:
        ensure_delivery_code(db, order_id)
    try:
        return db.transaction(lambda: _transition(db, order_id, action, user, reason, code, payment_method))
    except Conflict as exc:
        if exc.code == "wrong_code" and (code or "").strip():
            _count_wrong_code(db, order_id)
        raise


# Orders waiting to be delivered, and the actions on them that make sure the order has a delivery code.
AWAITING_DELIVERY = [OS.SHOP_CONFIRMED.value, OS.PREPARING.value, OS.OUT_FOR_DELIVERY.value]
CODE_ACTIONS = {"prepare", "dispatch", "deliver"}
# Wrong delivery codes: after MAX_CODE_FAILURES in a row, the order takes no code for CODE_LOCK_MINUTES, so the
# 10,000 codes cannot be tried at the counter.
MAX_CODE_FAILURES = 5
CODE_LOCK_MINUTES = 30


def new_delivery_code() -> str:
    return f"{secrets.randbelow(10_000):04d}"


def ensure_delivery_code(db: Database, order_id: int) -> None:
    """Give a confirmed order its delivery code if it has none, and send the code to the customer. An order
    confirmed by the release before P3.5 (also while migration 6 ran) has none, and could never be delivered."""
    order = db.orders.find_one_and_update(
        {"_id": order_id, "status": {"$in": AWAITING_DELIVERY}, "delivery_code": {"$in": [None, ""]}},
        {"$set": {"delivery_code": new_delivery_code()}})
    if order is not None:
        shop = db.shops.get(order.shop_id)
        notify(db, order.customer_id, "order_delivery_code", link=f"/account/orders/{order.id}",
               message="order_delivery_code",
               params={"shop": shop.name if shop else Term("the_shop"), "delivery_code": order.delivery_code})


def _count_wrong_code(db: Database, order_id: int) -> None:
    """Count a wrong delivery code outside the (aborted) transaction; the MAX_CODE_FAILURES-th locks the order."""
    order = db.orders.find_one_and_update({"_id": order_id}, {"$inc": {"code_failures": 1}})
    if order is not None and order.code_failures >= MAX_CODE_FAILURES:
        db.orders.update_one({"_id": order_id}, {"$set": {
            "code_failures": 0, "code_locked_until": utcnow() + timedelta(minutes=CODE_LOCK_MINUTES)}})


def _transition(db: Database, order_id: int, action: str, user: User | None, reason: str | None,
                code: str | None = None, payment_method: str | None = None) -> Order:
    order = db.orders.get(order_id)  # loaded inside the transaction, see reservations._transition
    if order is None:
        raise NotFound("Order not found")
    shop = db.shops.get(order.shop_id)
    actor = _actor_for(db, user, order, shop.owner_id)
    key = (order.status, action)
    if key not in TRANSITIONS:
        raise InvalidTransition(f"Cannot {action} an order that is {order.status.replace('_', ' ').lower()}")
    to_status, actors = TRANSITIONS[key]
    if ("owner" if actor == "staff" else actor) not in actors and actor != "admin":
        raise Forbidden(f"Only the {' or '.join(sorted(actors))} can {action} this order")
    if action == "deliver" and settings.completion_codes_required:
        locked = order.code_locked_until
        if locked is not None and locked > utcnow():
            minutes = max(1, round((locked - utcnow()).total_seconds() / 60))
            raise AppError(f"Too many wrong delivery codes. Try again in {minutes} min, or call the customer.",
                           code="code_locked", status_code=429)
        entered = (code or "").strip()
        if not entered:
            raise Conflict("Enter the 4-digit delivery code the customer gives you", code="wrong_code")
        if not order.delivery_code or not secrets.compare_digest(entered.encode(), order.delivery_code.encode()):
            raise Conflict("That is not this order's delivery code", code="wrong_code")
    method = check_payment_method(shop, payment_method) if action == "deliver" else None

    now = utcnow()
    from_status = order.status
    actor_id = user.id if user else None
    changes: dict = {"status": to_status}
    products = db.products.by_ids(i.product_id for i in order.items) \
        if action in {"confirm", "deliver", "cancel", "return"} else {}

    if action == "confirm":
        # All-or-nothing: if any item is short, change_stock raises and the whole transaction aborts.
        for item in order.items:
            change_stock(db, products[item.product_id], -item.quantity, InventoryReason.ORDER_HOLD, actor_id)
        changes.update(stock_held=True, delivery_code=new_delivery_code())
    elif action == "deliver":
        # Paid at the door, in cash or to the shop's own UPI ID, as the owner recorded.
        changes.update(delivered_at=now, payment_method=method, payment_status="paid", stock_held=False,
                       code_failures=0)
        record_sales(db, [(products[i.product_id], i.quantity, i.unit_price) for i in order.items],
                     source=SaleSource.ORDER, basket_id=order.code, customer_id=order.customer_id)
    elif action == "cancel" and owner_out_of_stock(reason, actor, order.stock_held):
        # The owner confirmed, then found the shelf empty (P9.8): an arrival failure for each listing, no release.
        for product in products.values():
            record_arrival_failure(db, product, actor_id, now)
        changes.update(stock_held=False, closed_at=now, close_reason=OUT_OF_STOCK_AFTER_CONFIRM)
        reason = None  # a code, not words for the customer's timeline or notification
    elif action in {"cancel", "return"}:
        if order.stock_held:
            for item in order.items:
                change_stock(db, products[item.product_id], item.quantity, InventoryReason.ORDER_RELEASE, actor_id)
        close_reason = (reason or "").strip() or None
        if close_reason in RESERVED_REASONS:  # set only by NearShop (the owner's out-of-stock cancel above)
            close_reason = reason = None
        changes.update(stock_held=False, closed_at=now, close_reason=close_reason)
    elif action == "fail":
        changes["close_reason"] = (reason or "").strip() or None

    db.orders.set(order, **changes)

    if actor != "customer" and to_status in CUSTOMER_MESSAGES:
        message = CUSTOMER_MESSAGES[to_status]
        if order.close_reason and order.close_reason != OUT_OF_STOCK_AFTER_CONFIRM \
                and to_status in {OS.CANCELLED, OS.DELIVERY_FAILED}:
            no_response = (to_status == OS.CANCELLED and actor == "system"
                           and order.close_reason == NO_RESPONSE_REASON)
            message = "order_cancelled_no_response" if no_response else f"{message}_reason"
        notify(db, order.customer_id, f"order_{to_status.lower()}", link=f"/account/orders/{order.id}",
               message=message, params={"shop": shop.name, "total": order.total,
                                        "delivery_code": order.delivery_code, "reason": order.close_reason})
    if actor == "customer" and to_status == OS.CANCELLED:
        notify(db, shop.owner_id, "order_cancelled", link="/shop/orders", message="order_cancelled_by_customer",
               params={"code": order.code})

    record_event(db, entity="order", entity_id=order.id, shop_id=order.shop_id, from_status=from_status,
                 to_status=to_status, actor_role=actor, actor_id=actor_id, note=reason)
    return order


def cancel_stale_pending(db: Database) -> int:
    cutoff = utcnow() - timedelta(minutes=PENDING_TIMEOUT_MINUTES)
    stale = db.orders.find_raw({"status": OS.PENDING, "created_at": {"$lte": cutoff}}, {"_id": 1})
    for doc in stale:
        try:
            transition(db, doc["_id"], "cancel", None, NO_RESPONSE_REASON)
        except Exception as exc:
            log.warning("could not auto-cancel order %s: %s", doc["_id"], exc)
    return len(stale)
