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
    review stays held until an admin decides it: waiting 30 days does not release it, and a review an admin
    kept while it was held (kept_held) is never held again. Keeping a review for any other reason, such as
    after reports, does not exempt it from the rule.
  * the rating is recomputed from counted, visible, unheld reviews: rating_sum, rating_count and
    rating_customers (distinct customers) on the shop. Nothing increments it. Shop.rating_avg shows a rating
    only from MIN_RATED_CUSTOMERS (app/models/core.py) distinct customers; below that the screens show "New shop" (P3.7).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pymongo import UpdateOne
from pymongo.client_session import ClientSession
from pymongo.database import Database as MongoDatabase

from app.core.config import settings
from app.core.database import Collection, Database, utcnow
from app.core.errors import Conflict, NotFound
from app.models import OrderStatus, ReservationStatus, Shop, User
from app.models.activity import Moderation, Review, ReviewReply, ReviewReport

REVIEW_WINDOW_DAYS = 14
MIN_ACCOUNT_AGE_DAYS = 3
COUNT_WINDOW_DAYS = 30
SELF_DEALING_SHARE = 0.30
SELF_DEALING_WINDOW_DAYS = 30
VISIBLE, HIDDEN, REMOVED = "visible", "hidden", "removed"
HELD_SELF_DEALING = "self_dealing"
# The reviews a shop's rating is built from (recompute_shop writes counted on every review, false unless visible).
# Every other figure drawn from reviews (the star distribution, the accuracy trust signal) uses the same set.
RATED = {"counted": True, "held": {"$ne": True}}
# The reviews public lists show (P3.6): not hidden or removed by moderation (reviews from before P3.5 have no status).
PUBLIC = {"status": {"$nin": [HIDDEN, REMOVED]}}


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
        {"customer_id": 1, "rating": 1, "created_at": 1, "status": 1, "counted": 1, "held": 1, "held_at": 1,
         "kept_held": 1},
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
                # An admin who kept a review while it was held (P3.6) has cleared it of self-dealing: it is
                # never held again. A review kept after reports was never judged for self-dealing.
                held[r["_id"]] = held[r["_id"]] or not r.get("kept_held")

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


# ---------------------------------------------------------------- moderation (P3.6, plan section 8.2)
#
# Reports: any signed-in user may report a review, or a shop's reply, once (review_reports, unique per user and
# target), for one of REPORT_REASONS with an optional note; app/core/ratelimit.py allows 10 reports per user per
# day. Reports from HIDE_AFTER_REPORTS distinct users hide it until an admin decides it, and the shop's rating is
# recomputed. A report of something already decided is recorded but changes nothing.
# Queue: reported reviews and replies and self-dealing reviews (held) wait, oldest first, until an admin decides
# them: keep (visible again, and a held review counts), hide or remove. The SLA is triage within TRIAGE_HOURS
# and a decision within DECIDE_HOURS of entering the queue, reported per month by sla().

ReportReason = Literal["fake", "abusive", "personal_info", "off_topic", "other"]
REPORT_REASONS: tuple[str, ...] = get_args(ReportReason)
Target = Literal["review", "reply"]
REVIEW, REPLY = "review", "reply"
Decision = Literal["keep", "hide", "remove"]
KEEP = "keep"
DECISION_STATUS = {"keep": VISIBLE, "hide": HIDDEN, "remove": REMOVED}
HIDE_AFTER_REPORTS = 3
REPLY_EDIT_HOURS = 24
TRIAGE_HOURS = 24
DECIDE_HOURS = 72
QUEUE_LIMIT = 200


def reports(db: Database) -> Collection[ReviewReport]:
    return Collection(db, "review_reports", ReviewReport)


def _target(review: Review, target: str) -> Moderation:
    """The review itself, or its reply. NotFound when there is no such reply."""
    if target == REPLY:
        if review.reply is None:
            raise NotFound("This review has no reply")
        return review.reply
    return review


def _save(db: Database, review: Review, target: str, fields: dict[str, Any]) -> None:
    """Write moderation fields of the review, or of its reply, and keep the loaded model in step."""
    thing = _target(review, target)
    for k, v in fields.items():
        setattr(thing, k, v)
    prefix = "reply." if target == REPLY else ""
    db.reviews.update_one({"_id": review.id}, {"$set": {prefix + k: v for k, v in fields.items()}})


