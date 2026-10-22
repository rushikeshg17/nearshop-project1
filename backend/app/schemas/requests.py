"""Validated request bodies (Pydantic). Anything a client sends is checked here first."""
from typing import Literal

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
