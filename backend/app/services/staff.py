"""Staff logins (P9.13; plan section 8.18).

A shop on a paid plan gives counter staff their own sign-in for daily work: the reservation and order queues and their
actions, listings and stock, the catalogue and imports (app/core/deps.py WorkShop). The owner alone keeps money,
identity and data sharing: shop settings and the UPI ID, KYC, the plan and billing, staff, insights, reviews and
account deletion (OwnerShop). Every status event a staff member causes records actor_role staff and their user id, so
the owner's timelines name them.

  * Invitation: the owner gives the staff member's name and Indian mobile number, and states that the invitee agreed
    to be messaged (recorded as invitee_agreed_at). The invitee gets nearshop_staff_invitation on WhatsApp
    (docs/whatsapp-templates.md) when WhatsApp is on here, and signs in with a code sent to that phone: the first
    sign-in creates the staff account and makes the invitation active. In v1 a phone already registered to any
    NearShop account is refused (staff use their own number); the one exception is the account of a former staff
    member, removed from every shop, which a new invitation reuses.
  * The limit: the plan's logins entitlement counts the owner, so a shop has logins - 1 staff seats. Invited and
    active staff take a seat; an invitation beyond them is refused with plan_limit.
  * A lapse or a plan with fewer logins: reconcile() keeps the oldest staff (by activation) active and suspends the
    newest beyond the seats, ending their sessions; on renewal it reactivates them, oldest first. It runs whenever a
    staff member makes a request (deps.get_work_shop, before access is decided), on the owner's staff screen and on
    every invitation, removal and activation, so a suspended member never reaches a route.
  * Removal: at once, for good; the member's sessions end (P2.4 token version).

The rows live in shop_staff (database/schema.py), whose phone field app/services/leads.py reads to keep the staff's
own requests out of a shop's customer requests.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import Field
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Collection, Database, utcnow
from app.core.errors import AppError, Conflict, NotFound
from app.core.security import revoke_all_update
from app.models import Shop, User
from app.models.base import Document, Timestamps
from app.models.enums import Role, StaffStatus
from app.schemas.serializers import iso
from app.services import leads, plans, whatsapp
from app.services.messaging import masked

log = logging.getLogger(__name__)

STAFF = leads.STAFF  # the shop_staff collection
TEMPLATE = "nearshop_staff_invitation"
OPEN = (StaffStatus.INVITED.value, StaffStatus.ACTIVE.value, StaffStatus.SUSPENDED.value)
SEATED = (StaffStatus.ACTIVE.value, StaffStatus.SUSPENDED.value)  # rows with an account, active or not


class ShopStaff(Document, Timestamps):
    """Collection shop_staff: one invitation of a phone to a shop, and the staff membership it becomes."""

    shop_id: int
    user_id: int | None = None
    phone: str
    name: str
    status: StaffStatus = StaffStatus.INVITED
    invited_by: int
    invited_at: datetime = Field(default_factory=utcnow)
    invitee_agreed_at: datetime = Field(default_factory=utcnow)  # the owner's statement (docs/whatsapp-templates.md)
    activated_at: datetime | None = None
    suspended_at: datetime | None = None
    removed_at: datetime | None = None
    removed_by: int | None = None
    invitation: str | None = None  # sent, failed or whatsapp_off


def rows(db: Database) -> Collection[ShopStaff]:
    return Collection(db, STAFF, ShopStaff)


def plan_limit(logins: int) -> Conflict:
    return Conflict(f"Your plan allows {logins} login{'s' if logins != 1 else ''}, the owner's included. Remove a "
                    "staff member or choose a plan with more logins.", code="plan_limit")


def phone_registered() -> Conflict:
    return Conflict("This number already has a NearShop account. Staff sign in with their own number, one that is not "
                    "yet on NearShop.", code="phone_registered")


def revoke_sessions(db: Database, user_id: int | None) -> None:
    """End every session of the staff member's account (P2.4): their next request is signed out."""
    if user_id is not None:
        db.users.update_one({"_id": user_id}, revoke_all_update())


# ---------------------------------------------------------------- the limit


def logins(db: Database, shop_id: int, at: datetime | None = None) -> int:
    """The plan's logins (P9.4 entitlement logins, the owner included) for the shop at `at`."""
    return int(plans.entitled(db, shop_id, "logins", at))


