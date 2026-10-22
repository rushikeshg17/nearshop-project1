import re
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, File, Query, Response, UploadFile
from pydantic import BaseModel, Field

from app.api.routes import grievances as grievance_routes
from app.core import cache_version
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.deps import DB, AdminUser
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.models import AnomalyStatus, Role, Shop, User
from app.models.billing import Actor, SubscriptionStatus
from app.models.core import VerificationStatus, evidence_keys, months_after
from app.schemas.requests import (
    AdminPaymentRequestIn,
    AdminShopUpdateIn,
    AnomalyReviewIn,
    CataloguePublishIn,
    CreditReturnIn,
    ManualMatchIn,
    PilotLedgerIn,
    RefundIn,
    SubscriptionCancelIn,
    SubscriptionExtendIn,
    SubscriptionGrantIn,
)
from app.schemas.serializers import iso, listing, shop_brief
from app.services import analytics, billing, plans
from app.services import reviews as review_service
from app.services import synonyms as synonym_service
from app.services.loaders import with_categories, with_category
from app.services.media import (
    PrivateStorageUnavailable,
    check_signed_link,
    delete_private,
    read_private,
    signed_link,
)
from app.services.notifications import notify

router = APIRouter(prefix="/admin", tags=["admin"])


def _contains(q: str) -> dict:
    return {"$regex": re.escape(q.strip()), "$options": "i"}


def mask_email(email: str) -> str:
    """An email as admin lists show it: the first letter and the domain (r***@example.com)."""
    local, at, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if at else "***"


class RevealEmailIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    reason: str = Field(min_length=10, max_length=500)


class SynonymIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    term: str = Field(min_length=1, max_length=80)
    expansions: list[str] = Field(min_length=1, max_length=synonym_service.MAX_EXPANSIONS)
    languages: list[str] = Field(default_factory=list, max_length=synonym_service.MAX_LANGUAGES)


class SynonymUpdateIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    term: str | None = Field(None, min_length=1, max_length=80)
    expansions: list[str] | None = Field(None, min_length=1, max_length=synonym_service.MAX_EXPANSIONS)
    languages: list[str] | None = Field(None, max_length=synonym_service.MAX_LANGUAGES)
    active: bool | None = None


class SynonymPreviewIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    term: str = Field(min_length=1, max_length=80)
    expansions: list[str] = Field(min_length=1, max_length=synonym_service.MAX_EXPANSIONS)
    query: str | None = Field(None, max_length=80)
    entry_id: int | None = None


@router.get("/overview")
def overview(_: AdminUser, db: DB):
    return analytics.admin_overview(db)


@router.get("/storage")
def storage(_: AdminUser, db: DB):
    """The storage budget: the latest daily snapshot against NEARSHOP_STORAGE_LIMIT_MB, the 30-day growth
    per day, the projected date of reaching 85%, and the five largest collections."""
    return analytics.storage_report(db.mongo)


@router.get("/freshness")
def freshness(_: AdminUser, db: DB):
    """Stock freshness per live shop (median listing age, share confirmed within 48 hours), the shops beyond 7 days
    and the median listing age across verified live shops in the launch city: check 10.3's metric (P7.4)."""
    return analytics.freshness_report(db)


@router.get("/anomalies")
def anomalies(_: AdminUser, db: DB, status: Literal["open", "reviewed", "dismissed"] = "open"):
    rows = db.price_anomalies.find({"status": AnomalyStatus(status)}, sort=[("score", -1)])
    products, shops = db.gather(
        lambda: {p.id: p for p in with_category(db, db.products.by_ids(a.product_id for a in rows).values())},
        lambda: db.shops.by_ids(a.shop_id for a in rows),
    )
    return [
        {"id": a.id, "price": a.price, "reference_price": a.reference_price, "deviation_pct": a.deviation_pct,
         "score": a.score, "direction": a.direction, "peer_count": a.peer_count, "status": a.status.value,
         "review_note": a.review_note, "created_at": iso(a.created_at), "reviewed_at": iso(a.reviewed_at),
         "product": listing(products[a.product_id]),
         "shop": {"id": s.id, "name": s.name, "slug": s.slug, "locality": s.locality}}
        for a in rows
        if a.product_id in products and (s := shops.get(a.shop_id))
    ]


