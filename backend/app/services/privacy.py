"""Privacy rights and retention (P3.2; plan sections 8.4 and 8.5; docs/legal/retention.md).

  * Export: export_data returns the account's profile, consents, nominee, reservations, orders, reviews
    (without admin moderation bookkeeping), the review reports the user filed, notifications and search
    history as one JSON document (GET /api/me/export, 3 a day).
  * Consent: the newest consents row for a (user, purpose) is the one in force (P3.1). set_consent appends a
    row; it never updates one. Withdrawing personalised suggestions also unlinks the user's past searches.
  * Nomination: one nominee (a name, and a phone or an email) stored on the user, in the export, removed on
    deletion. Support acts on a nomination only with documentary proof (retention.md).
  * Deletion: deletion_plan says what deleting the account would do now, and request_deletion does it:
      - nothing open: the account is anonymised at once (anonymise);
      - reservations or orders the user may cancel: refused with DeletionBlocked (409) listing them, unless
        the caller asked to cancel them first, which releases their stock;
      - orders past that point (PREPARING, OUT_FOR_DELIVERY, DELIVERY_FAILED): the deletion is scheduled; the
        process_deletions job runs it once they close, and flags it for an admin if they are still open
        DELETION_GRACE (7 days) later;
      - owners: the same rules for their shop's open work, then every UPI Autopay mandate of their shops is
        cancelled at the payment gateway (P9.6; the deletion waits while the gateway cannot be reached), the
        shop is hidden, its listings deactivated and the owner anonymised;
      - admins: refused; an admin is removed with the operator CLI.
  * Retention: run_retention (the nightly retention job, app/jobs.py) removes user_id and the demand key (P9.7)
    from searches older than 30 days and clears the contact phone and address of orders closed more than 180 days ago, unless
    an open grievance refers to the order. TTL indexes (database/schema.py, migration 4) delete
    search_history after 180 days and notifications after 90.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import AppError, Conflict, Forbidden
from app.models import Order, OrderStatus, Reservation, ReservationStatus, Role, User
from app.schemas.requests import OPTIONAL_PURPOSES, REQUIRED_PURPOSES
from app.schemas.serializers import iso
from app.services import orders as order_service
from app.services import reservations as reservation_service
from app.services.notifications import notify

log = logging.getLogger(__name__)

RS, OS = ReservationStatus, OrderStatus
OPEN_RESERVATIONS = (RS.REQUESTED, RS.CONFIRMED, RS.READY_FOR_PICKUP)
CANCELLABLE_ORDERS = (OS.PENDING, OS.SHOP_CONFIRMED)  # the customer may cancel these
IN_FLIGHT_ORDERS = (OS.PREPARING, OS.OUT_FOR_DELIVERY, OS.DELIVERY_FAILED)
CLOSED_ORDERS = (OS.DELIVERED, OS.CANCELLED, OS.RETURNED_TO_SHOP)

DELETION_GRACE = timedelta(days=7)
SEARCH_USER_ID_KEEP = timedelta(days=30)
DEMAND_KEY_KEEP = timedelta(days=30)  # P9.7, plan section 8.19
ORDER_CONTACT_KEEP = timedelta(days=180)
DELETED_NAME = "Deleted user"
FORMER_CUSTOMER = "Former customer"
# P3.8 stores grievances with these statuses; any other status is open and holds the order's contact details.
GRIEVANCE_CLOSED = ("resolved",)


def deleted_email(user_id: int) -> str:
    """The placeholder that replaces a deleted account's email: unique (uq_email) and personal to no one."""
    return f"deleted-{user_id}@deleted.invalid"


# ---------------------------------------------------------------- consent


