"""Grievances (P3.8, plan check 6.4): anyone can file one, track it by ticket number and get an answer within
30 days.

  * GET /api/grievances/officer: the grievance officer's name, designation, email, phone and postal address, from
    settings (NEARSHOP_GRIEVANCE_OFFICER_*). Until B10 appoints the officer, any detail not set is a clearly
    marked placeholder and the answer says placeholder: true.
  * POST /api/grievances: signed in or not, with a phone number or an email address to reach the complainant
    (contact). Fields: category, an optional order or reservation code, and a description. The answer is the
    ticket (G-XXXXXX) and the acknowledgement; a signed-in complainant also gets it as an in-app notification.
    A grievance counts as acknowledged when it is created. 5 per contact per day (app/core/ratelimit.py).
  * POST /api/grievances/lookup: the status, for the ticket plus the contact it was filed with. A POST, so the
    contact never travels in a URL.
  * GET /api/grievances/mine: the signed-in user's own grievances.

The admin queue and status changes are in app/api/routes/admin.py. Each grievance is due for resolution 30
days after it was received (due_at); the queue marks it for attention after 25 days. While a grievance is not
resolved, the order it names keeps its contact details past the retention period (app/services/privacy.py).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError, field_validator
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.deps import DB, CurrentUser, OptionalUser
from app.core.errors import NotFound
from app.core.ratelimit import normalise_contact
from app.schemas.serializers import iso
from app.services.notifications import notify
from app.utils.text import short_code

router = APIRouter(prefix="/grievances", tags=["grievances"])

COLLECTION = "grievances"
CATEGORIES = ("order", "shop_conduct", "privacy", "account", "other")
STATUSES = ("open", "acknowledged", "in_progress", "resolved")
RESOLVED = "resolved"
Category = Literal["order", "shop_conduct", "privacy", "account", "other"]
RESOLVE_DAYS = 30  # the resolution is due this many days after receipt
WARN_DAYS = 25  # after this many days an unresolved grievance is highlighted in the admin queue
TICKET_PATTERN = r"^G-[A-HJ-NP-Z2-9]{6}$"  # short_code("G"): no 0, O, 1 or I
CODE_PATTERN = r"^[A-Z]{1,3}-[A-Z0-9]{4,12}$"  # an order (D-...) or reservation (R-...) code
DESCRIPTION_MAX = 2000
ACKNOWLEDGEMENT = ("We have received your grievance and it is acknowledged. Keep the ticket number: with it and "
                   "the phone or email you gave, you can check its status on this page. The grievance officer will "
                   "resolve it within 30 days.")

PLACEHOLDER_OFFICER = {
    "name": "Placeholder: grievance officer not yet appointed",
    "designation": "Grievance Officer (placeholder)",
    "email": "grievance-placeholder@example.invalid",
    "phone": "Placeholder: not yet published",
    "address": "Placeholder: postal address not yet published",
}

_email_adapter: TypeAdapter[str] = TypeAdapter(EmailStr)


def officer() -> dict:
    """The officer's details from settings; any detail not set is its placeholder."""
    configured = {
        "name": settings.grievance_officer_name, "designation": settings.grievance_officer_designation,
        "email": settings.grievance_officer_email, "phone": settings.grievance_officer_phone,
        "address": settings.grievance_officer_address,
    }
    details = {k: (v.strip() or PLACEHOLDER_OFFICER[k]) for k, v in configured.items()}
    return {**details, "placeholder": any(not v.strip() for v in configured.values())}


def check_contact(value: str) -> str:
    """A contact as stored and compared: a valid email address (lower-cased), or an Indian mobile number as its
    ten digits (+91 98765 43210, 098765 43210 and 9876543210 alike). Anything else raises ValueError."""
    value = value.strip()
    if "@" in value:
        try:
            _email_adapter.validate_python(value)
        except ValidationError:
            raise ValueError("Enter a valid email address or a 10-digit mobile number") from None
        return normalise_contact(value)
    digits = re.sub(r"\D", "", value)
    national = (digits[2:] if len(digits) == 12 and digits.startswith("91")
                else digits[1:] if len(digits) == 11 and digits.startswith("0") else digits)
    if not re.fullmatch(r"[\d\s()+-]+", value) or not re.fullmatch(r"[6-9]\d{9}", national):
        raise ValueError("Enter a valid email address or a 10-digit mobile number")
    return national


def contact_kind(contact: str) -> str:
    return "email" if "@" in contact else "phone"


def due_date(received: datetime) -> datetime:
    """When the resolution is due: 30 days after receipt."""
    return received + timedelta(days=RESOLVE_DAYS)


def needs_attention(grievance: dict, now: datetime) -> bool:
    """Unresolved more than 25 days after receipt: the admin queue shows it in red."""
    return grievance["status"] != RESOLVED and now - grievance["created_at"] > timedelta(days=WARN_DAYS)


