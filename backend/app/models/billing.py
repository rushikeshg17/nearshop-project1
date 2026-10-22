"""Plans, subscriptions and their event log (P9.4; plan section 8.18; app/services/plans.py).

  * plans: the versioned catalogue. A catalogue version is one row per plan code, all with the same version number
    and effective_from; the version in force at a time is the highest one whose effective_from has passed. A price
    change is a new version (plans.publish_catalogue), never an edit: rows are never changed after they are written.
  * subscriptions: which plan version a billing account pays for, for which shops, over which dates.
  * subscription_events: one append-only row for every change to a subscription, with the actor and the reason.

Prices are integers in paise and exclude GST (P9.5 adds it if NearShop registers).
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.core.database import utcnow
from app.models.base import Document, Embedded, Timestamps


class PlanCode(StrEnum):
    FREE = "free"
    GROWTH = "growth"
    PRO = "pro"
    BUSINESS = "business"
    PARTNER_SEAT = "partner_seat"


class Period(StrEnum):
    YEAR = "year"
    MONTH = "month"


class SubscriptionSource(StrEnum):
    SHOP_PAID = "shop_paid"
    ADVANCE = "advance"
    SPONSORED = "sponsored"  # paid by a distributor (P10.3) or another sponsor
    GUARANTEE_CREDIT = "guarantee_credit"


class SubscriptionStatus(StrEnum):
    ADVANCE = "advance"  # paid in advance, active from billing_starts_on
    ACTIVE = "active"
    LAPSED = "lapsed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"  # P9.5


class AccountType(StrEnum):
    OWNER = "owner"  # the owner's user id; P10.3 and P10.10 add distributors and lenders


# The sources a shop paid for itself: only these earn the outcome guarantee.
PAID_SOURCES = (SubscriptionSource.SHOP_PAID, SubscriptionSource.ADVANCE)


class Entitlements(Embedded):
    """What a plan allows. Every paid feature reads these through plans.effective_plan, and nothing else.
    The freshness tools of plan section 8.17 are never here: they are free on every plan, lapsed or not."""

    demand_report: str = "teaser"  # "teaser" (the top-3 "asked for near you") or "full" (the weekly report)
    low_stock_alerts: bool = False
    logins: int = 1  # the owner counts as one
    roi_statement: bool = False
    priority_support: bool = False
    boost_discount_pct: int = 0
    outcome_guarantee: bool = False
    pro_insights: bool = False  # forecast and restock, bundles, price position (P10.6)
    outlets: int = 1


class Plan(Document):
    """Collection plans: one plan in one catalogue version. Unique on (version, code)."""

    code: PlanCode
    version: int
    name: str
    price_year_paise: int | None = None  # None: not sold by the year (partner_seat is paid by a distributor)
    price_month_paise: int | None = None  # None: no monthly price
    founding_year_paise: int | None = None  # the founding offer's first-year price (Growth only)
    on_sale: bool = False
    entitlements: Entitlements = Field(default_factory=Entitlements)
    effective_from: datetime
    published_at: datetime = Field(default_factory=utcnow)
    published_by: int | None = None  # the admin's user id; None for the migration
    reason: str


class Subscription(Document, Timestamps):
    """Collection subscriptions. ends_at is exclusive: the shop is on Free from that moment. paid_until is the end of
    the paid term (ends_at before any guarantee credit); term_start, quarters_done and next_quarter_end are the
    outcome guarantee's cursor over the paid quarters (None: no paid quarter left to check). reminded lists the reminder stages (days before ends_at) already sent
    for the current ends_at."""

    account_type: AccountType = AccountType.OWNER
    account_id: int
    shop_ids: list[int]
    plan_id: int
    plan_code: PlanCode
    plan_version: int
    period: Period = Period.YEAR
    price_paise: int = 0
    founding: bool = False
    founding_slot: int | None = None
    founding_shop_id: int | None = None  # set with founding: one founding offer per shop (unique index)
    source: SubscriptionSource
    status: SubscriptionStatus
    starts_at: datetime
    ends_at: datetime
    paid_until: datetime
    paid_at: datetime | None = None  # the first payment (advances: the 300-day alert counts from it)
    term_start: datetime | None = None
    quarters_done: int = 0
    next_quarter_end: datetime | None = None
    reminded: list[int] = Field(default_factory=list)
    advance_alerted_at: datetime | None = None


class Actor(Embedded):
    type: str  # admin, system or owner
    id: int | None = None


class SubscriptionEvent(Document):
    """Collection subscription_events: append-only, never updated or deleted by the app."""

    subscription_id: int
    shop_ids: list[int]
    kind: str  # one of EVENT_KINDS
    actor: Actor
    reason: str
    status_before: SubscriptionStatus | None = None
    status_after: SubscriptionStatus
    ends_at_before: datetime | None = None
    ends_at_after: datetime
    at: datetime = Field(default_factory=utcnow)


EVENT_KINDS = ("created", "activated", "renewed", "extended", "lapsed", "cancelled", "guarantee_credit",
               "guarantee_met", "reminder", "advance_alert")
