"""Pickup reservation state machine.

    REQUESTED --confirm--> CONFIRMED --ready--> READY_FOR_PICKUP --complete--> COMPLETED
        |                      |                      |
        +--reject--> REJECTED  +------cancel / expire-+--> CANCELLED / EXPIRED

Stock is held when the shop confirms (so the unit is physically set aside) and returned
to stock if the reservation is cancelled or expires. Completing records how the customer paid (P6.1).

A customer who finds a CONFIRMED or READY_FOR_PICKUP item missing reports it instead of cancelling (action
not_available, P9.1, plan section 8.17): the reservation is CANCELLED with close_reason not_available_on_arrival, the
hold ends without putting the units back (they were never there), an arrival_failure inventory event with delta 0 is
written, the listing is unconfirmed until the shop next counts it, and the owner is told. An owner who confirmed and then
finds the shelf empty cancels with the reason out_of_stock_after_confirm (P9.8), handled the same way
(record_arrival_failure): the units are not put back, and the customer is told the reservation was cancelled.

A call-only listing (P9.8, app/schemas/serializers.py is_call_only) takes no reservations: 409 listing_call_only.
"""
import logging
import secrets
from datetime import datetime, timedelta

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import AppError, Conflict, Forbidden, InvalidTransition, NotFound
from app.models import (
    InventoryEvent,
    InventoryReason,
    Product,
    Reservation,
    Role,
    SaleSource,
    Shop,
    User,
)
from app.models import (
    ReservationStatus as RS,
)
from app.models.activity import PAYMENT_METHODS
from app.schemas.serializers import OUT_OF_STOCK_AFTER_CONFIRM, is_call_only
from app.services.fulfillment_common import record_event, record_sales
from app.services.inventory import change_stock
from app.services.notifications import notify
from app.utils.text import short_code

log = logging.getLogger(__name__)

ACTIVE = {RS.REQUESTED, RS.CONFIRMED, RS.READY_FOR_PICKUP}
# Customers cannot see or reserve from a shop whose verification is not in force (P3.3).
SHOP_NOT_VERIFIED = "This shop is not available on NearShop right now"
MAX_ACTIVE_PER_CUSTOMER = 5
# Public stock bands (P10.1, plan section 8.20). For a shop that has not opted in to exact stock (Shop.show_exact_stock),
# a customer's error never states a count, and one request may ask for at most settings.public_stock_cap units.
NOT_ENOUGH_STOCK = "Not enough in stock for that quantity"


def check_public_cap(shop: Shop, quantity: int) -> None:
    """Refuse (422 quantity_above_cap) a reservation or order line above settings.public_stock_cap from a shop without
    the exact-stock opt-in; opted-in shops keep the request schemas' limits."""
    cap = settings.public_stock_cap
    if not shop.show_exact_stock and quantity > cap:
        raise AppError(f"You can ask for up to {cap} at a time online. Call the shop for more",
                       code="quantity_above_cap", status_code=422)

# The customer's "Not available when I arrived" report (P9.1): its action and the close_reason it records.
NOT_AVAILABLE = "not_available"
NOT_AVAILABLE_ON_ARRIVAL = "not_available_on_arrival"
# close_reason codes only NearShop sets: never stored from a typed reason.
RESERVED_REASONS = {NOT_AVAILABLE_ON_ARRIVAL, OUT_OF_STOCK_AFTER_CONFIRM}
# The listing is call-only (P9.8): customers are asked to call the shop instead.
CALL_ONLY = "This item's stock has not been confirmed recently. Call the shop to check"


def check_not_call_only(product: Product) -> None:
    """Refuse (409 listing_call_only) a reservation or delivery order for a call-only listing (P9.8)."""
    if is_call_only(product.stock_updated_at, product.stock_unconfirmed_at):
        raise Conflict(CALL_ONLY, code="listing_call_only")


def owner_out_of_stock(reason: str | None, actor: str, stock_held: bool) -> bool:
    """Whether a cancellation is the owner's "Out of stock after confirming" (P9.8): that reason, from the owner (or an
    admin), on a reservation or order whose stock is held (confirmed and not yet handed over)."""
    return (reason or "").strip() == OUT_OF_STOCK_AFTER_CONFIRM and actor in {"owner", "admin"} and stock_held


def record_arrival_failure(db: Database, product: Product, actor_id: int | None, now: datetime) -> None:
    """An arrival failure on one listing (plan section 8.17), inside the caller's transaction: an arrival_failure
    inventory event with delta 0 (the held units are not put back, since they were never on the shelf), and the
    listing is unconfirmed until the shop next sets its stock. Used by the customer's report (P9.1) and the owner's
    out-of-stock cancellation of a reservation or an order (P9.8)."""
    db.inventory_events.insert(InventoryEvent(
        product_id=product.id, shop_id=product.shop_id, delta=0, quantity_after=product.quantity,
        reason=InventoryReason.ARRIVAL_FAILURE, actor_id=actor_id, created_at=now))
    db.products.set(product, stock_unconfirmed_at=now)

