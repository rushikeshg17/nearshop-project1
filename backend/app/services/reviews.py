"""Review integrity (P3.5, plan section 8.2): who may review, and what counts toward a shop's rating.

Who may review (check_eligibility, called by POST /reviews before anything is written):
  * within REVIEW_WINDOW_DAYS of the pickup's completion or the order's delivery;
  * from an account created at least MIN_ACCOUNT_AGE_DAYS before the purchase was placed;
  * from a phone, if the account has one, that is neither the shop's phone nor its owner's phone;
  * with a verified phone, once settings.review_requires_verified_phone is on (P4.1 turns it on).

What counts (recompute_shop, run in the same transaction as every review change, and by migration 6):
  * counted: at most one review per customer per shop in COUNT_WINDOW_DAYS. A later review inside that window
    is kept and shown, but counted is false.
  * held: when SELF_DEALING_SHARE or more of the shop's counted reviews from the last SELF_DEALING_WINDOW_DAYS
    come from customers whose only completed purchases (completed pickups, delivered orders) are at this shop,
    those reviews are held out of the rating and flagged (held_at) for the admin moderation queue (P3.6). A held
    review stays held until an admin decides it: waiting 30 days does not release it.
  * the rating is recomputed from counted, visible, unheld reviews: rating_sum, rating_count and
    rating_customers (distinct customers) on the shop. Nothing increments it. Shop.rating_avg shows a rating
    only from MIN_RATED_CUSTOMERS (app/models/core.py) distinct customers; below that the screens show "New shop" (P3.7).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from pymongo import UpdateOne
from pymongo.client_session import ClientSession
from pymongo.database import Database as MongoDatabase

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import Conflict
from app.models import OrderStatus, ReservationStatus, Shop, User

REVIEW_WINDOW_DAYS = 14
MIN_ACCOUNT_AGE_DAYS = 3
COUNT_WINDOW_DAYS = 30
SELF_DEALING_SHARE = 0.30
SELF_DEALING_WINDOW_DAYS = 30
VISIBLE = "visible"
HELD_SELF_DEALING = "self_dealing"
# The reviews a shop's rating is built from (recompute_shop writes counted on every review, false unless visible).
# Every other figure drawn from reviews (the star distribution, the accuracy trust signal) uses the same set.
RATED = {"counted": True, "held": {"$ne": True}}


def phone_key(phone: str | None) -> str | None:
    """The last ten digits of a phone number, so +91 98765 43210 and 9876543210 compare equal."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def check_eligibility(db: Database, user: User, shop: Shop, *, purchased_at: datetime,
                      completed_at: datetime) -> None:
    """Raise Conflict (409) when `user` may not review this purchase at `shop` (plan section 8.2)."""
    now = utcnow()
    if now - completed_at > timedelta(days=REVIEW_WINDOW_DAYS):
        raise Conflict(f"Reviews can be written within {REVIEW_WINDOW_DAYS} days of the purchase",
                       code="review_window_closed")
    if user.created_at > purchased_at - timedelta(days=MIN_ACCOUNT_AGE_DAYS):
        raise Conflict(f"Only accounts created at least {MIN_ACCOUNT_AGE_DAYS} days before the purchase can "
                       "review it", code="account_too_new")
    mine = phone_key(user.phone)
    if mine:
        owner = db.users.get(shop.owner_id)
        if mine in {phone_key(shop.phone), phone_key(owner.phone if owner else None)}:
            raise Conflict("Shops cannot be reviewed from their own phone number", code="review_not_allowed")
    if settings.review_requires_verified_phone and user.phone_verified_at is None:
        raise Conflict("Verify your phone number to write reviews", code="phone_not_verified")


def recompute(db: Database, shop_id: int) -> dict[str, int]:
    """recompute_shop inside the handle's transaction, if it has one."""
    return recompute_shop(db.mongo, shop_id, session=db.session)