@router.post("/anomalies/{anomaly_id}")
def review_anomaly(anomaly_id: int, body: AnomalyReviewIn, admin: AdminUser, db: DB):
    a = db.price_anomalies.get(anomaly_id)
    if a is None:
        raise NotFound("Flag not found")
    db.price_anomalies.set(a, status=AnomalyStatus(body.status), review_note=body.note, reviewed_by=admin.id,
                           reviewed_at=utcnow())
    return {"ok": True}


# ---------------------------------------------------------------- review moderation (P3.6, plan section 8.2)

class ReviewTargetIn(BaseModel):
    model_config = {"extra": "forbid"}

    target: review_service.Target = "review"


class ReviewDecisionIn(ReviewTargetIn):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    decision: review_service.Decision
    note: str | None = Field(default=None, max_length=500)


def _queue_item(item: dict, shops: dict[int, Shop]) -> dict:
    s = shops.get(item["shop_id"])
    return {**item, "entered_at": iso(item["entered_at"]), "triaged_at": iso(item["triaged_at"]),
            "created_at": iso(item["created_at"]),
            "shop": {"id": s.id, "name": s.name, "slug": s.slug} if s else None}


@router.get("/reviews/queue")
def review_queue(_: AdminUser, db: DB):
    """Reported reviews and replies, and reviews held out by the self-dealing rule, oldest first, with their age,
    reports and flags, the first QUEUE_LIMIT of them; total counts everything waiting. Each is decided with keep,
    hide or remove."""
    items, total = review_service.queue(db)
    shops = db.shops.by_ids({i["shop_id"] for i in items})
    return {"items": [_queue_item(i, shops) for i in items], "total": total, "triage_hours": review_service.TRIAGE_HOURS,
            "decide_hours": review_service.DECIDE_HOURS}


def _moderation_json(review_id: int, db: Database, target: str) -> dict:
    r = db.reviews.get(review_id)
    thing = r.reply if target == review_service.REPLY and r and r.reply else r
    return {"review_id": review_id, "target": target, "status": thing.status, "decision": thing.decision,
            "triaged_at": iso(thing.triaged_at), "decided_at": iso(thing.decided_at)}


@router.post("/reviews/{review_id}/triage")
def triage_review(review_id: int, body: ReviewTargetIn, _: AdminUser, db: DB):
    """An admin has picked the item up (the 24-hour triage target)."""
    review_service.triage(db, review_id, body.target)
    return _moderation_json(review_id, db, body.target)


@router.post("/reviews/{review_id}/decision")
def decide_review(review_id: int, body: ReviewDecisionIn, admin: AdminUser, db: DB):
    """keep, hide or remove a review or its reply (the 72-hour decision target). The shop's rating is recomputed."""
    review_service.decide(db, admin, review_id, body.target, body.decision, body.note)
    return _moderation_json(review_id, db, body.target)