def reconcile(db: Database, shop_id: int, now: datetime | None = None) -> dict[str, list[int]]:
    """Fit the shop's staff to its plan: of the active and suspended members, the oldest (by activation) up to the
    plan's seats are active and the rest suspended, with their sessions ended. Returns the ids changed."""
    now = now or utcnow()
    seats = max(logins(db, shop_id, now) - 1, 0)
    members = rows(db).find({"shop_id": shop_id, "status": {"$in": list(SEATED)}},
                            sort=[("activated_at", 1), ("_id", 1)])
    suspended: list[int] = []
    reactivated: list[int] = []
    for n, row in enumerate(members):
        if n < seats and row.status == StaffStatus.SUSPENDED:
            if rows(db).update_one({"_id": row.id, "status": StaffStatus.SUSPENDED.value},
                                   {"$set": {"status": StaffStatus.ACTIVE.value, "suspended_at": None,
                                             "updated_at": now}}):
                reactivated.append(row.id)
        elif n >= seats and row.status == StaffStatus.ACTIVE:
            if rows(db).update_one({"_id": row.id, "status": StaffStatus.ACTIVE.value},
                                   {"$set": {"status": StaffStatus.SUSPENDED.value, "suspended_at": now,
                                             "updated_at": now}}):
                revoke_sessions(db, row.user_id)
                suspended.append(row.id)
    if suspended or reactivated:
        log.info("staff of shop %s fitted to its plan: %d suspended, %d reactivated", shop_id, len(suspended),
                 len(reactivated))
    return {"suspended": suspended, "reactivated": reactivated}


# ---------------------------------------------------------------- the staff member's side


def membership(db: Database, user: User) -> ShopStaff | None:
    """The staff member's active membership after fitting their shop to its plan, or None (suspended, removed or
    never active)."""
    if user.role != Role.STAFF:
        return None
    row = rows(db).find_one({"user_id": user.id, "status": {"$in": list(SEATED)}})
    if row is None:
        return None
    reconcile(db, row.shop_id)
    row = rows(db).get(row.id)
    return row if row is not None and row.status == StaffStatus.ACTIVE else None


def shop_for(db: Database, user: User) -> Shop | None:
    """The shop an active staff member works for, or None."""
    row = membership(db, user)
    return db.shops.get(row.shop_id) if row is not None else None


def is_member(db: Database, user_id: int, shop_id: int) -> bool:
    """Whether the user is an active staff member of the shop (read in db's transaction)."""
    return rows(db).exists({"user_id": user_id, "shop_id": shop_id, "status": StaffStatus.ACTIVE.value})


def summary(db: Database, user: User) -> dict[str, Any] | None:
    """What /api/auth/me says about a staff member's shop: its id, slug and name, and the membership status."""
    row = rows(db).find_one({"user_id": user.id, "status": {"$in": list(OPEN)}}, sort=[("_id", -1)])
    if row is None:
        return None
    shop = db.shops.get(row.shop_id)
    if shop is None:
        return None
    return {"id": shop.id, "slug": shop.slug, "name": shop.name, "staff_status": row.status.value}


def pending_invitation(db: Database, phone: str) -> ShopStaff | None:
    return rows(db).find_one({"phone": phone, "status": StaffStatus.INVITED.value})


def activate(db: Database, user: User, now: datetime | None = None) -> ShopStaff | None:
    """On a staff account's sign-in: the invitation of its phone, if one is waiting, becomes active (suspended at once
    when the plan has no seat left, as reconcile decides)."""
    if user.role != Role.STAFF or not user.phone:
        return None
    now = now or utcnow()
    row = pending_invitation(db, user.phone)
    if row is None:
        return None
    if not rows(db).update_one({"_id": row.id, "status": StaffStatus.INVITED.value},
                               {"$set": {"status": StaffStatus.ACTIVE.value, "user_id": user.id,
                                         "activated_at": now, "updated_at": now}}):
        return None
    reconcile(db, row.shop_id, now)
    return rows(db).get(row.id)


def new_account(db: Database, phone: str, now: datetime) -> User | None:
    """The staff account for an invited phone at its first sign-in (call in a transaction), or None when no
    invitation waits for the phone. The account has no password: staff sign in by phone code only."""
    row = pending_invitation(db, phone)
    if row is None:
        return None
    return db.users.insert(User(name=row.name, phone=phone, phone_verified_at=now, password_hash="", role=Role.STAFF))


# ---------------------------------------------------------------- the owner's side


def staff_out(row: ShopStaff) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "phone": row.phone, "status": row.status.value,
            "invited_at": iso(row.invited_at), "activated_at": iso(row.activated_at),
            "suspended_at": iso(row.suspended_at), "invitation": row.invitation}