def consent_state(db: Database, user_id: int) -> dict[str, bool]:
    """The consent in force for every purpose: the newest row's granted, or False without a row."""
    state = dict.fromkeys((*REQUIRED_PURPOSES, *OPTIONAL_PURPOSES), False)
    seen: set[str] = set()
    for row in db.mongo["consents"].find({"user_id": user_id}, {"purpose": 1, "granted": 1},
                                         sort=[("at", -1), ("_id", -1)], session=db.session):
        if row["purpose"] in state and row["purpose"] not in seen:
            seen.add(row["purpose"])
            state[row["purpose"]] = bool(row["granted"])
    return state


def consent_granted(db: Database, user_id: int, purpose: str) -> bool:
    row = db.mongo["consents"].find_one({"user_id": user_id, "purpose": purpose}, {"granted": 1},
                                        sort=[("at", -1), ("_id", -1)], session=db.session)
    return bool(row and row.get("granted"))


def _consent_rows(db: Database, user_id: int, changes: dict[str, bool], at: datetime) -> list[dict]:
    return [{"_id": row_id, "user_id": user_id, "purpose": purpose, "notice_version": settings.notice_version,
             "granted": granted, "at": at, "source": "web"}
            for row_id, (purpose, granted) in zip(db.next_ids("consents", len(changes)), changes.items())]


def set_consent(db: Database, user_id: int, purpose: str, granted: bool) -> dict[str, bool]:
    """Grant or withdraw an optional purpose by appending a consents row. Withdrawing personalised
    suggestions also removes user_id from the user's past searches, so none of them stays linked."""
    if purpose not in OPTIONAL_PURPOSES:
        raise ValueError(f"{purpose!r} is not an optional purpose")

    def write() -> None:
        db.mongo["consents"].insert_many(_consent_rows(db, user_id, {purpose: granted}, utcnow()),
                                         session=db.session)
        if purpose == "personalised_suggestions" and not granted:
            db.search_history.update_many({"user_id": user_id}, {"$set": {"user_id": None}})
        if (purpose == settings.demand_key_consent_purpose and not granted and settings.demand_key_needs_consent):
            forget_demand_key(db, user_id)  # P9.7: the demand key needed this consent

    db.transaction(write)
    return consent_state(db, user_id)


def forget_demand_key(db: Database, user_id: int) -> int:
    """Remove the user's demand key (P9.7) from every search that holds it: the key is recomputed from the user id,
    as nothing stores which user a key belongs to. Nothing to do while NEARSHOP_DEMAND_KEY_SECRET is unset."""
    from app.services.search import demand_key_of  # search imports this module

    key = demand_key_of(user_id)
    if key is None:
        return 0
    return db.search_history.update_many({"demand_key": key}, {"$set": {"demand_key": None}})


# ---------------------------------------------------------------- nomination


def set_nominee(db: Database, user: User, name: str, phone: str | None, email: str | None) -> dict:
    """Name (or replace) the account's one nominee."""
    nominee = {"name": name, "phone": phone, "email": email.lower() if email else None, "added_at": utcnow()}
    db.users.update_one({"_id": user.id}, {"$set": {"nominee": nominee, "updated_at": utcnow()}})
    return nominee_out(nominee)


def remove_nominee(db: Database, user: User) -> None:
    db.users.update_one({"_id": user.id}, {"$unset": {"nominee": ""}, "$set": {"updated_at": utcnow()}})


def nominee_out(nominee: Any) -> dict | None:
    if not nominee:
        return None
    data = nominee if isinstance(nominee, dict) else nominee.model_dump()
    return {"name": data["name"], "phone": data.get("phone"), "email": data.get("email"),
            "added_at": iso(data.get("added_at"))}


# ---------------------------------------------------------------- export


def _clean(doc: dict, drop: Iterable[str] = ()) -> dict:
    """A stored document as export JSON: _id becomes id, dates become ISO strings."""
    def value(v: Any) -> Any:
        if isinstance(v, datetime):
            return iso(v)
        if isinstance(v, dict):
            return {k: value(x) for k, x in v.items()}
        if isinstance(v, list):
            return [value(x) for x in v]
        return v

    skip = set(drop)
    return {("id" if k == "_id" else k): value(v) for k, v in doc.items() if k not in skip}


