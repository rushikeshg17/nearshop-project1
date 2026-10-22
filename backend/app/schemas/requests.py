"""Validated request bodies (Pydantic). Anything a client sends is checked here first."""
from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.core import Language, validate_upi_id, validate_upi_payee_name

PHONE_PATTERN = r"^[6-9]\d{9}$"  # Indian mobile numbers


class _Strict(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}


# Consent at registration (P3.1, plan section 8.5). Each purpose, and the age declaration, is written as a
# row of the append-only consents collection against the current notice version (settings.notice_version).
REQUIRED_PURPOSES: tuple[str, ...] = ("account_and_orders",)  # account and order fulfilment
# Off unless the user turns them on. search_measurement: helping improve search (used by P5.6).
OPTIONAL_PURPOSES: tuple[str, ...] = ("whatsapp", "personalised_suggestions", "search_measurement")
AGE_PURPOSE = "age_18_plus"  # the declaration that the user is 18 or older
CONSENT_PURPOSES: tuple[str, ...] = (*REQUIRED_PURPOSES, *OPTIONAL_PURPOSES, AGE_PURPOSE)


class ConsentIn(_Strict):
    """What the user ticked on the sign-up screen. Everything defaults to false, so a missing or empty
    consent object is a refusal (422 consent_required or age_confirmation_required from the route)."""

    account_and_orders: bool = False
    age_18_plus: bool = False
    whatsapp: bool = False
    personalised_suggestions: bool = False
    search_measurement: bool = False


class RegisterIn(_Strict):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
    consent: ConsentIn = Field(default_factory=ConsentIn)


class ShopSetupIn(_Strict):
    name: str = Field(min_length=3, max_length=160)
    tagline: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    category_slugs: list[str] = Field(min_length=1, max_length=3)
    phone: str = Field(pattern=PHONE_PATTERN)
    address_line: str = Field(min_length=5, max_length=255)
    locality: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, pattern=r"^\d{6}$")
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    opening_hours: str | None = Field(default="9:30 AM - 9:00 PM", max_length=80)
    closed_on: str | None = Field(default=None, max_length=40)
    offers_delivery: bool = False
    delivery_radius_km: float = Field(default=0, ge=0, le=25)
    delivery_fee: float = Field(default=0, ge=0, le=500)
    free_delivery_above: float | None = Field(default=None, ge=0)
    hold_minutes: int = Field(default=30, ge=10, le=240)


class RegisterShopIn(_Strict):
    owner: RegisterIn
    shop: ShopSetupIn


class LoginIn(_Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ProfileUpdateIn(_Strict):
    """The profile fields a user edits directly. Not the phone: since P4.1 a new number is set only by verifying
    a code sent to it (POST /api/auth/otp/request and /otp/verify, purpose verify), so a phone here is refused
    (422, an unknown field)."""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    home_lat: float | None = Field(default=None, ge=-90, le=90)
    home_lng: float | None = Field(default=None, ge=-180, le=180)
    home_label: str | None = Field(default=None, max_length=160)
    # The language notifications and WhatsApp messages come in (P7.11): en or kn. Not nullable: null is refused (422).
    language: Language = "en"


# Phone one-time codes (P4.1, app/services/otp.py). login: sign in to the account with this verified phone;
# register: create a customer account for it; verify: set or confirm the signed-in user's phone; reset: set a new
# password for the account with this verified phone; delete: confirm deleting the signed-in account (POST
# /api/auth/delete takes the code instead of a password, for an account registered by phone, which has none).
OTP_PURPOSES: tuple[str, ...] = ("login", "register", "verify", "reset", "delete")
OtpPurpose = Literal["login", "register", "verify", "reset"]  # the purposes POST /api/auth/otp/verify spends
OtpRequestPurpose = Literal["login", "register", "verify", "reset", "delete"]
OtpChannel = Literal["whatsapp", "sms"]
OTP_CODE_PATTERN = r"^\d{6}$"


class OtpRequestIn(_Strict):
    """channel: the channel to try first, when it is configured (GET /api/auth/phone-signin lists them), with
    the others as the fallback. Left out, WhatsApp goes first, except on a resend after an unused WhatsApp code,
    when SMS does (app/services/otp.py)."""

    phone: str = Field(pattern=PHONE_PATTERN)
    purpose: OtpRequestPurpose
    channel: OtpChannel | None = None


class OtpVerifyIn(_Strict):
    """The code, and what its purpose needs besides: a name and the P3.1 consent to register, a new password to
    reset. The consent defaults to nothing ticked, which registration refuses (422 consent_required)."""

    phone: str = Field(pattern=PHONE_PATTERN)
    purpose: OtpPurpose
    code: str = Field(pattern=OTP_CODE_PATTERN)
    name: str | None = Field(default=None, min_length=2, max_length=120)
    consent: ConsentIn = Field(default_factory=ConsentIn)
    new_password: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def _purpose_fields(self) -> "OtpVerifyIn":
        if self.purpose == "register" and not self.name:
            raise ValueError("Give your name to create an account")
        if self.purpose == "reset" and not self.new_password:
            raise ValueError("Choose a new password of 8 or more characters")
        return self


class ShopUpdateIn(_Strict):
    name: str | None = Field(default=None, min_length=3, max_length=160)
    tagline: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    category_slugs: list[str] | None = Field(default=None, min_length=1, max_length=3)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
    address_line: str | None = Field(default=None, min_length=5, max_length=255)
    locality: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, pattern=r"^\d{6}$")
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    opening_hours: str | None = Field(default=None, max_length=80)
    closed_on: str | None = Field(default=None, max_length=40)
    offers_pickup: bool | None = None
    offers_delivery: bool | None = None
    delivery_radius_km: float | None = Field(default=None, ge=0, le=25)
    delivery_fee: float | None = Field(default=None, ge=0, le=500)
    free_delivery_above: float | None = Field(default=None, ge=0)
    hold_minutes: int | None = Field(default=None, ge=10, le=240)
    show_exact_stock: bool | None = None  # P10.1: show customers exact stock counts
    is_active: bool | None = None
    # Direct UPI (P6.1): the shop's own UPI ID and payee name. null or an empty string removes them.
    upi_id: str | None = Field(default=None, max_length=50)
    upi_payee_name: str | None = Field(default=None, max_length=60)

    @field_validator("upi_id")
    @classmethod
    def _upi_id(cls, value: str | None) -> str | None:
        return validate_upi_id(value) if value else None

    @field_validator("upi_payee_name")
    @classmethod
    def _upi_payee_name(cls, value: str | None) -> str | None:
        return validate_upi_payee_name(value) if value else None

    @field_validator("show_exact_stock")
    @classmethod
    def _show_exact_stock_not_null(cls, v: bool | None) -> bool:
        # Leave the field out to keep it unchanged; an explicit null would be stored and break loading the Shop.
        if v is None:
            raise ValueError("show_exact_stock must be true or false")
        return v