def report(db: Database, user: User, review_id: int, target: str, reason: str, note: str | None) -> dict:
    """File `user`'s report of a review or its reply. Reporting the same thing twice counts once. Returns the
    target's public moderation state."""

    def run() -> dict:
        review = db.reviews.get(review_id)
        if review is None or review.status == REMOVED:
            raise NotFound("Review not found")
        thing = _target(review, target)
        if thing.status == REMOVED:
            raise NotFound("Reply not found")
        if target == REVIEW and review.customer_id == user.id:
            raise Conflict("You cannot report your own review", code="own_review")
        if target == REPLY and review.reply and review.reply.author_id == user.id:
            raise Conflict("You cannot report your own reply", code="own_reply")
        store = reports(db)
        if store.exists({"review_id": review_id, "target": target, "reporter_id": user.id}):
            return {"ok": True, "already_reported": True}
        store.insert(ReviewReport(review_id=review_id, target=target, reporter_id=user.id, reason=reason,
                                  note=(note or "").strip() or None))
        now = utcnow()
        fields: dict[str, Any] = {"report_count": store.count({"review_id": review_id, "target": target})}
        if thing.decided_at is None:
            fields["reported_at"] = thing.reported_at or now
            if fields["report_count"] >= HIDE_AFTER_REPORTS and thing.status == VISIBLE:
                fields["status"] = HIDDEN
        _save(db, review, target, fields)
        if target == REVIEW and fields.get("status") == HIDDEN:
            recompute(db, review.shop_id)
        return {"ok": True, "already_reported": False}

    return db.transaction(run)


def triage(db: Database, review_id: int, target: str) -> Review:
    """An admin picks the item up: triaged_at is set the first time."""

    def run() -> Review:
        review = _get(db, review_id)
        if _target(review, target).triaged_at is None:
            _save(db, review, target, {"triaged_at": utcnow()})
        return review

    return db.transaction(run)


def decide(db: Database, admin: User, review_id: int, target: str, decision: str, note: str | None) -> Review:
    """keep, hide or remove a review or a reply. Sets decided_at (and triaged_at) the first time, and recomputes
    the shop's rating. Keeping a held review releases it into the rating."""

    def run() -> Review:
        review = _get(db, review_id)
        thing = _target(review, target)
        now = utcnow()
        fields: dict[str, Any] = {"status": DECISION_STATUS[decision], "decision": decision, "decided_by": admin.id,
                                  "decision_note": (note or "").strip() or None,
                                  "decided_at": thing.decided_at or now, "triaged_at": thing.triaged_at or now}
        _save(db, review, target, fields)
        if target == REVIEW:
            if decision == KEEP and review.held:
                db.reviews.update_one({"_id": review.id}, {"$set": {"held": False, "kept_held": True}})
                review.held, review.kept_held = False, True
            recompute(db, review.shop_id)
        return review

    return db.transaction(run)


def _get(db: Database, review_id: int) -> Review:
    review = db.reviews.get(review_id)
    if review is None:
        raise NotFound("Review not found")
    return review


# ---- shop replies

def reply(db: Database, shop: Shop, owner: User, review_id: int, text: str) -> ReviewReply:
    """The shop's one public reply to a review of it."""

    def run() -> ReviewReply:
        review = db.reviews.get(review_id)
        if review is None or review.shop_id != shop.id or review.status == REMOVED:
            raise NotFound("Review not found")
        if review.reply is not None:
            raise Conflict("This review already has your reply; edit it instead", code="reply_exists")
        new = ReviewReply(text=text, author_id=owner.id)
        db.reviews.set(review, reply=new)
        return new

    return db.transaction(run)


def edit_reply(db: Database, shop: Shop, review_id: int, text: str) -> ReviewReply:
    """Change the reply's text, within REPLY_EDIT_HOURS of writing it and while no one has reported it."""

    def run() -> ReviewReply:
        review = db.reviews.get(review_id)
        if review is None or review.shop_id != shop.id or review.status == REMOVED or review.reply is None:
            raise NotFound("Reply not found")
        current = review.reply
        if utcnow() - current.created_at > timedelta(hours=REPLY_EDIT_HOURS):
            raise Conflict(f"A reply can be edited for {REPLY_EDIT_HOURS} hours after it is written",
                           code="reply_locked")
        if current.reported_at is not None or current.decided_at is not None or current.status != VISIBLE:
            raise Conflict("A reported reply cannot be edited while it is reviewed", code="reply_under_review")
        _save(db, review, REPLY, {"text": text, "edited_at": utcnow()})
        return current

    return db.transaction(run)


# ---- the admin queue and the monthly SLA

def _entered(doc: dict) -> datetime | None:
    """When a review (or reply sub-document) entered the queue: its first report, or its hold."""
    times = [t for t in (doc.get("reported_at"), doc.get("held_at")) if t is not None]
    return min(times) if times else None


QUEUE_FILTER = {"$or": [{"decided_at": None, "reported_at": {"$type": "date"}},
                        {"held": True, "status": {"$nin": [HIDDEN, REMOVED]}}]}
REPLY_QUEUE_FILTER = {"reply.decided_at": None, "reply.reported_at": {"$type": "date"}}