# Moderation bookkeeping on a review and on the shop's reply (P3.6) that is the admins' and the shop's, not the
# customer's: who decided it and their internal note, how many people reported it, and which owner account
# wrote the reply. The export shows the review's and the reply's status instead.
REVIEW_INTERNAL = ("decided_by", "decision_note", "report_count", "triaged_at", "kept_held")
REPLY_INTERNAL = ("decided_by", "decision_note", "report_count", "triaged_at", "author_id")
# Which admin resolved a grievance (P3.8) is the team's record; the export shows the status and resolution.
GRIEVANCE_INTERNAL = ("resolved_by",)
# A WhatsApp outbox row (P4.3) in the export: what was sent and when, without the sender's bookkeeping.
OUTBOX_INTERNAL = ("user_id", "buttons", "claimed_at", "next_attempt_at", "cost_estimate")


def _review_out(doc: dict) -> dict:
    out = _clean(doc, REVIEW_INTERNAL)
    if isinstance(doc.get("reply"), dict):
        out["reply"] = _clean(doc["reply"], REPLY_INTERNAL)
    return out


def export_data(db: Database, user: User) -> dict:
    """Everything NearShop holds about the account, as one JSON document (plan section 8.5). Secrets
    (the password hash, session ids, second-factor material) are never included."""
    raw = db.users.raw.find_one({"_id": user.id}) or {}
    profile = {"id": user.id, "name": user.name, "email": user.email, "phone": user.phone, "role": user.role.value,
               "home_lat": user.home_lat, "home_lng": user.home_lng, "home_label": user.home_label,
               "created_at": iso(user.created_at), "last_login_at": iso(user.last_login_at),
               "signed_in_devices": [{"device": s.device, "created_at": iso(s.created_at),
                                      "last_seen_at": iso(s.last_seen_at)} for s in user.sessions]}
    newest_first = [("created_at", -1)]

    def find(name: str, flt: dict, sort: list = newest_first) -> list[dict]:
        return [_clean(d) for d in db.mongo[name].find(flt, sort=sort)]

    shops = [_clean(d, ("location",)) | {"lat": d["location"]["coordinates"][1], "lng": d["location"]["coordinates"][0]}
             for d in db.mongo["shops"].find({"owner_id": user.id})] if user.role == Role.OWNER else []
    return {
        "format": "nearshop-export-v1",
        "exported_at": iso(utcnow()),
        "profile": profile,
        "consents": find("consents", {"user_id": user.id}, [("at", 1), ("_id", 1)]),
        "consents_in_force": consent_state(db, user.id),
        "nominee": nominee_out(raw.get("nominee")),
        "shops": shops,
        "reservations": find("reservations", {"customer_id": user.id}),
        "orders": find("orders", {"customer_id": user.id}),
        "reviews": [_review_out(d) for d in db.mongo["reviews"].find({"customer_id": user.id}, sort=newest_first)],
        "review_reports": [_clean(d, ("reporter_id",)) for d in db.mongo["review_reports"].find(
            {"reporter_id": user.id}, sort=newest_first)],
        "notifications": find("notifications", {"user_id": user.id}),
        "whatsapp_messages": [_clean(d, OUTBOX_INTERNAL) for d in db.mongo["notification_outbox"].find(
            {"user_id": user.id}, sort=newest_first)],
        "grievances": [_clean(d, GRIEVANCE_INTERNAL) for d in db.mongo["grievances"].find(  # filed signed in
            {"user_id": user.id}, sort=newest_first)],
        "search_history": [_clean(d, ("user_id",)) for d in db.mongo["search_history"].find(
            {"user_id": user.id}, sort=newest_first)],
    }


# ---------------------------------------------------------------- deletion