class ProductIn(_Strict):
    name: str = Field(min_length=2, max_length=200)
    category_slug: str
    brand: str | None = Field(default=None, max_length=80)
    sku: str | None = Field(default=None, max_length=60)
    unit: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=2000)
    specs: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=20)
    price: float = Field(gt=0, le=10_000_000)
    mrp: float | None = Field(default=None, gt=0)
    quantity: int = Field(ge=0, le=100_000)
    low_stock_threshold: int = Field(default=5, ge=0, le=10_000)


class ProductUpdateIn(_Strict):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    category_slug: str | None = None
    brand: str | None = Field(default=None, max_length=80)
    sku: str | None = Field(default=None, max_length=60)
    unit: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=2000)
    specs: dict[str, str] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    price: float | None = Field(default=None, gt=0, le=10_000_000)
    mrp: float | None = Field(default=None, gt=0)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None


class StockIn(_Strict):
    quantity: int = Field(ge=0, le=100_000)


class CatalogAddItem(_Strict):
    catalog_item_id: int
    price: float = Field(gt=0, le=10_000_000)
    quantity: int = Field(ge=0, le=100_000)


class CatalogAddIn(_Strict):
    items: list[CatalogAddItem] = Field(min_length=1, max_length=100)


class ReservationIn(_Strict):
    product_id: int
    quantity: int = Field(ge=1, le=20)
    note: str | None = Field(default=None, max_length=300)


class TransitionIn(_Strict):
    reason: str | None = Field(default=None, max_length=300)


# How the customer paid, which the owner chooses when delivering an order or completing a pickup (P6.1): cash, or
# the shop's own UPI ID (app/models/activity.py, PAYMENT_METHODS).
PaymentMethod = Literal["cod", "upi_direct"]


class OrderItemIn(_Strict):
    product_id: int
    quantity: int = Field(ge=1, le=50)


class DeliveryLocation(_Strict):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class OrderQuoteIn(DeliveryLocation):
    items: list[OrderItemIn] = Field(min_length=1, max_length=20)


class OrderIn(DeliveryLocation):
    items: list[OrderItemIn] = Field(min_length=1, max_length=20)
    address: str = Field(min_length=8, max_length=300)
    phone: str = Field(pattern=PHONE_PATTERN)
    note: str | None = Field(default=None, max_length=300)


class ReviewIn(_Strict):
    reservation_id: int | None = None
    order_id: int | None = None
    rating: int = Field(ge=1, le=5)
    accuracy_rating: int | None = Field(default=None, ge=1, le=5)
    delivery_rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def one_source(self):
        if (self.order_id is None) == (self.reservation_id is None):
            raise ValueError("Review exactly one completed reservation or delivered order")
        return self


class NotificationsReadIn(_Strict):
    ids: list[int] | None = None  # None = mark all read


class AnomalyReviewIn(_Strict):
    status: Literal["reviewed", "dismissed"]
    note: str | None = Field(default=None, max_length=300)


class AdminShopUpdateIn(_Strict):
    is_verified: bool | None = None
    is_active: bool | None = None