def _single_shop_customers(mongo: MongoDatabase, customer_ids: list[int], shop_id: int,
                           session: ClientSession | None) -> set[int]:
    """The customers among customer_ids whose completed purchases are all at shop_id."""
    shops: dict[int, set[int]] = {c: set() for c in customer_ids}
    for coll, status in (("reservations", ReservationStatus.COMPLETED.value),
                         ("orders", OrderStatus.DELIVERED.value)):
        rows = mongo[coll].aggregate([
            {"$match": {"customer_id": {"$in": customer_ids}, "status": status}},
            {"$group": {"_id": "$customer_id", "shops": {"$addToSet": "$shop_id"}}},
        ], session=session)
        for row in rows:
            shops[row["_id"]].update(row["shops"])
    return {c for c, s in shops.items() if s <= {shop_id}}


def recompute_shop(mongo: MongoDatabase, shop_id: int, *, session: ClientSession | None = None,
                   now: datetime | None = None) -> dict[str, int]:
    """Recompute which of a shop's reviews count and are held, then its rating, from scratch. Pure function of
    the stored reviews and purchases, so running it twice changes nothing. Returns the shop's new totals."""
    now = now or utcnow()
    reviews = list(mongo["reviews"].find(
        {"shop_id": shop_id},
        {"customer_id": 1, "rating": 1, "created_at": 1, "status": 1, "counted": 1, "held": 1, "held_at": 1},
        sort=[("created_at", 1), ("_id", 1)], session=session))

    # One counted review per customer per COUNT_WINDOW_DAYS, oldest first, among the visible ones.
    last_counted: dict[int, datetime] = {}
    counted: dict[int, bool] = {}
    for r in reviews:
        if r.get("status", VISIBLE) != VISIBLE:
            counted[r["_id"]] = False
            continue
        prev = last_counted.get(r["customer_id"])
        counted[r["_id"]] = prev is None or r["created_at"] - prev >= timedelta(days=COUNT_WINDOW_DAYS)
        if counted[r["_id"]]:
            last_counted[r["customer_id"]] = r["created_at"]

    # Self-dealing: held reviews stay held (an admin decides them in P3.6); new ones are held when the share of
    # recent counted reviews from single-shop customers reaches SELF_DEALING_SHARE.
    held = {r["_id"]: bool(r.get("held")) for r in reviews}
    recent = [r for r in reviews if counted[r["_id"]]
              and r["created_at"] >= now - timedelta(days=SELF_DEALING_WINDOW_DAYS)]
    if recent:
        single = _single_shop_customers(mongo, sorted({r["customer_id"] for r in recent}), shop_id, session)
        suspect = [r for r in recent if r["customer_id"] in single]
        if suspect and len(suspect) / len(recent) >= SELF_DEALING_SHARE:
            for r in suspect:
                held[r["_id"]] = True

    ops: list[UpdateOne] = []
    for r in reviews:
        changes: dict[str, Any] = {}
        if r.get("counted") is not counted[r["_id"]]:
            changes["counted"] = counted[r["_id"]]
        if r.get("held") is not held[r["_id"]]:  # also writes both flags on reviews from before P3.5
            changes["held"] = held[r["_id"]]
        if held[r["_id"]] and not r.get("held_at"):
            changes.update(held_at=now, held_reason=HELD_SELF_DEALING)
        if changes:
            ops.append(UpdateOne({"_id": r["_id"]}, {"$set": changes}))
    if ops:
        mongo["reviews"].bulk_write(ops, ordered=False, session=session)

    rated = [r for r in reviews if counted[r["_id"]] and not held[r["_id"]]]
    totals = {"rating_sum": sum(int(r["rating"]) for r in rated), "rating_count": len(rated),
              "rating_customers": len({r["customer_id"] for r in rated})}
    mongo["shops"].update_one({"_id": shop_id}, {"$set": totals}, session=session)
    return totals