def grievance_doc(grievance_id: int, *, category: str, contact: str, description: str, user_id: int | None = None,
                  order_code: str | None = None, order_id: int | None = None, reservation_id: int | None = None,
                  ticket: str | None = None, now: datetime | None = None) -> dict:
    """A new grievance, acknowledged on receipt."""
    now = now or utcnow()
    return {
        "_id": grievance_id, "ticket": ticket or short_code("G"), "category": category, "description": description,
        "contact": contact, "contact_kind": contact_kind(contact), "user_id": user_id, "order_code": order_code,
        "order_id": order_id, "reservation_id": reservation_id, "status": "acknowledged", "source": "web",
        "created_at": now, "acknowledged_at": now, "due_at": due_date(now), "in_progress_at": None,
        "resolved_at": None, "resolved_by": None, "resolution": None, "updated_at": now,
    }


def public_json(g: dict) -> dict:
    """What the complainant sees: never the contact or the internal ids."""
    return {
        "ticket": g["ticket"], "category": g["category"], "description": g["description"],
        "order_code": g.get("order_code"), "status": g["status"], "received_at": iso(g["created_at"]),
        "acknowledged_at": iso(g.get("acknowledged_at")), "due_at": iso(g["due_at"]),
        "in_progress_at": iso(g.get("in_progress_at")), "resolved_at": iso(g.get("resolved_at")),
        "resolution": g.get("resolution"), "updated_at": iso(g.get("updated_at")),
    }


def admin_json(g: dict, now: datetime) -> dict:
    """A row of the admin queue: everything, with the due date, the days open and the 25-day highlight."""
    end = g.get("resolved_at") or now
    return {
        **public_json(g), "id": g["_id"], "contact": g["contact"], "contact_kind": g["contact_kind"],
        "user_id": g.get("user_id"), "order_id": g.get("order_id"), "reservation_id": g.get("reservation_id"),
        "days_open": (end - g["created_at"]).days, "needs_attention": needs_attention(g, now),
        "past_due": g["status"] != RESOLVED and now > g["due_at"],
    }


def _link_purchase(db: Database, code: str | None) -> tuple[int | None, int | None]:
    """The order or reservation a code names, if one does."""
    if not code:
        return None, None
    order = db.mongo["orders"].find_one({"code": code}, {"_id": 1})
    reservation = None if order else db.mongo["reservations"].find_one({"code": code}, {"_id": 1})
    return (order["_id"] if order else None), (reservation["_id"] if reservation else None)


class ContactIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    contact: str = Field(min_length=3, max_length=254)

    @field_validator("contact")
    @classmethod
    def _contact(cls, value: str) -> str:
        return check_contact(value)


class GrievanceIn(ContactIn):
    category: Category
    order_code: str | None = Field(default=None, max_length=20)
    description: str = Field(min_length=10, max_length=DESCRIPTION_MAX)

    @field_validator("order_code")
    @classmethod
    def _order_code(cls, value: str | None) -> str | None:
        value = (value or "").strip().upper()
        if not value:
            return None
        if not re.fullmatch(CODE_PATTERN, value):
            raise ValueError("Enter the code as it appears on your order or reservation, for example D-7KQ2MX")
        return value


class LookupIn(ContactIn):
    ticket: str = Field(min_length=3, max_length=20)

    @field_validator("ticket")
    @classmethod
    def _ticket(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(TICKET_PATTERN, value):
            raise ValueError("A ticket number looks like G-7KQ2MX")
        return value


@router.get("/officer")
def grievance_officer():
    return {**officer(), "resolve_days": RESOLVE_DAYS}


@router.post("", status_code=201)
def submit(body: GrievanceIn, user: OptionalUser, db: DB):
    order_id, reservation_id = _link_purchase(db, body.order_code)
    for attempt in range(5):
        doc = grievance_doc(db.next_id(COLLECTION), category=body.category, contact=body.contact,
                            description=body.description, user_id=user.id if user else None,
                            order_code=body.order_code, order_id=order_id, reservation_id=reservation_id)
        try:
            db.mongo[COLLECTION].insert_one(doc)
            break
        except DuplicateKeyError:  # the same ticket drawn twice; draw again
            if attempt == 4:
                raise
    if user:
        notify(db, user.id, "grievance", f"Grievance {doc['ticket']} received",
               body=f"Acknowledged. A resolution is due by {doc['due_at']:%d %b %Y}.",
               link=f"/grievance?ticket={doc['ticket']}")
    return {**public_json(doc), "message": ACKNOWLEDGEMENT}


@router.post("/lookup")
def lookup(body: LookupIn, db: DB):
    doc = db.mongo[COLLECTION].find_one({"ticket": body.ticket})
    if not doc or doc["contact"] != body.contact:
        raise NotFound("No grievance matches that ticket number and contact. Check both and try again.")
    return public_json(doc)


@router.get("/mine")
def mine(user: CurrentUser, db: DB):
    rows = db.mongo[COLLECTION].find({"user_id": user.id}, sort=[("created_at", -1)], limit=50)
    return [public_json(g) for g in rows]
