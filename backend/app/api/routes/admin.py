import re
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.database import utcnow
from app.core.deps import DB, AdminUser
from app.core.errors import NotFound
from app.models import AnomalyStatus, Role
from app.schemas.requests import AdminShopUpdateIn, AnomalyReviewIn
from app.schemas.serializers import iso, listing, shop_brief
from app.services import analytics
from app.services import synonyms as synonym_service
from app.services.loaders import with_categories, with_category

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
    and the city-wide median listing age: check 10.3's metric (P7.4)."""
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


@router.get("/shops")
def shops(_: AdminUser, db: DB, q: str | None = Query(None, max_length=80)):
    query = {"$or": [{"name": _contains(q)}, {"locality": _contains(q)}]} if q else {}
    rows, counts = db.gather(
        lambda: with_categories(db, db.shops.find(query, sort=[("name", 1)])),
        lambda: {r["_id"]: r["n"] for r in db.products.aggregate([
            {"$match": {"is_active": True}}, {"$group": {"_id": "$shop_id", "n": {"$sum": 1}}}])},
    )
    owners = db.users.by_ids(s.owner_id for s in rows)
    return [{**shop_brief(s), "is_active": s.is_active, "owner": owners[s.owner_id].name,
             "owner_email": mask_email(owners[s.owner_id].email), "listings": counts.get(s.id, 0), "created_at": iso(s.created_at)}
            for s in rows if s.owner_id in owners]


@router.patch("/shops/{shop_id}")
def update_shop(shop_id: int, body: AdminShopUpdateIn, _: AdminUser, db: DB):
    s = db.shops.get(shop_id)
    if s is None:
        raise NotFound("Shop not found")
    changes = body.model_dump(exclude_unset=True)
    if changes:
        db.shops.set(s, **changes)
    return {"ok": True}


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
             "is_active": u.is_active, "created_at": iso(u.created_at), "last_login_at": iso(u.last_login_at)}
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
