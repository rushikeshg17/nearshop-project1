"""Users, categories, catalog, shops and products."""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from app.core.database import utcnow
from app.models.base import Document, Embedded, GeoPoint, Timestamps
from app.models.enums import InventoryReason, Role


class UserSession(Embedded):
    """One signed-in device (P2.4, app/core/security.py). Its id is the token's sid; last_seen_at is
    written at most once an hour; device is a short label from the user agent, never the raw string."""

    id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    device: str = "Unknown device"


class Nominee(Embedded):
    """The one person a user names to exercise their rights after their death or incapacity (P3.2, plan
    section 8.5): a name, and a phone or an email. Support acts on it only with documentary proof."""

    name: str
    phone: str | None = None
    email: str | None = None
    added_at: datetime = Field(default_factory=utcnow)


class User(Document, Timestamps):
    """Collection `users`. One account per email and one per phone (unique partial indexes uq_email_set and
    uq_phone, migration 7): since P4.1 an account may have a phone and no email (phone registration), and then
    the email field is left out of the document, not stored as null.

    Sessions (P2.4): a token is valid only while its ver equals token_version and its sid is in sessions
    (at most 10, newest last). Bumping token_version and clearing sessions ends every session at once.
    Admins also hold a second factor: totp_secret_enc (encrypted with NEARSHOP_TOTP_KEY), the SHA-256
    digests of their unused recovery codes, the last time step accepted (a code is never accepted twice)
    and the failed-code counter and lock."""

    name: str
    email: str | None = None
    phone: str | None = None
    # Set when the phone is verified by a one-time code (P4.1, app/services/otp.py). Phone sign-in reaches only an
    # account whose phone is verified. Reviews require it while settings.review_requires_verified_phone is on (P3.5).
    phone_verified_at: datetime | None = None
    # An Argon2 hash, or "" for an account with no password: one registered by phone (until it sets one through a
    # reset code) or a deleted one. An empty hash matches no password.
    password_hash: str
    role: Role = Role.CUSTOMER
    is_active: bool = True
    last_login_at: datetime | None = None
    # Saved default location for "near me"
    home_lat: float | None = None
    home_lng: float | None = None
    home_label: str | None = None
    # Revocable sessions (P2.4). Defaults cover documents written before them.
    token_version: int = 0
    sessions: list[UserSession] = Field(default_factory=list)
    # Admin second factor (P2.4), set only by database/admin_tasks.py create-admin.
    totp_secret_enc: str | None = None
    totp_enrolled_at: datetime | None = None
    totp_recovery_hashes: list[str] = Field(default_factory=list)
    totp_last_step: int = 0
    totp_failures: int = 0
    totp_locked_until: datetime | None = None
    # Privacy (P3.2, app/services/privacy.py). The nominee is declared here because models ignore undeclared
    # fields. A deletion waiting for orders in flight carries deletion_requested_at and deletion_due_at (at
    # most 7 days later), and deletion_flagged_at once it is overdue and flagged for an admin. deleted_at is
    # set when the account is anonymised.
    nominee: Nominee | None = None
    deletion_requested_at: datetime | None = None
    deletion_due_at: datetime | None = None
    deletion_flagged_at: datetime | None = None
    deleted_at: datetime | None = None
    # Why and when the account was switched off by a job, not by deletion: "phone_not_verified" when the phone
    # migration (P4.2, app/jobs.py) switched it off, still without a verified phone 30 days after the email sign-in
    # sunset. Admins see the reason in the users list.
    deactivated_at: datetime | None = None
    deactivated_reason: str | None = None

    @property
    def totp_enrolled(self) -> bool:
        return bool(self.totp_secret_enc)

    def to_mongo(self) -> dict[str, Any]:
        doc = super().to_mongo()
        if doc.get("email") is None:
            doc.pop("email", None)  # absent, never null: uq_email_set covers only accounts with an email
        return doc


class Category(Document):
    """Collection `categories`. Small reference data, unique on slug."""

    slug: str
    name: str
    icon: str = "Package"
    description: str | None = None
    color_hue: int = 250
    sort_order: int = 0


class CatalogItem(Document):
    """Collection `catalog_items`. Master product definition; shop listings (Product) may link to
    one so the same item can be compared across shops."""

    slug: str
    name: str
    brand: str | None = None
    category_id: int
    subcategory: str | None = None
    unit: str | None = None
    mrp: float | None = None
    typical_price: float | None = None
    description: str | None = None
    specs: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    icon: str = "Package"

    category: Category | None = Field(default=None, exclude=True)