# (from_status, action) -> (to_status, who may do it)
TRANSITIONS: dict[tuple[RS, str], tuple[RS, set[str]]] = {
    (RS.REQUESTED, "confirm"): (RS.CONFIRMED, {"owner"}),
    (RS.REQUESTED, "reject"): (RS.REJECTED, {"owner"}),
    (RS.REQUESTED, "cancel"): (RS.CANCELLED, {"customer"}),
    (RS.REQUESTED, "expire"): (RS.EXPIRED, {"system"}),
    (RS.CONFIRMED, "ready"): (RS.READY_FOR_PICKUP, {"owner"}),
    (RS.CONFIRMED, "complete"): (RS.COMPLETED, {"owner"}),
    (RS.CONFIRMED, "cancel"): (RS.CANCELLED, {"customer", "owner"}),
    (RS.CONFIRMED, NOT_AVAILABLE): (RS.CANCELLED, {"customer"}),
    (RS.CONFIRMED, "expire"): (RS.EXPIRED, {"system"}),
    (RS.READY_FOR_PICKUP, "complete"): (RS.COMPLETED, {"owner"}),
    (RS.READY_FOR_PICKUP, "cancel"): (RS.CANCELLED, {"customer", "owner"}),
    (RS.READY_FOR_PICKUP, NOT_AVAILABLE): (RS.CANCELLED, {"customer"}),
    (RS.READY_FOR_PICKUP, "expire"): (RS.EXPIRED, {"system"}),
}


def check_payment_method(shop: Shop, method: str | None) -> str:
    """The payment method the owner chose when completing a pickup or delivering an order (P6.1, plan section 8.8):
    cod (cash) or upi_direct (the shop's own UPI ID, so refused while the shop has none). NearShop never holds the
    money either way."""
    if not method:
        raise AppError("Choose how the customer paid: cash or UPI", code="payment_method_required", status_code=422)
    if method not in PAYMENT_METHODS:
        raise AppError("Choose cash or UPI as the payment method", code="invalid_payment_method", status_code=422)
    if method == "upi_direct" and not shop.upi_id:
        raise Conflict("Add your shop's UPI ID in shop settings before recording a UPI payment", code="upi_not_set_up")
    return method


def allowed_actions(reservation: Reservation, actor: str) -> list[str]:
    return [action for (status, action), (_, actors) in TRANSITIONS.items() if status == reservation.status and actor in actors]


def create_reservation(db: Database, customer: User, product_id: int, quantity: int, note: str | None) -> Reservation:
    try:
        return db.transaction(lambda: _create_reservation(db, customer, product_id, quantity, note))
    except DuplicateKeyError:
        # The unique partial index on (customer, product) for active reservations caught a double submit.
        raise Conflict("You already have an active reservation for this item", code="duplicate_reservation") from None


def _create_reservation(db: Database, customer: User, product_id: int, quantity: int, note: str | None) -> Reservation:
    product = db.products.get(product_id)
    if product is None or not product.is_active:
        raise NotFound("This product is no longer listed")
    shop = db.shops.get(product.shop_id)
    if not shop.is_active or not shop.offers_pickup:
        raise Conflict("This shop is not accepting pickup reservations right now")
    if not shop.is_live:  # not verified, or its approval has ended (P3.3, plan section 8.1)
        raise Conflict(SHOP_NOT_VERIFIED, code="shop_not_verified")
    check_not_call_only(product)
    if quantity < 1 or quantity > 20:
        raise Conflict("You can reserve between 1 and 20 units")
    check_public_cap(shop, quantity)
    if product.quantity < quantity:
        if not product.quantity:
            message = "This item is out of stock"
        elif shop.show_exact_stock:
            message = f"Only {product.quantity} in stock"
        else:
            message = NOT_ENOUGH_STOCK
        raise Conflict(message, code="insufficient_stock")

    active = db.reservations.find_raw({"customer_id": customer.id, "status": {"$in": list(ACTIVE)}}, {"product_id": 1})
    if any(r["product_id"] == product.id for r in active):
        raise Conflict("You already have an active reservation for this item", code="duplicate_reservation")
    if len(active) >= MAX_ACTIVE_PER_CUSTOMER:
        raise Conflict(f"You can hold up to {MAX_ACTIVE_PER_CUSTOMER} active reservations at a time")

    now = utcnow()
    res = db.reservations.insert(Reservation(
        code=short_code("R"),
        customer_id=customer.id,
        shop_id=shop.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=product.price,
        status=RS.REQUESTED,
        note=(note or "").strip() or None,
        hold_minutes=shop.hold_minutes,
        expires_at=now + timedelta(minutes=settings.request_response_minutes),
        created_at=now,
        updated_at=now,
    ))
    record_event(db, entity="reservation", entity_id=res.id, shop_id=shop.id, from_status=None,
                 to_status=RS.REQUESTED, actor_role="customer", actor_id=customer.id)
    notify(db, shop.owner_id, "reservation_new", link="/shop/reservations", message="reservation_new",
           params={"code": res.code, "customer": customer.name, "quantity": quantity, "product": product.name,
                   "minutes": settings.request_response_minutes})
    return res