@router.get("/reviews/sla")
def review_sla(_: AdminUser, db: DB, month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """For the items that entered the queue in one month (India time; this month by default): the share triaged
    within 24 hours and the share decided within 72 hours. Items still inside a deadline are pending, not misses."""
    if month:
        year, mon = int(month[:4]), int(month[5:])
    else:
        local = utcnow() + IST
        year, mon = local.year, local.month
    start = datetime(year, mon, 1) - IST
    end = datetime(year + mon // 12, mon % 12 + 1, 1) - IST
    return {"month": f"{year:04d}-{mon:02d}", **review_service.sla(db, start, end)}


# ---------------------------------------------------------------- grievances (P3.8)

GRIEVANCE_QUEUE_LIMIT = 200


class GrievanceStatusIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    status: Literal["in_progress", "resolved"]
    # The answer the complainant reads with their ticket: required to resolve.
    resolution: str | None = Field(default=None, min_length=10, max_length=grievance_routes.DESCRIPTION_MAX)


@router.get("/grievances")
def grievance_queue(_: AdminUser, db: DB, state: Literal["unresolved", "resolved", "all"] = "unresolved"):
    """Grievances, oldest first (resolved ones newest first), each with its resolution due date (30 days after
    receipt), the days it has been open and needs_attention once it is unresolved after 25 days."""
    coll = db.mongo[grievance_routes.COLLECTION]
    flt = {} if state == "all" else {"status": grievance_routes.RESOLVED} if state == "resolved" else {
        "status": {"$ne": grievance_routes.RESOLVED}}
    order = -1 if state == "resolved" else 1
    now = utcnow()
    rows = list(coll.find(flt, sort=[("created_at", order), ("_id", order)], limit=GRIEVANCE_QUEUE_LIMIT))
    return {"items": [grievance_routes.admin_json(g, now) for g in rows], "total": coll.count_documents(flt),
            "resolve_days": grievance_routes.RESOLVE_DAYS, "warn_days": grievance_routes.WARN_DAYS}


@router.post("/grievances/{grievance_id}/status")
def set_grievance_status(grievance_id: int, body: GrievanceStatusIn, admin: AdminUser, db: DB):
    """Move a grievance to in_progress, or resolve it with the answer the complainant will read. A resolved
    grievance is final. A signed-in complainant is told in the app."""
    coll = db.mongo[grievance_routes.COLLECTION]
    g = coll.find_one({"_id": grievance_id})
    if g is None:
        raise NotFound("Grievance not found")
    if g["status"] == grievance_routes.RESOLVED:
        raise Conflict("This grievance is already resolved", code="grievance_resolved")
    now = utcnow()
    update: dict = {"status": body.status, "updated_at": now}
    if body.status == "resolved":
        if not body.resolution:
            raise AppError("Write the resolution the complainant will read (10 characters or more)",
                           code="resolution_required", status_code=422)
        update |= {"resolved_at": now, "resolved_by": admin.id, "resolution": body.resolution}
    elif g["status"] == "in_progress":
        raise Conflict("This grievance is already in progress", code="grievance_in_progress")
    else:
        update |= {"in_progress_at": now}
    # Only from the status read above, so two admins acting at once cannot both change it.
    if not coll.update_one({"_id": grievance_id, "status": g["status"]}, {"$set": update}).modified_count:
        raise Conflict("This grievance was changed by someone else. Reload and try again.", code="grievance_changed")
    g |= update
    if g.get("user_id"):
        title = (f"Grievance {g['ticket']} resolved" if body.status == "resolved"
                 else f"Grievance {g['ticket']} is being worked on")
        notify(db, g["user_id"], "grievance", title, link=f"/grievance?ticket={g['ticket']}")
    return grievance_routes.admin_json(g, now)


@router.get("/shops")
def shops(_: AdminUser, db: DB, q: str | None = Query(None, max_length=80)):
    query = {"$or": [{"name": _contains(q)}, {"locality": _contains(q)}]} if q else {}
    rows, counts = db.gather(
        lambda: with_categories(db, db.shops.find(query, sort=[("name", 1)])),
        lambda: {r["_id"]: r["n"] for r in db.products.aggregate([
            {"$match": {"is_active": True}}, {"$group": {"_id": "$shop_id", "n": {"$sum": 1}}}])},
    )
    owners = db.users.by_ids(s.owner_id for s in rows)
    return [{**shop_brief(s), "is_active": s.is_active, "verification_status": s.verification_status.value,
             "verified_until": iso(s.verified_until), "visible_to_customers": s.is_live,
             "owner": owners[s.owner_id].name,
             "owner_email": mask_email(owners[s.owner_id].email), "listings": counts.get(s.id, 0), "created_at": iso(s.created_at)}
            for s in rows if s.owner_id in owners]


@router.patch("/shops/{shop_id}")
def update_shop(shop_id: int, body: AdminShopUpdateIn, admin: AdminUser, db: DB):
    changes = body.model_dump(exclude_unset=True)
    # is_verified follows verification_status (P3.3): the old switch goes through the same rules as the
    # verification endpoints below, so it can never verify a shop without evidence and a phone check.
    verified = changes.pop("is_verified", None)

    def apply(s: Shop) -> list[str]:
        stale: list[str] = []
        if verified is True and not s.is_verified:
            stale = _approve(db, s, admin)
        elif verified is False and s.verification_status == VerificationStatus.VERIFIED:
            stale = _decide(db, s, admin, VerificationStatus.SUSPENDED, "Verification removed by an admin")
        if changes:
            db.shops.set(s, **changes)
        return stale

    _decided(db, shop_id, apply)
    return {"ok": True}


# ---------------------------------------------------------------- shop verification (P3.3, plan section 8.1)

IST = timedelta(hours=5, minutes=30)
# Waiting for a decision: new evidence (submitted), or a verified shop's renewal (submitted after its decision).
QUEUE = {"$or": [{"verification_status": VerificationStatus.SUBMITTED.value},
                 {"verification_status": VerificationStatus.VERIFIED.value,
                  "$expr": {"$gt": ["$submitted_at", "$decided_at"]}}]}


class VerificationDecisionIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    # At least 10 characters, as every admin_audit reason (database/schema.py): the owner reads it too.
    reason: str = Field(min_length=10, max_length=500)


def _shop(db: Database, shop_id: int) -> Shop:
    s = db.shops.get(shop_id)
    if s is None:
        raise NotFound("Shop not found")
    return s


def working_days_between(start: datetime, end: datetime) -> int:
    """Working days (Monday to Friday, India time) after start's date up to and including end's date: 0 on the
    day of submission, 1 on the next working day. The plan's target is a decision within 2."""
    day, last = (start + IST).date(), (end + IST).date()
    count = 0
    while day < last:
        day += timedelta(days=1)
        count += day.weekday() < 5
    return count


def _link(key: str | None) -> str | None:
    try:
        return signed_link(key)
    except PrivateStorageUnavailable:
        return None


def _audit(db: Database, admin: User, action: str, shop: Shop, reason: str) -> None:
    """One admin_audit row (database/schema.py: action in SHOP_AUDIT_ACTIONS, target_shop_id, a reason of at
    least 10 characters). Written in the caller's transaction, so a decision and its audit row stand or fall
    together."""
    db.mongo["admin_audit"].insert_one(
        {"_id": db.next_id("admin_audit"), "admin_id": admin.id, "action": action, "target_shop_id": shop.id,
         "reason": reason, "created_at": utcnow()}, session=db.session)


def _in_queue(shop: Shop) -> bool:
    return shop.verification_status == VerificationStatus.SUBMITTED or shop.renewal_pending


def _approve(db: Database, shop: Shop, admin: User) -> list[str]:
    """Approve the evidence waiting for review. Returns the private keys the approval makes stale (the evidence
    a renewal replaced), for the caller to delete once the transaction has committed."""
    if not _in_queue(shop):
        raise Conflict("This shop has no evidence waiting for review", code="not_awaiting_review")
    if not (shop.kyc_number and shop.kyc_storefront_key):
        raise Conflict("This shop has not submitted KYC evidence yet", code="evidence_missing")
    if shop.phone_verified_at is None:
        raise Conflict("Verify the shop's phone first (call the owner back, then record it)",
                       code="phone_not_verified")
    now = utcnow()
    renewal = shop.renewal_pending
    stale = evidence_keys(shop.previous_kyc)
    # A renewal moves the approval 12 months on from where it ends (or from now, if it has already ended).
    start = max(now, shop.verified_until) if renewal and shop.verified_until else now
    db.shops.set(shop, verification_status=VerificationStatus.VERIFIED, is_verified=True, decided_at=now,
                 decided_by=admin.id, rejection_reason=None, verified_until=months_after(start), previous_kyc=None)
    _audit(db, admin, "shop_verification_approve", shop,
           "KYC renewal approved" if renewal else "KYC evidence approved")
    notify(db, shop.owner_id, "shop_verified", "Your shop is verified",
           f"Customers can now find {shop.name} on NearShop. Verification lasts until "
           f"{shop.verified_until:%d %b %Y}.", "/shop/settings")
    return stale


def _reject_renewal(db: Database, shop: Shop, admin: User, reason: str) -> list[str]:
    """Reject a verified shop's renewal. The approval in force stands (plan section 8.1: the shop stays visible
    until verified_until), and so does the evidence behind it, restored from previous_kyc. Returns the keys of
    the rejected renewal's photos, to delete after the commit."""
    stale = [k for k in (shop.kyc_document_key, shop.kyc_storefront_key) if k]
    restored = shop.previous_kyc or {}
    fields: dict = {"decided_at": utcnow(), "decided_by": admin.id, "rejection_reason": reason, "previous_kyc": None}
    if restored:
        fields.update(kyc_type=restored.get("kyc_type"), kyc_number=restored.get("kyc_number"),
                      kyc_document_key=restored.get("kyc_document_key"),
                      kyc_storefront_key=restored.get("kyc_storefront_key"))
    else:
        stale = []  # nothing to fall back on (renewed before evidence was stashed): keep what there is
    db.shops.set(shop, **fields)
    _audit(db, admin, "shop_verification_reject_renewal", shop, reason)
    until = f" until {shop.verified_until:%d %b %Y}" if shop.verified_until else ""
    notify(db, shop.owner_id, "shop_renewal_rejected", "Your verification renewal was not approved",
           f"{reason} {shop.name} stays visible{until}. Submit fresh evidence before then.", "/shop/settings")
    return stale


def _decide(db: Database, shop: Shop, admin: User, status: VerificationStatus, reason: str) -> list[str]:
    """Reject or suspend: the shop is hidden at once. A rejection also drops any stashed earlier evidence (no
    approval rests on it any more); returns those keys, to delete after the commit."""
    stale = evidence_keys(shop.previous_kyc) if status == VerificationStatus.REJECTED else []
    fields: dict = {"verification_status": status, "is_verified": False, "decided_at": utcnow(),
                    "decided_by": admin.id, "rejection_reason": reason}
    if stale:
        fields["previous_kyc"] = None
    db.shops.set(shop, **fields)
    _audit(db, admin, f"shop_verification_{'reject' if status == VerificationStatus.REJECTED else 'suspend'}",
           shop, reason)
    title = "Your verification was not approved" if status == VerificationStatus.REJECTED else \
        "Your shop is suspended"
    notify(db, shop.owner_id, f"shop_{status.value}", title,
           f"{reason} Customers cannot see {shop.name} until it is verified.", "/shop/settings")
    return stale


def _decided(db: Database, shop_id: int, decide) -> Shop:
    """Load the shop and apply a decision in one transaction (shop change, audit row and notification together),
    then delete the private photos it made stale."""
    def run() -> tuple[Shop, list[str]]:
        s = _shop(db, shop_id)
        return s, decide(s) or []

    shop, stale = db.transaction(run)
    # Verification and suspension change what search may show: every API process's search caches follow
    # within 5 seconds (P8.1).
    cache_version.bump(db.mongo, cache_version.LISTINGS)
    for key in stale:
        delete_private(key)
    return shop


@router.get("/verification")
def verification_queue(_: AdminUser, db: DB):
    """Shops waiting for a decision, oldest submission first, each with its age in working days and 5-minute
    signed links to its document and storefront photos."""
    rows = db.shops.find(QUEUE, sort=[("submitted_at", 1), ("_id", 1)])
    owners = db.users.by_ids(s.owner_id for s in rows)
    now = utcnow()
    return [{
        "id": s.id, "name": s.name, "slug": s.slug, "locality": s.locality, "address_line": s.address_line,
        "pincode": s.pincode, "phone": s.phone, "owner": owners[s.owner_id].name if s.owner_id in owners else None,
        "kind": "renewal" if s.renewal_pending else "review" if s.decided_at else "new",
        "status": s.verification_status.value, "kyc_type": s.kyc_type.value if s.kyc_type else None,
        "kyc_number": s.kyc_number, "submitted_at": iso(s.submitted_at),
        "age_working_days": working_days_between(s.submitted_at, now) if s.submitted_at else None,
        "phone_verified_at": iso(s.phone_verified_at), "verified_until": iso(s.verified_until),
        "document_photo_url": _link(s.kyc_document_key), "storefront_photo_url": _link(s.kyc_storefront_key),
    } for s in rows]


@router.post("/shops/{shop_id}/verification/phone-verified")
def record_phone_verified(shop_id: int, admin: AdminUser, db: DB):
    """Record that an admin called the shop's phone back and reached the owner (until phone OTP, P4.1)."""
    def record(s: Shop) -> None:
        if not s.phone:
            raise Conflict("This shop has no phone number", code="no_phone")
        db.shops.set(s, phone_verified_at=utcnow())
        _audit(db, admin, "shop_phone_verified", s, "Called the shop back and reached the owner")

    s = _decided(db, shop_id, record)
    return {"ok": True, "phone_verified_at": iso(s.phone_verified_at)}


@router.post("/shops/{shop_id}/verification/approve")
def approve_verification(shop_id: int, admin: AdminUser, db: DB):
    s = _decided(db, shop_id, lambda s: _approve(db, s, admin))
    return {"ok": True, "status": s.verification_status.value, "verified_until": iso(s.verified_until)}


@router.post("/shops/{shop_id}/verification/reject")
def reject_verification(shop_id: int, body: VerificationDecisionIn, admin: AdminUser, db: DB):
    """Reject the evidence waiting for review. A new or re-reviewed shop becomes rejected (hidden); a renewal's
    rejection leaves the approval in force until verified_until (plan section 8.1)."""
    def reject(s: Shop) -> list[str]:
        if not _in_queue(s):
            raise Conflict("This shop has no evidence waiting for review", code="not_awaiting_review")
        if s.renewal_pending:
            return _reject_renewal(db, s, admin, body.reason)
        return _decide(db, s, admin, VerificationStatus.REJECTED, body.reason)

    s = _decided(db, shop_id, reject)
    return {"ok": True, "status": s.verification_status.value, "verified_until": iso(s.verified_until),
            "visible_to_customers": s.is_live}


@router.post("/shops/{shop_id}/verification/suspend")
def suspend_verification(shop_id: int, body: VerificationDecisionIn, admin: AdminUser, db: DB):
    def suspend(s: Shop) -> list[str]:
        if s.verification_status != VerificationStatus.VERIFIED:
            raise Conflict("Only a verified shop can be suspended", code="not_verified")
        return _decide(db, s, admin, VerificationStatus.SUSPENDED, body.reason)

    s = _decided(db, shop_id, suspend)
    return {"ok": True, "status": s.verification_status.value}


@router.get("/kyc-files/{key:path}")
def kyc_file(key: str, _: AdminUser, expires: int = 0, signature: str = ""):
    """A KYC document through its signed link (local storage; buckets use their own presigned links). Admins
    only, and only until the link expires (5 minutes); never cached."""
    if not check_signed_link(key, expires, signature):
        raise Forbidden("This link has expired. Open the document again from the queue.", code="link_expired")
    data = read_private(key)
    if data is None:
        raise NotFound("Document not found")
    return Response(data, media_type="image/webp",
                    headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"})


@router.get("/users")
def users(_: AdminUser, db: DB, role: Literal["customer", "owner", "admin"] | None = None,
          q: str | None = Query(None, max_length=80), limit: int = Query(100, ge=1, le=500)):
    query: dict = {}
    if role:
        query["role"] = Role(role)
    if q and q.strip():
        # By name, or by account id. Never by email: matching addresses would let an admin confirm or rebuild
        # a masked address one character at a time without the audited reveal below.
        term = q.strip()
        by_id = term.isascii() and term.isdigit() and len(term) <= 18
        query["$or"] = [{"name": _contains(term)}, *([{"_id": int(term)}] if by_id else [])]
    # Emails are masked; the full address takes the reveal action below, which is audited.
    return [{"id": u.id, "name": u.name, "email": mask_email(u.email), "role": u.role.value,
             "is_active": u.is_active, "deactivated_reason": u.deactivated_reason, "deactivated_at": iso(u.deactivated_at),
             "created_at": iso(u.created_at), "last_login_at": iso(u.last_login_at)}
            for u in db.users.find(query, sort=[("created_at", -1)], limit=limit)]


@router.post("/users/{user_id}/reveal-email")
def reveal_email(user_id: int, body: RevealEmailIn, admin: AdminUser, db: DB):
    """One user's full email, for a written reason. Who asked, when, for which user and why is recorded in
    admin_audit (kept for a year) before the address is returned."""
    user = db.users.get(user_id)
    if user is None:
        raise NotFound("User not found")
    db.mongo["admin_audit"].insert_one(
        {"_id": db.next_id("admin_audit"), "admin_id": admin.id, "action": "reveal_email",
         "target_user_id": user.id, "reason": body.reason, "created_at": utcnow()}, session=db.session)
    return {"id": user.id, "email": user.email}


# ---------------------------------------------------------------- synonyms (P5.4)


@router.get("/synonyms")
def synonyms(_: AdminUser, db: DB, q: str | None = Query(None, max_length=80)):
    """Every synonym entry (active first), the counts and the limit."""
    return synonym_service.list_entries(db, q)


@router.post("/synonyms", status_code=201)
def create_synonym(body: SynonymIn, admin: AdminUser, db: DB):
    return synonym_service.create_entry(db, admin.id, body.term, body.expansions, body.languages)


@router.patch("/synonyms/{entry_id}")
def update_synonym(entry_id: int, body: SynonymUpdateIn, admin: AdminUser, db: DB):
    """Edit an entry, or deactivate (active false) or reactivate it."""
    return synonym_service.update_entry(db, admin.id, entry_id, body.model_dump(exclude_unset=True))


@router.post("/synonyms/preview")
def preview_synonym(body: SynonymPreviewIn, _: AdminUser, db: DB):
    """The top 10 results at the city centre without the entry and with it, before saving."""
    return synonym_service.preview(db, body.term, body.expansions, body.query, body.entry_id)
# ------------------------------------------------------------------ plans and subscriptions (P9.4)


@router.get("/plans")
def plan_catalogue(_: AdminUser, db: DB, version: int | None = Query(None, ge=1)):
    """A catalogue version (default: the one in force), with every version number published so far."""
    in_force = plans.catalogue_version(db)
    shown = version or in_force
    rows = plans.catalogue(db, version=shown)
    if not rows:
        raise NotFound("No such catalogue version")
    versions = sorted(plans.plans(db).distinct("version"))
    return {"version": shown, "in_force": in_force, "versions": versions,
            "plans": [plans.plan_out(p) for p in rows.values()]}


@router.post("/plans")
def publish_plans(body: CataloguePublishIn, admin: AdminUser, db: DB):
    """Publish a new catalogue version: prices (paise) and on-sale flags, with a reason. Existing subscriptions keep
    their version until they renew."""
    version = plans.publish_catalogue(
        db, admin_id=admin.id, reason=body.reason, effective_from=body.effective_from,
        changes={code: change.model_dump(exclude_unset=True) for code, change in body.plans.items()})
    return {"version": version, "plans": [plans.plan_out(p) for p in plans.catalogue(db, version=version).values()]}


@router.get("/founding")
def founding_counter(_: AdminUser, db: DB):
    """The founding offer's counter: the cap, slots taken and remaining, and the founding cities."""
    return plans.founding_counter(db)


@router.get("/subscriptions")
def subscription_list(_: AdminUser, db: DB, status: Literal["advance", "active", "lapsed", "cancelled", "refunded"]
                      | None = None, limit: int = Query(200, ge=1, le=500)):
    """Subscriptions by status (all when none is given), the ones ending soonest first."""
    query = {"status": SubscriptionStatus(status).value} if status else {}
    rows = plans.subscriptions(db).find(query, sort=[("ends_at", 1), ("_id", 1)], limit=limit)
    names = {s.id: s.name for s in db.shops.by_ids(i for r in rows for i in r.shop_ids).values()}
    return [{**plans.subscription_out(r), "shops": [{"id": i, "name": names.get(i)} for i in r.shop_ids]}
            for r in rows]


@router.get("/subscriptions/{subscription_id}")
def subscription_detail(subscription_id: int, _: AdminUser, db: DB):
    sub = plans.subscriptions(db).get(subscription_id)
    if sub is None:
        raise NotFound("Subscription not found")
    history = plans.events(db).find({"subscription_id": sub.id}, sort=[("at", 1), ("_id", 1)])
    return {**plans.subscription_out(sub), "events": [plans.event_out(e) for e in history]}


@router.post("/subscriptions")
def grant_subscription(body: SubscriptionGrantIn, admin: AdminUser, db: DB):
    """Record a subscription: a payment taken by hand (shop_paid, or an advance before billing starts), a sponsored
    seat or a credit. A founding-city Growth year takes a founding slot while any remain."""
    sub = plans.create_subscription(
        db, shop_ids=body.shop_ids, plan_code=body.plan_code, period=body.period, source=body.source,
        actor=Actor(type="admin", id=admin.id), reason=body.reason, paid_at=body.paid_at, months=body.months)
    return plans.subscription_out(sub)


@router.post("/subscriptions/{subscription_id}/extend")
def extend_subscription(subscription_id: int, body: SubscriptionExtendIn, admin: AdminUser, db: DB):
    sub = plans.extend(db, subscription_id, actor=Actor(type="admin", id=admin.id), reason=body.reason,
                       months=body.months, ends_at=body.ends_at)
    return plans.subscription_out(sub)


@router.post("/subscriptions/{subscription_id}/cancel")
def cancel_subscription(subscription_id: int, body: SubscriptionCancelIn, admin: AdminUser, db: DB):
    sub = plans.cancel(db, subscription_id, actor=Actor(type="admin", id=admin.id), reason=body.reason)
    return plans.subscription_out(sub)


# ------------------------------------------------------------------ payments to NearShop (P9.5, app/services/billing.py)


def _admin(admin: User) -> Actor:
    return Actor(type="admin", id=admin.id)


@router.get("/billing/requests")
def payment_requests(_: AdminUser, db: DB, status: Literal["open", "paid", "cancelled", "refunded"] | None = None,
                     limit: int = Query(200, ge=1, le=500)):
    query = {"status": status} if status else {}
    return [billing.request_out(r) for r in billing.requests(db).find(query, sort=[("_id", -1)], limit=limit)]


@router.post("/billing/requests")
def create_payment_request(body: AdminPaymentRequestIn, admin: AdminUser, db: DB):
    """A payment request made for an owner, with the admin's reason kept on it (the shop's other open requests are
    cancelled)."""
    req = billing.create_request(db, _shop(db, body.shop_id), plan_code=body.plan_code, period=body.period,
                                 actor=_admin(admin), reason=body.reason)
    return billing.request_out(req)


@router.post("/billing/statements")
def upload_statement(admin: AdminUser, db: DB, file: UploadFile = File(...)):
    """The bank's statement export (CSV, the layout in docs/runbooks/billing.md): every new credit is recorded and
    matched by its request number and amount; the rest go to the unmatched queue, and repeats are duplicates."""
    data = file.file.read(settings.import_max_mb * 1024 * 1024 + 1)
    return billing.import_statement(db, data, file.filename or "statement.csv", actor=_admin(admin))


@router.get("/billing/credits")
def bank_credits(_: AdminUser, db: DB,
                 status: Literal["unmatched", "matched", "returned", "closed"] | None = "unmatched",
                 limit: int = Query(200, ge=1, le=500)):
    """The credits, by default the unmatched queue, oldest first."""
    query = {"status": status} if status else {}
    rows = billing.credits(db).find(query, sort=[("credited_at", 1), ("_id", 1)], limit=limit)
    return [billing.credit_out(c) for c in rows]


@router.post("/billing/credits/match")
def match_credit(body: ManualMatchIn, admin: AdminUser, db: DB):
    """Match a credit to a request by hand, by its UPI reference, with a reason."""
    pay = billing.match_by_hand(db, upi_reference=body.upi_reference, request_number=body.request_number,
                                reason=body.reason, actor=_admin(admin), amount_paise=body.amount_paise,
                                credited_on=body.credited_on)
    return billing.payment_out(pay)


@router.post("/billing/credits/{credit_id}/return")
def return_credit(credit_id: int, body: CreditReturnIn, admin: AdminUser, db: DB):
    """An unmatched credit sent back to the payer by bank transfer."""
    credit = billing.return_credit(db, credit_id, reason=body.reason, bank_reference=body.bank_reference,
                                   actor=_admin(admin))
    return billing.credit_out(credit)


@router.post("/billing/credits/{credit_id}/close")
def close_credit(credit_id: int, body: CreditReturnIn, admin: AdminUser, db: DB):
    """A gateway reconciliation mismatch (source reconciliation, P9.6) checked and closed, with the reason and the
    gateway's reference (a settlement id, support ticket or refund id) in bank_reference. Never money received."""
    credit = billing.close_credit(db, credit_id, reason=body.reason, reference=body.bank_reference,
                                  actor=_admin(admin))
    return billing.credit_out(credit)


@router.get("/billing/payments")
def billing_payments(_: AdminUser, db: DB, status: Literal["matched", "refunded"] | None = None,
                     limit: int = Query(200, ge=1, le=500)):
    query = {"status": status} if status else {}
    return [billing.payment_out(p) for p in billing.payments(db).find(query, sort=[("_id", -1)], limit=limit)]


@router.post("/billing/payments/{payment_id}/refund")
def refund_payment(payment_id: int, body: RefundIn, admin: AdminUser, db: DB):
    """Refund a payment in full: the subscription is refunded and a refund receipt or credit note is issued."""
    pay = billing.refund_payment(db, payment_id, reason=body.reason, bank_reference=body.bank_reference,
                                 actor=_admin(admin))
    return billing.payment_out(pay)


@router.get("/billing/documents/{document_id}")
def billing_document(document_id: int, _: AdminUser, db: DB):
    doc = billing.documents(db).get(document_id)
    if doc is None:
        raise NotFound("Document not found")
    return billing.document_out(doc)


@router.post("/billing/pilot-ledger")
def import_pilot_ledger(body: PilotLedgerIn, admin: AdminUser, db: DB):
    """The advance payments R1 recorded by hand: each becomes a matched payment, an advance subscription and a
    receipt showing the original payment date. A repeated UPI reference refuses the whole import."""
    return billing.import_pilot_ledger(db, [r.model_dump() for r in body.rows], reason=body.reason,
                                       actor=_admin(admin))