class DeletionBlocked(Conflict):
    """409: open reservations or orders must be cancelled first. `open` lists them."""

    code = "deletion_blocked"

    def __init__(self, message: str, open_items: list[dict]):
        super().__init__(message)
        self.open = open_items


def _shop_ids(db: Database, user: User) -> list[int]:
    return [d["_id"] for d in db.shops.find_raw({"owner_id": user.id}, {"_id": 1})] if user.role == Role.OWNER else []


def _open_work(db: Database, user: User) -> tuple[list[Reservation], list[Order], list[Order]]:
    """(cancellable reservations, cancellable orders, in-flight orders) for the account: a customer's own, or
    for an owner those of their shops."""
    if user.role == Role.OWNER:
        who: dict = {"shop_id": {"$in": _shop_ids(db, user)}}
    else:
        who = {"customer_id": user.id}
    reservations = db.reservations.find({**who, "status": {"$in": list(OPEN_RESERVATIONS)}}, sort=[("_id", 1)])
    orders = db.orders.find({**who, "status": {"$in": [*CANCELLABLE_ORDERS, *IN_FLIGHT_ORDERS]}}, sort=[("_id", 1)])
    return (reservations, [o for o in orders if o.status in CANCELLABLE_ORDERS],
            [o for o in orders if o.status in IN_FLIGHT_ORDERS])


def _items(db: Database, reservations: list[Reservation], orders: list[Order]) -> list[dict]:
    shops = db.shops.by_ids([r.shop_id for r in reservations] + [o.shop_id for o in orders])
    products = db.products.by_ids(r.product_id for r in reservations)
    out = [{"kind": "reservation", "id": r.id, "code": r.code, "status": r.status.value,
            "shop": shops[r.shop_id].name if r.shop_id in shops else None,
            "summary": f"{r.quantity} x {products[r.product_id].name}" if r.product_id in products else None}
           for r in reservations]
    out += [{"kind": "order", "id": o.id, "code": o.code, "status": o.status.value,
             "shop": shops[o.shop_id].name if o.shop_id in shops else None,
             "summary": f"{sum(i.quantity for i in o.items)} items, Rs {o.total:.0f}"} for o in orders]
    return out


def deletion_plan(db: Database, user: User, now: datetime | None = None) -> dict:
    """What deleting this account would do now, shown before the user confirms.

    outcome: refused (admins), scheduled (already requested, or orders in flight), cancel_first (open
    reservations or orders the user may cancel; then says what follows), or immediate."""
    now = now or utcnow()
    raw = db.users.raw.find_one({"_id": user.id}, {"deletion_due_at": 1}) or {}
    base = {"role": user.role.value, "open": [], "in_flight": [], "then": None, "scheduled_for": None,
            "shops": [d["name"] for d in db.shops.find_raw({"owner_id": user.id}, {"name": 1})]
            if user.role == Role.OWNER else []}
    if user.role == Role.ADMIN:
        return {**base, "outcome": "refused",
                "message": "Admin accounts cannot delete themselves. Ask another admin to remove the account "
                           "with the operator command line."}
    reservations, cancellable, in_flight = _open_work(db, user)
    base.update(open=_items(db, reservations, cancellable), in_flight=_items(db, [], in_flight))
    if raw.get("deletion_due_at"):
        return {**base, "outcome": "scheduled", "scheduled_for": iso(raw["deletion_due_at"]),
                "message": "Your account is already scheduled for deletion."}
    after = "scheduled" if in_flight else "immediate"
    due = iso(now + DELETION_GRACE) if in_flight else None
    if base["open"]:
        return {**base, "outcome": "cancel_first", "then": after, "scheduled_for": due,
                "message": "Cancel these first. Cancelling releases the items back to the shop."}
    if in_flight:
        return {**base, "outcome": "scheduled", "scheduled_for": due,
                "message": "Some orders are already on their way. Your account will be deleted when they close, "
                           "at the latest on the date shown."}
    return {**base, "outcome": "immediate", "message": "Your account will be deleted now."}