def sla_filters(start: datetime, end: datetime) -> tuple[dict, dict]:
    """The reviews, and the replies, that entered the queue in [start, end). The $type condition lets MongoDB
    use the partial indexes (reported, held, reply_reported), which are filtered on it."""
    window = {"$type": "date", "$gte": start, "$lt": end}
    return {"$or": [{"reported_at": window}, {"held_at": window}]}, {"reply.reported_at": window}


def queue(db: Database) -> list[dict[str, Any]]:
    """Everything waiting for an admin, oldest first (the first QUEUE_LIMIT): reported reviews and replies not
    yet decided, and held reviews an admin has not hidden or removed (keeping one releases it). A review kept
    after reports and held later by the self-dealing rule comes back, aged from its hold. Each branch of the
    query has its own index in database/schema.py (reported, held_open, reply_reported)."""
    rows = db.reviews.find_raw(QUEUE_FILTER)
    reply_rows = db.reviews.find_raw(REPLY_QUEUE_FILTER)
    items = [(REVIEW, r, r.get("held_at") if r.get("decided_at") else _entered(r)) for r in rows]
    items += [(REPLY, r, r["reply"]["reported_at"]) for r in reply_rows]
    items = [i for i in items if i[2] is not None]
    items.sort(key=lambda i: (i[2], i[1]["_id"]))
    items = items[:QUEUE_LIMIT]
    ids = list({r["_id"] for _, r, _ in items})
    counts: dict[tuple[int, str], dict[str, Any]] = {}
    for rep in reports(db).find_raw({"review_id": {"$in": ids}}, sort=[("created_at", 1)]):
        c = counts.setdefault((rep["review_id"], rep["target"]), {"reasons": {}, "notes": []})
        c["reasons"][rep["reason"]] = c["reasons"].get(rep["reason"], 0) + 1
        if rep.get("note"):
            c["notes"].append(rep["note"])
    now = utcnow()
    out = []
    for target, r, entered in items:
        thing = r["reply"] if target == REPLY else r
        flags = []
        if thing.get("reported_at"):
            flags.append("reported")
        if target == REVIEW and r.get("held"):
            flags.append("held_self_dealing")
        if thing.get("status", VISIBLE) == HIDDEN:
            flags.append("hidden_by_reports")
        out.append({
            "target": target, "review_id": r["_id"], "shop_id": r["shop_id"], "entered_at": entered,
            "age_hours": round((now - entered).total_seconds() / 3600, 1),
            "status": thing.get("status", VISIBLE), "triaged_at": thing.get("triaged_at"),
            "rating": r["rating"], "comment": r.get("comment"), "created_at": r["created_at"],
            "reply_text": (r.get("reply") or {}).get("text"),
            "reports": {"count": thing.get("report_count", 0),
                        **counts.get((r["_id"], target), {"reasons": {}, "notes": []})},
            "flags": flags, "actions": list(DECISION_STATUS),
        })
    return out


def sla_shares(items: list[tuple[datetime, datetime | None, datetime | None]], now: datetime) -> dict[str, Any]:
    """The month's SLA from (entered, triaged, decided) times. An item counts toward a share once its deadline
    has passed or it was acted on; one still inside its deadline is pending and left out of that share."""
    triage_due = timedelta(hours=TRIAGE_HOURS)
    decide_due = timedelta(hours=DECIDE_HOURS)
    t_base = [i for i in items if i[1] is not None or now - i[0] > triage_due]
    d_base = [i for i in items if i[2] is not None or now - i[0] > decide_due]
    t_ok = sum(1 for e, t, _ in t_base if t is not None and t - e <= triage_due)
    d_ok = sum(1 for e, _, d in d_base if d is not None and d - e <= decide_due)
    return {
        "items": len(items), "open": sum(1 for i in items if i[2] is None),
        "triage_counted": len(t_base), "triaged_within_24h": t_ok,
        "triaged_share": round(t_ok / len(t_base), 4) if t_base else None,
        "decision_counted": len(d_base), "decided_within_72h": d_ok,
        "decided_share": round(d_ok / len(d_base), 4) if d_base else None,
    }


def sla(db: Database, start: datetime, end: datetime) -> dict[str, Any]:
    """SLA shares for the reviews and replies that entered the queue in [start, end)."""
    review_filter, reply_filter = sla_filters(start, end)
    items: list[tuple[datetime, datetime | None, datetime | None]] = []
    for r in db.reviews.find_raw(review_filter,
                                 {"reported_at": 1, "held_at": 1, "triaged_at": 1, "decided_at": 1}):
        entered = _entered(r)
        if entered is not None and start <= entered < end:
            items.append((entered, r.get("triaged_at"), r.get("decided_at")))
    for r in db.reviews.find_raw(reply_filter, {"reply": 1}):
        rp = r["reply"]
        items.append((rp["reported_at"], rp.get("triaged_at"), rp.get("decided_at")))
    return sla_shares(items, utcnow())