def overview(db: Database, shop: Shop) -> dict[str, Any]:
    """The owner's staff screen: the members (removed ones left out), the plan's logins and how many are used."""
    reconcile(db, shop.id)
    plan = plans.effective_plan(db, shop.id)
    members = rows(db).find({"shop_id": shop.id, "status": {"$in": list(OPEN)}}, sort=[("invited_at", 1), ("_id", 1)])
    used = 1 + sum(1 for r in members if r.status in (StaffStatus.INVITED, StaffStatus.ACTIVE))
    limit = int(plan["entitlements"]["logins"])
    return {"staff": [staff_out(r) for r in members],
            "logins": {"limit": limit, "used": used, "available": max(limit - used, 0)},
            "plan": {"code": plan["code"], "name": plan["name"], "status": plan["status"],
                     "ends_at": iso(plan["ends_at"])}}


def _first_name(name: str) -> str:
    return (name.split() or [name])[0]


def send_invitation(db: Database, row: ShopStaff, owner: User, shop: Shop) -> str:
    """nearshop_staff_invitation to the invited phone, straight through the WhatsApp client: the invitee has no
    account, so no outbox row (which belongs to a user) can carry it. Off (whatsapp_off) unless WhatsApp is on here;
    in development and tests the fake client keeps it in memory."""
    client = whatsapp.get_client()
    if client is None:
        outcome = "whatsapp_off"
    else:
        origin = (settings.cors_origins[0] if settings.cors_origins else "").rstrip("/")
        params = [_first_name(owner.name), shop.name, f"{origin}/login"]
        components = [{"type": "body", "parameters": [{"type": "text", "text": p[:200]} for p in params]}]
        try:
            delivery = client.send_template(row.phone, TEMPLATE, whatsapp.template_language(owner.language),
                                            components)
            outcome = "sent" if delivery.ok else "failed"
        except Exception as exc:  # a client must not raise; the invitation stands either way
            log.warning("staff invitation to %s failed: %s", masked(row.phone), type(exc).__name__)
            outcome = "failed"
    rows(db).update_one({"_id": row.id}, {"$set": {"invitation": outcome, "updated_at": utcnow()}})
    row.invitation = outcome
    return outcome


def invite(db: Database, shop: Shop, owner: User, *, phone: str, name: str, agreed: bool) -> ShopStaff:
    """Invite a phone to the shop's staff (module docstring). Refused: without the owner's statement that the invitee
    agreed to be messaged, for a phone with a NearShop account (other than a former staff member's) or already on a
    shop's staff, and beyond the plan's logins (plan_limit)."""
    if not agreed:
        raise AppError("Confirm that this person agreed to get the invitation on WhatsApp", code="agreement_required",
                       status_code=422)
    holder = db.users.find_one({"phone": phone})
    if holder is not None and holder.role != Role.STAFF:
        raise phone_registered()
    if rows(db).exists({"phone": phone, "status": {"$in": list(OPEN)}}):
        raise Conflict("This number is already on a shop's staff or invited.", code="already_staff")
    reconcile(db, shop.id)
    limit = logins(db, shop.id)
    taken = rows(db).count({"shop_id": shop.id, "status": {"$in": [StaffStatus.INVITED.value,
                                                                   StaffStatus.ACTIVE.value]}})
    if 1 + taken >= limit:
        raise plan_limit(limit)
    now = utcnow()
    row = ShopStaff(shop_id=shop.id, phone=phone, name=name, invited_by=owner.id, invited_at=now,
                    invitee_agreed_at=now)
    try:
        row = db.transaction(lambda: rows(db).insert(row))
    except DuplicateKeyError:
        raise Conflict("This number is already on a shop's staff or invited.", code="already_staff") from None
    send_invitation(db, row, owner, shop)
    return row


def remove(db: Database, shop: Shop, owner: User, staff_id: int) -> ShopStaff:
    """Remove a staff member (or withdraw an invitation) at once: every session of theirs ends, and a suspended
    member may take the freed seat."""
    row = rows(db).get(staff_id)
    if row is None or row.shop_id != shop.id or row.status == StaffStatus.REMOVED:
        raise NotFound("Staff member not found")
    now = utcnow()
    if not rows(db).update_one({"_id": row.id, "status": row.status.value},
                               {"$set": {"status": StaffStatus.REMOVED.value, "removed_at": now,
                                         "removed_by": owner.id, "updated_at": now}}):
        raise Conflict("This staff member changed meanwhile. Reload and try again.", code="staff_changed")
    revoke_sessions(db, row.user_id)
    reconcile(db, shop.id, now)
    return rows(db).get(row.id)


def names(db: Database, user_ids: set[int]) -> dict[int, str]:
    """Staff members' names by user id, for the owner's timelines."""
    return {u.id: u.name for u in db.users.by_ids(user_ids).values()} if user_ids else {}