def _cancel_open(db: Database, user: User, reservations: list[Reservation], orders: list[Order],
                 reason: str) -> int:
    """Cancel each open reservation and cancellable order as the user (a customer cancels; an owner rejects
    requests and cancels the rest), releasing held stock. One that has moved on meanwhile is skipped."""
    done = 0
    for r in reservations:
        action = "reject" if user.role == Role.OWNER and r.status == RS.REQUESTED else "cancel"
        try:
            reservation_service.transition(db, r.id, action, user, reason)
            done += 1
        except Exception as exc:  # it changed state in between; the plan is recomputed afterwards
            log.info("deletion: reservation %s not cancelled (%s)", r.id, type(exc).__name__)
    for o in orders:
        try:
            order_service.transition(db, o.id, "cancel", user, reason)
            done += 1
        except Exception as exc:
            log.info("deletion: order %s not cancelled (%s)", o.id, type(exc).__name__)
    return done


def request_deletion(db: Database, user: User, *, cancel_open: bool, now: datetime | None = None) -> dict:
    """Delete the account now, schedule it, or refuse (see the module docstring). Returns {status: deleted}
    or {status: scheduled, scheduled_for}. Raises Forbidden for admins and DeletionBlocked (409) while
    cancellable work is open and cancel_open is false."""
    now = now or utcnow()
    plan = deletion_plan(db, user, now)
    if plan["outcome"] == "refused":
        raise Forbidden(plan["message"], code="admin_self_delete")
    if plan["open"]:
        if not cancel_open:
            raise DeletionBlocked("Cancel your open reservations and orders first, or choose cancel all and delete.",
                                  plan["open"])
        reservations, cancellable, _ = _open_work(db, user)
        reason = "The shop has closed" if user.role == Role.OWNER else "The customer deleted their account"
        _cancel_open(db, user, reservations, cancellable, reason)
        plan = deletion_plan(db, user, now)
        if plan["open"]:
            raise DeletionBlocked("Some reservations or orders changed while we were cancelling them. "
                                  "Check them and try again.", plan["open"])
    if plan["in_flight"]:
        raw = db.users.raw.find_one({"_id": user.id}, {"deletion_due_at": 1}) or {}
        due = raw.get("deletion_due_at") or now + DELETION_GRACE
        db.users.update_one({"_id": user.id, "deletion_due_at": None},
                            {"$set": {"deletion_requested_at": now, "deletion_due_at": due}})
        log.info("deletion of user %s scheduled for %s", user.id, due.isoformat())
        return {"status": "scheduled", "scheduled_for": iso(due), "in_flight": plan["in_flight"]}
    anonymise(db, user.id, now)
    return {"status": "deleted"}