def _actor_for(user: User | None, res: Reservation, shop_owner_id: int) -> str:
    if user is None:
        return "system"
    if user.role == Role.CUSTOMER and res.customer_id == user.id:
        return "customer"
    if user.role == Role.OWNER and shop_owner_id == user.id:
        return "owner"
    if user.role == Role.ADMIN:
        return "admin"
    raise Forbidden("This reservation belongs to someone else")


def transition(db: Database, reservation_id: int, action: str, user: User | None,
               reason: str | None = None, code: str | None = None, payment_method: str | None = None) -> Reservation:
    """Move a reservation through its state machine. The reservation, the stock hold, the audit
    event, the sale and the notifications commit together or not at all. Completing needs the reservation
    code the customer shows at the counter (P3.5, plan section 8.2), compared case-insensitively, and the payment
    method, cod or upi_direct (P6.1, check_payment_method); other actions ignore payment_method."""
    return db.transaction(lambda: _transition(db, reservation_id, action, user, reason, code, payment_method))


def code_matches(entered: str | None, expected: str) -> bool:
    """Whether the code the shop typed is the reservation's: case and outer spaces do not matter."""
    return bool(entered) and secrets.compare_digest(entered.strip().upper().encode(), expected.upper().encode())


def _transition(db: Database, reservation_id: int, action: str, user: User | None, reason: str | None,
                code: str | None = None, payment_method: str | None = None) -> Reservation:
    # Loaded inside the transaction: if someone else changes it first, this transaction is
    # retried and sees the new status instead of overwriting it.
    res = db.reservations.get(reservation_id)
    if res is None:
        raise NotFound("Reservation not found")
    product = db.products.get(res.product_id)
    shop = db.shops.get(res.shop_id)
    actor = _actor_for(user, res, shop.owner_id)
    key = (res.status, action)
    if action == NOT_AVAILABLE and res.arrival_reported_at is not None:
        raise InvalidTransition("You have already reported this item as not available")
    if action == NOT_AVAILABLE and key not in TRANSITIONS:
        raise InvalidTransition("You can report an item as not available only while the shop is holding it for you")
    if key not in TRANSITIONS:
        raise InvalidTransition(f"Cannot {action} a reservation that is {res.status.replace('_', ' ').lower()}")
    to_status, actors = TRANSITIONS[key]
    if action == NOT_AVAILABLE:
        # Only the customer who stood at the counter can say the item was missing, and only once (P9.1).
        if actor != "customer":
            raise Forbidden("Only the customer can report that the item was not available")
    elif actor not in actors and actor != "admin":
        raise Forbidden(f"Only the {' or '.join(sorted(actors))} can {action} this reservation")
    if action == "complete" and settings.completion_codes_required and not code_matches(code, res.code):
        raise Conflict("Enter the reservation code the customer shows you" if not (code or "").strip()
                       else "That is not this reservation's code", code="wrong_code")
    method = check_payment_method(shop, payment_method) if action == "complete" else None

    now = utcnow()
    from_status = res.status
    actor_id = user.id if user else None
    changes: dict = {"status": to_status}

    if action == "confirm":
        change_stock(db, product, -res.quantity, InventoryReason.RESERVATION_HOLD, actor_id)
        changes.update(stock_held=True, confirmed_at=now, expires_at=now + timedelta(minutes=res.hold_minutes))
        notify(db, res.customer_id, "reservation_confirmed", link=f"/account/reservations/{res.id}",
               message="reservation_confirmed", params={"shop": shop.name, "quantity": res.quantity,
                                                        "product": product.name, "minutes": res.hold_minutes,
                                                        "code": res.code})
    elif action == "ready":
        changes.update(ready_at=now, expires_at=max(res.expires_at, now + timedelta(minutes=res.hold_minutes)))
        notify(db, res.customer_id, "reservation_ready", link=f"/account/reservations/{res.id}",
               message="reservation_ready", params={"product": product.name, "shop": shop.name, "code": res.code})
    elif action == "complete":
        # The held unit left the shop with the customer, who paid the shop in cash or to its own UPI ID.
        changes.update(completed_at=now, stock_held=False, payment_method=method, payment_status="paid")
        record_sales(db, [(product, res.quantity, res.unit_price)], source=SaleSource.RESERVATION,
                     basket_id=res.code, customer_id=res.customer_id)
        notify(db, res.customer_id, "reservation_completed", link=f"/account/reservations/{res.id}",
               message="reservation_completed", params={"shop": shop.name})
    elif action == NOT_AVAILABLE:
        _report_not_available(db, res, product, shop, actor_id, now, changes)
        reason = None  # the timeline shows the status; close_reason and arrival_reported_at record the report
    elif action == "cancel" and owner_out_of_stock(reason, actor, res.stock_held):
        # The owner confirmed, then found the shelf empty (P9.8): an arrival failure, not a release.
        record_arrival_failure(db, product, actor_id, now)
        changes.update(stock_held=False, closed_at=now, close_reason=OUT_OF_STOCK_AFTER_CONFIRM)
        reason = None  # a code, not words for the customer's timeline
        notify(db, res.customer_id, "reservation_cancelled", link=f"/account/reservations/{res.id}",
               message="reservation_cancelled_by_shop", params={"shop": shop.name, "reason": None})
    else:  # reject / cancel / expire
        if res.stock_held:
            change_stock(db, product, res.quantity, InventoryReason.RESERVATION_RELEASE, actor_id)
        close_reason = (reason or "").strip() or None
        if close_reason in RESERVED_REASONS:  # set only by the arrival report and the owner's out-of-stock cancel
            close_reason = reason = None
        changes.update(stock_held=False, closed_at=now, close_reason=close_reason)
        if action == "reject":
            # The owner's reason, as typed, is the body when there is one.
            notify(db, res.customer_id, "reservation_rejected", link=f"/search?q={product.name}",
                   message="reservation_rejected_reason" if close_reason else "reservation_rejected",
                   params={"shop": shop.name, "reason": close_reason})
        elif action == "cancel" and actor == "customer":
            notify(db, shop.owner_id, "reservation_cancelled", link="/shop/reservations",
                   message="reservation_cancelled_by_customer",
                   params={"code": res.code, "quantity": res.quantity, "product": product.name})
        elif action == "cancel":
            notify(db, res.customer_id, "reservation_cancelled", link=f"/account/reservations/{res.id}",
                   message="reservation_cancelled_by_shop_reason" if close_reason else "reservation_cancelled_by_shop",
                   params={"shop": shop.name, "reason": close_reason})
        elif action == "expire":
            was_request = from_status == RS.REQUESTED
            notify(db, res.customer_id, "reservation_expired", link=f"/account/reservations/{res.id}",
                   message="reservation_request_expired" if was_request else "reservation_hold_expired",
                   params={"shop": shop.name, "product": product.name})
            if not was_request:
                notify(db, shop.owner_id, "reservation_expired", link="/shop/reservations",
                       message="reservation_expired_shop",
                       params={"code": res.code, "quantity": res.quantity, "product": product.name})

    db.reservations.set(res, **changes)
    record_event(db, entity="reservation", entity_id=res.id, shop_id=res.shop_id, from_status=from_status,
                 to_status=to_status, actor_role=actor, actor_id=actor_id, note=reason)
    return res