class VerificationStatus(StrEnum):
    """A shop's KYC review (P3.3, plan section 8.1). Customers see a shop only while it is verified and its
    approval is in force (Shop.is_live)."""

    DRAFT = "draft"  # nothing submitted yet: a new shop starts here
    SUBMITTED = "submitted"  # waiting for an admin (also after a verified shop changes its address or phone)
    VERIFIED = "verified"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class KycType(StrEnum):
    GSTIN = "gstin"
    UDYAM = "udyam"
    TRADE_LICENCE = "trade_licence"


GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
UDYAM_PATTERN = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")
TRADE_LICENCE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9 /.-]{2,38}[A-Z0-9]$")
# Twelve digits in a row once spaces, dots and hyphens are removed: what an Aadhaar number looks like.
AADHAAR_LIKE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")


def gstin_check_char(first14: str) -> str:
    """The GSTIN check character for its first 14 characters (the published mod-36 scheme)."""
    total = 0
    for i, ch in enumerate(first14):
        product = GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def looks_like_aadhaar(value: str) -> bool:
    return bool(AADHAAR_LIKE.search(re.sub(r"[\s.-]", "", value)))


def validate_kyc_number(kyc_type: KycType | str, number: str) -> str:
    """The KYC number, normalised (upper case, outer spaces removed), or ValueError with a message an owner can
    act on. Anything that looks like an Aadhaar number is refused whatever the type: NearShop never stores one."""
    kyc_type = KycType(kyc_type)
    value = " ".join((number or "").split()).upper()
    if looks_like_aadhaar(value):
        raise ValueError("That looks like an Aadhaar number. NearShop never collects Aadhaar: enter your GSTIN, "
                         "Udyam number or trade licence number")
    if kyc_type == KycType.GSTIN:
        if len(value) != 15 or not GSTIN_PATTERN.match(value):
            raise ValueError("A GSTIN has 15 characters, like 27AAPFU0939F1ZV")
        if gstin_check_char(value[:14]) != value[14]:
            raise ValueError("That GSTIN's last character does not match: check it for a typing mistake")
    elif kyc_type == KycType.UDYAM:
        if not UDYAM_PATTERN.match(value):
            raise ValueError("A Udyam number looks like UDYAM-KR-03-0012345")
    elif not (4 <= len(value) <= 40 and TRADE_LICENCE_PATTERN.match(value)):
        raise ValueError("A trade licence number has 4 to 40 letters, digits, spaces, slashes, dots or hyphens")
    return value


def evidence_keys(evidence: dict[str, Any] | None) -> list[str]:
    """The private photo keys in a stashed set of KYC evidence (Shop.previous_kyc)."""
    return [k for k in ((evidence or {}).get("kyc_document_key"), (evidence or {}).get("kyc_storefront_key")) if k]


MIN_RATED_CUSTOMERS = 5  # a shop shows a rating only from this many distinct customers (P3.5)
VERIFICATION_MONTHS = 12  # an approval lasts this long
RENEWAL_WINDOW_DAYS = 30  # the owner may submit fresh evidence from this many days before it ends


def months_after(when: datetime, months: int = VERIFICATION_MONTHS) -> datetime:
    """The same day and time `months` later (the last day of the month when that day does not exist)."""
    total = when.month - 1 + months
    year, month = when.year + total // 12, total % 12 + 1
    for day in range(when.day, 27, -1):
        try:
            return when.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    return when.replace(year=year, month=month, day=min(when.day, 28))