def anonymise(db: Database, user_id: int, now: datetime | None = None) -> None:
    """Remove the account's personal data in one transaction (plan section 8.5): the profile becomes
    "Deleted user" with no email, phone, password, home, nominee or sessions; optional consents are
    withdrawn by new rows; notifications, their WhatsApp outbox rows and search history are deleted; reviews keep their rating and lose
    their comment; review reports the user filed keep their reason and lose their note; reservations and orders stay for the shop without phone, address or note; sales lose the
    customer id. An owner's shops are hidden and their listings deactivated first; a staff member's memberships, or
    the staff of an owner's shops, are removed (P9.13). Before anything changes, every UPI
    Autopay mandate of an owner's shops is cancelled at the payment gateway (P9.6, gateway.end_for_owner), so no more
    money is taken from someone who has left; while the gateway cannot be reached this raises (GatewayError or
    AutopayNotConfigured) and nothing is deleted."""
    now = now or utcnow()
    found = db.users.get(user_id)
    if found is not None and found.deleted_at is None and found.role == Role.OWNER:
        from app.services import gateway  # imported here: gateway imports billing and plans

        gateway.end_for_owner(db, user_id, now=now)

    from app.services import staff  # imported here: staff -> plans -> app.core.deps

    def run() -> None:
        user = db.users.get(user_id)
        if user is None or user.deleted_at is not None:
            return
        # P9.13: a staff member's memberships end; an owner's shops lose their staff, signed out.
        staff.end_for_deleted_account(db, user, now)
        if user.role == Role.OWNER:
            shop_ids = _shop_ids(db, user)
            db.shops.update_many({"_id": {"$in": shop_ids}}, {"$set": {"is_active": False, "updated_at": now}})
            db.products.update_many({"shop_id": {"$in": shop_ids}}, {"$set": {"is_active": False, "updated_at": now}})
        db.users.update_one({"_id": user_id}, {
            "$set": {"name": DELETED_NAME, "email": deleted_email(user_id), "phone": None, "password_hash": "",
                     "is_active": False, "home_lat": None, "home_lng": None, "home_label": None, "sessions": [],
                     "deleted_at": now, "updated_at": now},
            "$inc": {"token_version": 1},
            "$unset": {"nominee": "", "deletion_requested_at": "", "deletion_due_at": "", "deletion_flagged_at": ""}})
        state = consent_state(db, user_id)
        withdrawn = {p: False for p in OPTIONAL_PURPOSES if state.get(p)}
        if withdrawn:
            db.mongo["consents"].insert_many(_consent_rows(db, user_id, withdrawn, now), session=db.session)
        db.notifications.delete_many({"user_id": user_id})
        # Their WhatsApp outbox rows (P4.3): a queued one still holds the message text.
        db.mongo["notification_outbox"].delete_many({"user_id": user_id}, session=db.session)
        db.search_history.delete_many({"user_id": user_id})
        forget_demand_key(db, user_id)  # P9.7: searches kept without the user id lose the demand key too
        db.reviews.update_many({"customer_id": user_id}, {"$set": {"comment": None}})
        # Reports the user filed (P3.6) stay, so a review's distinct-reporter count holds, without their note.
        db.mongo["review_reports"].update_many({"reporter_id": user_id}, {"$set": {"note": None}},
                                               session=db.session)
        # Grievances (P3.8) stay until resolved, answered through the contact given with them, but no longer
        # point at the account.
        db.mongo["grievances"].update_many({"user_id": user_id}, {"$set": {"user_id": None}}, session=db.session)
        db.reservations.update_many({"customer_id": user_id}, {"$set": {"note": None}})
        db.orders.update_many({"customer_id": user_id}, clear_order_contact(now))
        db.sales_history.update_many({"customer_id": user_id}, {"$set": {"customer_id": None}})

    db.transaction(run)
    log.info("user %s deleted and anonymised", user_id)


def clear_order_contact(now: datetime) -> list[dict]:
    """The update that removes an order's contact details: phone, address and note, with the delivery point
    rounded to 2 decimals (about 1.1 km) so its distance still reads right. contact_cleared_at marks it done."""
    return [{"$set": {"contact_phone": "", "delivery_address": "", "note": None,
                      "delivery_lat": {"$round": ["$delivery_lat", 2]},
                      "delivery_lng": {"$round": ["$delivery_lng", 2]}, "contact_cleared_at": now}}]