def _report_not_available(db: Database, res: Reservation, product: Product, shop: Shop, actor_id: int | None,
                          now: datetime, changes: dict) -> None:
    """The customer's arrival report (P9.1, plan section 8.17), inside the transition's transaction. The hold ends
    without the units going back on the shelf: the count stays as it is, and an arrival_failure event with delta 0
    records the report. The listing is unconfirmed until the shop's next stock count clears it."""
    record_arrival_failure(db, product, actor_id, now)
    changes.update(stock_held=False, closed_at=now, close_reason=NOT_AVAILABLE_ON_ARRIVAL, arrival_reported_at=now)
    # The count the customer saw when reserving: the held units are no longer in product.quantity.
    shown = product.quantity + (res.quantity if res.stock_held else 0)
    notify(db, shop.owner_id, "arrival_failure", link=f"/shop/inventory?focus={product.id}",
           message="arrival_failure", params={"code": res.code, "product": product.name, "quantity": shown})


def expire_due(db: Database) -> int:
    """Expire reservations whose timer has run out. Called by the scheduler and lazily on reads."""
    due = db.reservations.find_raw({"status": {"$in": list(ACTIVE)}, "expires_at": {"$lte": utcnow()}}, {"_id": 1})
    for doc in due:
        try:
            transition(db, doc["_id"], "expire", None)
        except Exception as exc:  # never let one bad document stop the sweep
            log.warning("could not expire reservation %s: %s", doc["_id"], exc)
    return len(due)