class Shop(Document, Timestamps):
    """Collection `shops`. `location` is GeoJSON (2dsphere index) and `category_ids` embeds the
    shop-to-category relation, so "shops near me selling X" is a single indexed query."""

    owner_id: int
    name: str
    slug: str
    tagline: str | None = None
    description: str | None = None
    phone: str | None = None
    address_line: str
    locality: str | None = None
    city: str
    pincode: str | None = None
    location: GeoPoint
    opening_hours: str | None = None
    closed_on: str | None = None
    established_year: int | None = None

    offers_pickup: bool = True
    offers_delivery: bool = False
    delivery_radius_km: float = 0
    delivery_fee: float = 0
    free_delivery_above: float | None = None
    hold_minutes: int = 30
    # Public stock bands (P10.1, plan section 8.20): false by default, so customers see a listing's quantity capped at
    # settings.public_stock_cap and no exact count in any error, and may ask for at most that many units at once. The
    # owner turns it on in shop settings to show exact counts. Owners, staff and admins always see exact counts.
    show_exact_stock: bool = False

    is_active: bool = True
    # Kept in step with verification_status (true only while it is verified) for older readers.
    is_verified: bool = False
    image_path: str | None = None
    # KYC and verification (P3.3, plan section 8.1). The document and storefront photos are private storage
    # keys (app/services/media.py, save_private_image), never public URLs; admins see them through 5-minute
    # signed links. Never an Aadhaar number or image. A renewal is pending while the shop is verified and
    # submitted_at is later than decided_at.
    verification_status: VerificationStatus = VerificationStatus.DRAFT
    kyc_type: KycType | None = None
    kyc_number: str | None = None
    kyc_document_key: str | None = None
    kyc_storefront_key: str | None = None
    phone_verified_at: datetime | None = None
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: int | None = None
    rejection_reason: str | None = None
    verified_until: datetime | None = None
    # While a renewal waits, the evidence behind the approval still in force (kyc_type, kyc_number,
    # kyc_document_key, kyc_storefront_key): restored if the renewal is rejected, deleted once it is approved.
    previous_kyc: dict[str, Any] | None = None
    inventory_updated_at: datetime | None = None
    category_ids: list[int] = Field(default_factory=list)
    # Recomputed from the shop's counted, visible, unheld reviews in the same transaction as every review change
    # (app/services/reviews.py, P3.5), so listing shops never needs to aggregate reviews. rating_customers is the
    # number of distinct customers behind rating_count: no rating is shown below MIN_RATED_CUSTOMERS.
    rating_sum: int = 0
    rating_count: int = 0
    rating_customers: int = 0

    categories: list[Category] = Field(default_factory=list, exclude=True)
    owner: User | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _lat_lng_to_location(cls, data: Any) -> Any:
        # Convenience for code that thinks in lat/lng: Shop(lat=.., lng=..).
        if isinstance(data, dict) and "location" not in data and "lat" in data and "lng" in data:
            data = dict(data)
            data["location"] = GeoPoint.of(data.pop("lat"), data.pop("lng"))
        return data

    @property
    def lat(self) -> float:
        return self.location.lat

    @property
    def lng(self) -> float:
        return self.location.lng

    @property
    def rating_avg(self) -> float | None:
        """The average of the counted reviews, or None ("New shop") while fewer than MIN_RATED_CUSTOMERS distinct
        customers have counted reviews (P3.5, plan section 8.2)."""
        if not self.rating_count or self.rating_customers < MIN_RATED_CUSTOMERS:
            return None
        return round(self.rating_sum / self.rating_count, 1)

    @property
    def renewal_pending(self) -> bool:
        """Fresh evidence from a verified shop waits for an admin (renewals are accepted only before
        verified_until, so this also keeps a shop visible past it until the renewal is decided)."""
        return (self.verification_status == VerificationStatus.VERIFIED and self.submitted_at is not None
                and (self.decided_at is None or self.submitted_at > self.decided_at))

    def is_verified_at(self, now: datetime) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED and (
            (self.verified_until is not None and self.verified_until > now) or self.renewal_pending)

    @property
    def is_live(self) -> bool:
        """Whether customers may see and buy from this shop: active, and verified with the approval in force
        (or a renewal submitted in time still waiting). The same rule as live_shop_filter, on a loaded shop."""
        return self.is_active and self.is_verified_at(utcnow())


def live_shop_filter(now: datetime | None = None) -> dict[str, Any]:
    """The MongoDB filter for shops customers may see (Shop.is_live as a query). Expiry needs no job: a shop
    drops out once verified_until has passed, unless it submitted a renewal before then."""
    now = now or utcnow()
    return {"is_active": True, "verification_status": VerificationStatus.VERIFIED.value,
            "$or": [{"verified_until": {"$gt": now}}, {"$expr": {"$gt": ["$submitted_at", "$decided_at"]}}]}


class Product(Document, Timestamps):
    """Collection `products`. A shop's listing: its own price and stock for an item.
    Unique on (shop_id, catalog_item_id) when linked to the catalog. Text-indexed for keyword search."""

    shop_id: int
    catalog_item_id: int | None = None
    category_id: int
    name: str
    brand: str | None = None
    sku: str | None = None
    unit: str | None = None
    description: str | None = None
    specs: dict[str, Any] = Field(default_factory=dict)
    keywords: str = ""  # tags + category words, part of the text index
    icon: str = "Package"
    image_path: str | None = None
    price: float
    mrp: float | None = None
    quantity: int = 0
    low_stock_threshold: int = 5
    is_active: bool = True
    stock_updated_at: datetime = Field(default_factory=utcnow)

    shop: Shop | None = Field(default=None, exclude=True)
    category: Category | None = Field(default=None, exclude=True)
    catalog_item: CatalogItem | None = Field(default=None, exclude=True)

    @property
    def stock_status(self) -> str:
        if self.quantity <= 0:
            return "out_of_stock"
        if self.quantity <= self.low_stock_threshold:
            return "low_stock"
        return "in_stock"


class InventoryEvent(Document):
    """Collection `inventory_events`. Append-only audit log of every stock change; powers the
    'inventory freshness' trust signals."""

    product_id: int
    shop_id: int
    delta: int
    quantity_after: int
    reason: InventoryReason
    actor_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