def process_scheduled_deletions(db: Database, now: datetime | None = None) -> dict[str, int]:
    """The process_deletions job: run each scheduled deletion whose in-flight orders have all closed
    (cancelling anything cancellable the user opened since), and flag for the admins, once, each one still
    waiting at its date."""
    now = now or utcnow()
    deleted = flagged = 0
    for doc in db.users.find_raw({"deletion_due_at": {"$type": "date"}},
                                 {"_id": 1, "deletion_due_at": 1, "deletion_flagged_at": 1}):
        user = db.users.get(doc["_id"])
        if user is None:
            continue
        reservations, cancellable, in_flight = _open_work(db, user)
        if not in_flight:
            reason = "The shop has closed" if user.role == Role.OWNER else "The customer deleted their account"
            _cancel_open(db, user, reservations, cancellable, reason)
            if not any(_open_work(db, user)):
                try:
                    anonymise(db, user.id, now)
                except AppError as exc:  # an Autopay mandate the gateway could not cancel yet: retried next tick
                    log.warning("deletion of user %s waits: %s", user.id, exc.message)
                else:
                    deleted += 1
                    continue
        if doc["deletion_due_at"] <= now and not doc.get("deletion_flagged_at"):
            db.users.update_one({"_id": user.id}, {"$set": {"deletion_flagged_at": now}})
            for admin in db.users.find_raw({"role": Role.ADMIN.value, "is_active": True}, {"_id": 1}):
                notify(db, admin["_id"], "deletion_overdue", f"Account deletion for user {user.id} is waiting",
                       "Its orders have not closed 7 days after the request. Resolve the orders, then the "
                       "deletion runs on the next tick.", "/admin")
            log.warning("deletion of user %s is overdue: orders still open", user.id)
            flagged += 1
    return {"deleted": deleted, "flagged": flagged}


# ---------------------------------------------------------------- retention


def _grievance_holds(db: Database) -> tuple[list[int], list[str]]:
    """Order ids and codes an open grievance refers to (P3.8 stores order_id or order_code)."""
    coll = db.mongo["grievances"]
    flt = {"status": {"$nin": list(GRIEVANCE_CLOSED)}}
    ids = [i for i in coll.distinct("order_id", flt) if isinstance(i, int)]
    codes = [c for c in coll.distinct("order_code", flt) if isinstance(c, str)]
    return ids, codes


def run_retention(db: Database, now: datetime | None = None) -> dict[str, int]:
    """The nightly retention rules (plan section 8.4) as of `now`:

      * search_history rows older than 30 days lose their user_id and their demand key (P9.7);
      * orders closed (delivered, cancelled or returned) more than 180 days ago lose their contact phone,
        address and note, unless an open grievance refers to them.

    The TTL indexes do the rest: search_history rows go after 180 days, notifications after 90."""
    now = now or utcnow()
    unlinked = db.search_history.update_many(
        {"user_id": {"$type": "number"}, "created_at": {"$lt": now - SEARCH_USER_ID_KEEP}}, {"$set": {"user_id": None}})
    keys_removed = db.search_history.update_many(
        {"demand_key": {"$type": "string"}, "created_at": {"$lt": now - DEMAND_KEY_KEEP}}, {"$set": {"demand_key": None}})
    cutoff = now - ORDER_CONTACT_KEEP
    held_ids, held_codes = _grievance_holds(db)
    flt = {"status": {"$in": list(CLOSED_ORDERS)}, "contact_cleared_at": {"$exists": False},
           "$or": [{"closed_at": {"$lte": cutoff}}, {"closed_at": None, "delivered_at": {"$lte": cutoff}}],
           "_id": {"$nin": held_ids}, "code": {"$nin": held_codes}}
    cleared = db.orders.update_many(flt, clear_order_contact(now))
    held = db.orders.count({"status": {"$in": list(CLOSED_ORDERS)}, "contact_cleared_at": {"$exists": False},
                            "$or": [{"_id": {"$in": held_ids}}, {"code": {"$in": held_codes}}]}) \
        if held_ids or held_codes else 0
    if unlinked or cleared or keys_removed:
        log.info("retention: %d searches unlinked, %d demand keys removed, %d orders' contact details cleared",
                 unlinked, keys_removed, cleared)
    return {"searches_unlinked": unlinked, "demand_keys_removed": keys_removed, "orders_cleared": cleared,
            "orders_held_by_grievance": held}