# Plans and subscriptions (P9.4, app/services/plans.py). Every admin change carries a reason, kept on the catalogue
# row or in subscription_events. Times may be sent with an offset; they are stored as naive UTC.
PLAN_CODES = Literal["free", "growth", "pro", "business", "partner_seat"]
PAISE_MAX = 10_000_000  # ₹1,00,000


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


class PlanChangeIn(_Strict):
    """One plan's changes in a new catalogue version: prices in paise and the on-sale flag (and the name)."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    price_year_paise: int | None = Field(default=None, ge=0, le=PAISE_MAX)
    price_month_paise: int | None = Field(default=None, ge=0, le=PAISE_MAX)
    founding_year_paise: int | None = Field(default=None, ge=0, le=PAISE_MAX)
    on_sale: bool | None = None

    @model_validator(mode="after")
    def not_null(self):
        # A price of null means "not offered"; a name and the on-sale flag always have a value, so a change leaves
        # them out to keep them.
        nulls = [f for f in ("name", "on_sale") if f in self.model_fields_set and getattr(self, f) is None]
        if nulls:
            raise ValueError(f"{', '.join(nulls)} cannot be null: leave it out to keep the current value")
        return self


class CataloguePublishIn(_Strict):
    reason: str = Field(min_length=10, max_length=500)
    effective_from: datetime | None = None  # default: now; never earlier
    plans: dict[PLAN_CODES, PlanChangeIn] = Field(min_length=1)

    @field_validator("effective_from")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _naive_utc(value)


class SubscriptionGrantIn(_Strict):
    shop_ids: list[int] = Field(min_length=1, max_length=10)
    plan_code: Literal["growth", "pro", "business", "partner_seat"]
    period: Literal["year", "month"] = "year"
    source: Literal["shop_paid", "advance", "sponsored", "guarantee_credit"]
    paid_at: datetime | None = None  # shop_paid and advance: when the payment was made (default now)
    months: int | None = Field(default=None, ge=1, le=36)  # default: the period's length
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("paid_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _naive_utc(value)


class SubscriptionExtendIn(_Strict):
    months: int | None = Field(default=None, ge=1, le=36)
    ends_at: datetime | None = None
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("ends_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _naive_utc(value)

    @model_validator(mode="after")
    def _one(self) -> "SubscriptionExtendIn":
        if (self.months is None) == (self.ends_at is None):
            raise ValueError("Give either months or ends_at")
        return self


class SubscriptionCancelIn(_Strict):
    reason: str = Field(min_length=10, max_length=500)


# ------------------------------------------------------------------ payments to NearShop (P9.5, app/services/billing.py)

STATE_CODE_PATTERN = r"^\d{2}$"  # the two-digit GST state code (29 is Karnataka)
UPI_REFERENCE = Field(min_length=6, max_length=64, pattern=r"^[A-Za-z0-9/\-]+$")
BANK_REFERENCE = Field(min_length=4, max_length=64)
BILLING_REASON = Field(min_length=10, max_length=500)


class BillingAccountIn(_Strict):
    legal_name: str = Field(min_length=2, max_length=200)
    address: str = Field(min_length=5, max_length=500)
    state_code: str = Field(pattern=STATE_CODE_PATTERN)
    gstin: str | None = Field(default=None, max_length=15)  # only when the payer is registered for GST


class PaymentRequestIn(_Strict):
    plan_code: Literal["growth", "pro", "business"] = "growth"
    period: Literal["year"] = "year"  # monthly plans are paid by UPI Autopay (P9.6)


class AdminPaymentRequestIn(PaymentRequestIn):
    shop_id: int = Field(ge=1)
    reason: str = BILLING_REASON


class ManualMatchIn(_Strict):
    upi_reference: str = UPI_REFERENCE
    request_number: str = Field(min_length=9, max_length=16)
    reason: str = BILLING_REASON
    # Only for a credit not on any uploaded statement yet: its amount and the day it was credited.
    amount_paise: int | None = Field(default=None, ge=1, le=PAISE_MAX)
    credited_on: date | None = None

    @model_validator(mode="after")
    def _both(self) -> "ManualMatchIn":
        if (self.amount_paise is None) != (self.credited_on is None):
            raise ValueError("Give both amount_paise and credited_on, or neither")
        return self


class CreditReturnIn(_Strict):
    bank_reference: str = BANK_REFERENCE
    reason: str = BILLING_REASON


class RefundIn(_Strict):
    bank_reference: str = BANK_REFERENCE  # the bank transfer that returned the money
    reason: str = BILLING_REASON


class PilotLedgerRow(_Strict):
    paid_on: date
    shop_id: int = Field(ge=1)
    amount_paise: int = Field(ge=1, le=PAISE_MAX)
    upi_reference: str = UPI_REFERENCE

    @field_validator("paid_on")
    @classmethod
    def _not_future(cls, value: date) -> date:
        if value > datetime.now(ZoneInfo("Asia/Kolkata")).date():
            raise ValueError("paid_on is later than today (IST): a ledger row records a payment already made")
        return value


class PilotLedgerIn(_Strict):
    rows: list[PilotLedgerRow] = Field(min_length=1, max_length=500)
    reason: str = BILLING_REASON
