"""Payments to NearShop's own account, receipts and invoices (P9.5; plan section 8.18; docs/runbooks/billing.md).

A shop pays NearShop's fees by UPI straight into NearShop's own current account (the merchant UPI ID in
NEARSHOP_BILLING_UPI_ID), with the payment request's number as the payment note. Admins upload the bank's statement,
every credit is matched to its request, and each matched payment produces a numbered receipt or tax invoice. This
module only ever collects NearShop's own fees: it never collects money for a shop (customers pay shops directly,
P6.1, plan section 8.8). Monthly Growth is paid by UPI Autopay (P9.6), so a payment request here is for a year.

Collections
-----------
  * billing_accounts: who pays. kind (shop_owner now; distributor and lender are reserved for P10.3 and P10.10), the
    legal name, the address, the GST state code, the GSTIN when the payer is registered, and the contact user. A shop
    owner's account is made from the shop's name and address the first time it is needed; the owner can correct it.
  * payment_requests: what is to be paid. A unique number (NSP and six digits, the UPI payment note), the purpose
    (plan; boost is reserved for P10.12), the plan version and period, the kind (advance before
    NEARSHOP_BILLING_STARTS_ON, new, or renewal of the shop's active subscription), the amount before GST (the
    founding price when the shop qualifies), the GST and the total. A new request for a shop cancels its open ones.
  * bank_credits: every credit seen on an uploaded statement (or entered by hand, or imported from the pilot ledger),
    unique by its UPI reference: unmatched (with the reason), matched, or returned to the payer.
  * billing_payments: a matched payment, with its request, credit, subscription and document, and its refund.
  * billing_documents: the receipts, tax invoices, refund receipts and credit notes. Immutable snapshots: the
    supplier and recipient as they were, the amounts and the wording, with a content hash (verify_document).
    documents() refuses every write but insert. P9.10 renders them for printing.
  * bank_statements: one row per statement upload with its counts, for the audit trail.

Matching
--------
A statement credit is matched by the request number in its narration and its amount, which must equal the request's
total. Anything else (no number, an unknown, paid or cancelled request, another amount, a price or GST position that
changed since the request) stays in the unmatched queue with the reason; a credit whose UPI reference was already
seen is reported as a duplicate and not recorded again. An admin can match a credit by hand, by its UPI reference,
with a reason, or return it to the payer by bank transfer. The payment, its subscription change (plans.py:
create_subscription, or renew for a renewal) and its document are written in one transaction: if any of them fails,
none is written and the credit stays unmatched.

Documents
---------
The document type follows the GST position on the payment's date (gst_mode_at): unregistered, a payment receipt that
states no GST is charged; registered (NEARSHOP_BILLING_GST_MODE=registered from NEARSHOP_BILLING_GST_REGISTERED_FROM),
a tax invoice with CGST and SGST when the payer's state code is the supplier's (Karnataka, 29), IGST otherwise. A
refund issues a refund receipt for a receipt and a credit note for a tax invoice, referencing the original. Every
document takes the next number of one series per financial year (April to March, IST), <prefix>/<YY-YY>/<NNNNNN>
(at most 16 characters), from one counter document incremented inside the transaction, so a rolled-back payment
leaves no gap and two payments at once never share a number. The wording is the CA's approved text (B22) from
settings; until NEARSHOP_BILLING_DOCUMENT_FORMAT names the approved format, documents are drafts carrying
"Draft format, not for use", and production refuses to issue them.

Refunds and the pilot ledger
----------------------------
An admin refunds a payment with a reason and the bank transfer's reference: the payment and its request are marked
refunded, and the refund document references the original. The subscription changes by the payment's own term only:
when it is the subscription's only payment not yet refunded, the subscription becomes refunded (the shop is on Free
from then); when it is a renewal and earlier terms are still paid, only that renewal's term is taken off (ends_at and
paid_until move back by it, and the subscription lapses if that end has passed), so the shop keeps the time it paid
for and kept. Payments on one subscription are refunded latest first: an earlier one is refused, naming the later
payment to refund first, and becomes refundable once that one is, so a matched payment can always be refunded. Advance subscriptions are refunded on request before billing starts this way. The pilot ledger import
turns the advance payments R1 recorded by hand into matched payments, advance subscriptions and receipts showing the
original payment date, in the order they were paid (the founding offer counts first payments); a UPI reference seen
before refuses the whole import.

The daily job
-------------
alert_unmatched (the billing_reconciliation job in app/jobs.py, once per IST day) alerts every admin, in the app and
with a warning event, about each credit still unmatched NEARSHOP_BILLING_UNMATCHED_ALERT_WORKING_DAYS (3) working days
after the day it was credited, once per credit.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Collection, Database, utcnow
from app.core.errors import AppError, NotFound
from app.models import Shop
from app.models.base import Document, Embedded, Timestamps
from app.models.billing import (
    Actor,
    PaidTerm,
    Period,
    PlanCode,
    Subscription,
    SubscriptionSource,
    SubscriptionStatus,
)
from app.services import leads, plans
from app.services.notifications import notify

log = logging.getLogger(__name__)

ACCOUNTS = "billing_accounts"
REQUESTS = "payment_requests"
CREDITS = "bank_credits"
PAYMENTS = "billing_payments"
DOCUMENTS = "billing_documents"
STATEMENTS = "bank_statements"
SERIES_PREFIX = "billing_series_"  # counters: one document per financial year, seq the last number issued

REQUEST_PREFIX = "NSP"  # a request number: NSP and six digits, the UPI payment note
REQUEST_NUMBER = re.compile(r"NSP[\s\-/]*(\d{6,9})", re.IGNORECASE)
GSTIN_PATTERN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
DRAFT_FORMAT = "draft"
DRAFT_LINE = "Draft format, not for use"
WORDING_PENDING = "[The CA's approved wording (B22) goes here]"
MAX_NUMBER_LENGTH = 16
STATE_NAMES = {"29": "Karnataka"}  # the place of supply is printed as the state code, with its name when known
BILLING_LINK = "/owner/billing"
KIND_PAYMENT_RECEIVED, KIND_REFUNDED, KIND_UNMATCHED = "payment_received", "payment_refunded", "credit_unmatched"
UNMATCHED_WARNING = "bank credit unmatched"
SUBSCRIPTION_EVENT_KINDS = ("refunded", "renewal_refunded")  # the subscription_events kinds this module adds to plans.EVENT_KINDS


class BillingRefused(AppError):
    status_code = 409
    code = "billing_refused"


class BillingNotConfigured(AppError):
    status_code = 503
    code = "billing_not_configured"


class DocumentImmutable(AppError):
    status_code = 409
    code = "document_immutable"


# ---------------------------------------------------------------- models


class AccountKind(StrEnum):
    SHOP_OWNER = "shop_owner"
    DISTRIBUTOR = "distributor"  # P10.3
    LENDER = "lender"  # P10.10


class BillingAccount(Document, Timestamps):
    kind: AccountKind = AccountKind.SHOP_OWNER
    legal_name: str
    address: str
    state_code: str
    gstin: str | None = None
    contact_user_id: int


class Purpose(StrEnum):
    PLAN = "plan"
    BOOST = "boost"  # P10.12


class RequestKind(StrEnum):
    ADVANCE = "advance"  # before billing starts: an advance subscription
    NEW = "new"  # a new shop-paid subscription
    RENEWAL = "renewal"  # one more period of the shop's active subscription


class RequestStatus(StrEnum):
    OPEN = "open"
    PAID = "paid"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class GstMode(StrEnum):
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"


class PaymentRequest(Document, Timestamps):
    number: str = ""
    billing_account_id: int
    owner_id: int
    shop_id: int
    purpose: Purpose = Purpose.PLAN
    plan_code: PlanCode
    plan_version: int
    period: Period = Period.YEAR
    kind: RequestKind
    subscription_id: int | None = None  # a renewal's subscription
    founding: bool = False
    amount_paise: int  # before GST: the taxable value
    gst_mode: GstMode
    gst_paise: int = 0
    total_paise: int
    status: RequestStatus = RequestStatus.OPEN
    created_by: Actor
    reason: str | None = None  # why an admin made the request for the owner (AdminPaymentRequestIn)
    payment_id: int | None = None


class CreditStatus(StrEnum):
    UNMATCHED = "unmatched"
    MATCHED = "matched"
    RETURNED = "returned"


class CreditSource(StrEnum):
    STATEMENT = "statement"
    MANUAL = "manual"
    PILOT_LEDGER = "pilot_ledger"


class CreditReturn(Embedded):
    at: datetime
    by: Actor
    reason: str
    bank_reference: str


class BankCredit(Document):
    upi_reference: str
    credited_at: datetime  # the start of the IST day it was credited
    amount_paise: int
    narration: str = ""
    source: CreditSource
    statement_id: int | None = None
    request_number: str | None = None  # the number found in the narration, if any
    status: CreditStatus = CreditStatus.UNMATCHED
    note: str | None = None  # why it is unmatched
    payment_id: int | None = None
    alerted_at: datetime | None = None
    returned: CreditReturn | None = None
    created_at: datetime = Field(default_factory=utcnow)


class PaymentStatus(StrEnum):
    MATCHED = "matched"
    REFUNDED = "refunded"


class Refund(Embedded):
    at: datetime
    by: Actor
    reason: str
    bank_reference: str
    document_id: int


class Payment(Document):
    request_id: int
    request_number: str
    billing_account_id: int
    shop_id: int
    credit_id: int
    upi_reference: str
    amount_paise: int  # the total received
    paid_at: datetime  # the credit's date (the original payment date for the pilot ledger)
    matched_at: datetime
    matched_by: Actor
    match: CreditSource
    reason: str
    subscription_id: int
    document_id: int | None = None
    status: PaymentStatus = PaymentStatus.MATCHED
    refund: Refund | None = None


class DocType(StrEnum):
    RECEIPT = "receipt"
    TAX_INVOICE = "tax_invoice"
    REFUND_RECEIPT = "refund_receipt"
    CREDIT_NOTE = "credit_note"


class Party(Embedded):
    legal_name: str
    address: str
    state_code: str
    gstin: str | None = None


class BillingDocument(Document):
    number: str
    financial_year: str  # 2027-28
    seq: int
    type: DocType
    issued_at: datetime
    payment_date: datetime  # when the money moved: the payment's date, or the refund's
    format: str  # the CA-approved format's label, or draft
    draft_line: str | None = None  # "Draft format, not for use" on a draft
    wording: str
    supplier: Party
    recipient: Party
    place_of_supply: str
    description: str
    sac: str | None = None
    taxable_paise: int
    gst_rate_pct: int = 0
    cgst_paise: int = 0
    sgst_paise: int = 0
    igst_paise: int = 0
    total_paise: int
    payment_id: int
    request_number: str
    upi_reference: str
    original_document_id: int | None = None
    original_number: str | None = None
    refund_reference: str | None = None  # the bank transfer that returned the money
    content_hash: str = ""


class BankStatement(Document):
    filename: str
    uploaded_by: Actor
    uploaded_at: datetime
    credits: int
    matched: int
    unmatched: int
    duplicates: int
    debits_skipped: int
    unreadable: int


# ---------------------------------------------------------------- collections


class _Documents(Collection[BillingDocument]):
    """billing_documents: inserted once, never changed or deleted by the app."""

    def _refuse(self, *_: Any, **__: Any) -> Any:
        raise DocumentImmutable("A document cannot be changed after it is issued; refund it to reverse it")

    set = update_one = update_many = find_one_and_update = bulk_write = delete_many = _refuse  # type: ignore[assignment]


def accounts(db: Database) -> Collection[BillingAccount]:
    return Collection(db, ACCOUNTS, BillingAccount)


def requests(db: Database) -> Collection[PaymentRequest]:
    return Collection(db, REQUESTS, PaymentRequest)


def credits(db: Database) -> Collection[BankCredit]:
    return Collection(db, CREDITS, BankCredit)


def payments(db: Database) -> Collection[Payment]:
    return Collection(db, PAYMENTS, Payment)


def documents(db: Database) -> _Documents:
    return _Documents(db, DOCUMENTS, BillingDocument)


def statements(db: Database) -> Collection[BankStatement]:
    return Collection(db, STATEMENTS, BankStatement)


# ---------------------------------------------------------------- settings, dates and money


def configured() -> bool:
    return bool(settings.billing_upi_id.strip() and settings.billing_payee_name.strip())


def _require_configured() -> None:
    if not configured():
        raise BillingNotConfigured("Payments to NearShop are not set up yet (NEARSHOP_BILLING_UPI_ID and "
                                   "NEARSHOP_BILLING_PAYEE_NAME)")


def financial_year(at: datetime) -> str:
    """The Indian financial year (1 April to 31 March, IST) of a naive UTC time: 2027-28."""
    day = leads.ist_day(at)
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def gst_mode_at(at: datetime) -> GstMode:
    """The GST position at a time: registered from the IST day NEARSHOP_BILLING_GST_REGISTERED_FROM, if set so."""
    since = settings.billing_gst_registered_from
    if settings.billing_gst_mode == GstMode.REGISTERED and since is not None and at >= leads.ist_start(since):
        return GstMode.REGISTERED
    return GstMode.UNREGISTERED


def gst_amount(taxable_paise: int, mode: GstMode) -> int:
    if mode != GstMode.REGISTERED:
        return 0
    return (taxable_paise * settings.billing_gst_rate_pct + 50) // 100  # rounded half up, in paise


def gst_split(taxable_paise: int, mode: GstMode, payer_state: str) -> dict[str, int]:
    """CGST and SGST for a payer in the supplier's state (Karnataka), IGST otherwise; all zero when unregistered."""
    gst = gst_amount(taxable_paise, mode)
    if mode != GstMode.REGISTERED:
        return {"gst_rate_pct": 0, "cgst_paise": 0, "sgst_paise": 0, "igst_paise": 0}
    if payer_state == settings.billing_state_code:
        return {"gst_rate_pct": settings.billing_gst_rate_pct, "cgst_paise": gst // 2, "sgst_paise": gst - gst // 2,
                "igst_paise": 0}
    return {"gst_rate_pct": settings.billing_gst_rate_pct, "cgst_paise": 0, "sgst_paise": 0, "igst_paise": gst}


def rupees(paise: int) -> str:
    return f"{Decimal(paise) / 100:.2f}"


def working_days_since(day: date, today: date, holidays: list[date] | None = None) -> int:
    """Working days (Monday to Friday, except bank holidays) after `day` up to and including `today`."""
    off = set(settings.billing_holiday_list if holidays is None else holidays)
    count, current = 0, day
    while current < today:
        current += timedelta(days=1)
        count += current.weekday() < 5 and current not in off
    return count


# ---------------------------------------------------------------- billing accounts


def _shop_address(shop: Shop) -> str:
    return ", ".join(p for p in (shop.address_line, shop.locality, shop.city, shop.pincode) if p)


def account_for_owner(db: Database, owner_id: int, shop: Shop | None = None) -> BillingAccount:
    """The owner's billing account, made from the shop's name and address the first time (outside any transaction,
    so two requests at once make one account)."""
    found = accounts(db).find_one({"kind": AccountKind.SHOP_OWNER.value, "contact_user_id": owner_id})
    if found is not None:
        return found
    if shop is None:
        shop = db.shops.find_one({"owner_id": owner_id}, sort=[("_id", 1)])
        if shop is None:
            raise NotFound("Shop not found")
    account = BillingAccount(legal_name=shop.name, address=_shop_address(shop), state_code=settings.billing_state_code,
                             contact_user_id=owner_id)
    try:
        return accounts(db).insert(account)
    except DuplicateKeyError:
        found = accounts(db).find_one({"kind": AccountKind.SHOP_OWNER.value, "contact_user_id": owner_id})
        assert found is not None
        return found


def update_account(db: Database, owner_id: int, *, legal_name: str, address: str, state_code: str,
                   gstin: str | None, shop: Shop | None = None) -> BillingAccount:
    """Correct the owner's billing account. Documents already issued keep the details they were issued with."""
    gstin = (gstin or "").strip().upper() or None
    if gstin is not None and (not GSTIN_PATTERN.match(gstin) or gstin[:2] != state_code):
        raise AppError("The GSTIN is 15 characters and starts with the state code", code="invalid_gstin",
                       status_code=422)
    account = account_for_owner(db, owner_id, shop)
    return accounts(db).set(account, legal_name=legal_name, address=address, state_code=state_code, gstin=gstin)


def _party(account: BillingAccount) -> Party:
    return Party(legal_name=account.legal_name, address=account.address, state_code=account.state_code,
                 gstin=account.gstin)


def _supplier(draft: bool) -> Party:
    name, address = settings.billing_legal_name.strip(), settings.billing_address.strip()
    if not draft and not (name and address):
        raise BillingNotConfigured("NEARSHOP_BILLING_LEGAL_NAME and NEARSHOP_BILLING_ADDRESS are needed to issue "
                                   "documents")
    return Party(legal_name=name or "[NearShop's legal name]", address=address or "[NearShop's registered address]",
                 state_code=settings.billing_state_code, gstin=settings.billing_gstin.strip() or None)


# ---------------------------------------------------------------- payment requests


def _quote(db: Database, shop: Shop, plan_code: PlanCode | str, period: Period | str,
           now: datetime) -> dict[str, Any]:
    """What the shop would pay for the plan now: the kind of request, the subscription it renews, the plan row, the
    amount before GST and whether it is the founding price. Raises BillingRefused when it cannot be bought."""
    code, period = PlanCode(plan_code), Period(period)
    if period != Period.YEAR:
        raise BillingRefused("Monthly plans are paid by UPI Autopay; a payment request is for a year")
    plan = plans.plan_in_force(db, code, now)
    if code == PlanCode.FREE or not plan.on_sale or plan.price_year_paise is None:
        raise BillingRefused(f"{plan.name} cannot be bought")
    live = plans.subscriptions(db).find_one({"shop_ids": shop.id, "status": {"$in": [
        SubscriptionStatus.ACTIVE.value, SubscriptionStatus.ADVANCE.value]}})
    if live is not None and live.status == SubscriptionStatus.ADVANCE:
        raise BillingRefused("This shop has already paid in advance; its plan starts on "
                             f"{settings.billing_starts_on:%d %b %Y}")
    if live is not None:
        if (live.plan_code, live.period) != (code, period):
            raise BillingRefused(f"This shop is on {live.plan_code.value} by the {live.period.value}; a payment "
                                 f"request renews the same plan")
        return {"kind": RequestKind.RENEWAL, "subscription_id": live.id, "plan": plan,
                "amount": plan.price_year_paise, "founding": False}
    kind = RequestKind.ADVANCE if now < plans.billing_start() else RequestKind.NEW
    founding = (code == PlanCode.GROWTH and plan.founding_year_paise is not None
                and plans.founding_eligibility(db, shop)["eligible"])
    return {"kind": kind, "subscription_id": None, "plan": plan, "founding": founding,
            "amount": plan.founding_year_paise if founding else plan.price_year_paise}


def _new_request(db: Database, shop: Shop, account: BillingAccount, quote: dict[str, Any], actor: Actor,
                 now: datetime, *, request_id: int | None = None, gst_at: datetime | None = None) -> PaymentRequest:
    """The request for a quote (_quote). GST follows the position at gst_at (default now: when it will be paid)."""
    mode = gst_mode_at(gst_at or now)
    gst = gst_amount(quote["amount"], mode)
    req = PaymentRequest(
        id=request_id or db.next_id(REQUESTS), billing_account_id=account.id, owner_id=shop.owner_id, shop_id=shop.id,
        plan_code=quote["plan"].code, plan_version=quote["plan"].version, period=Period.YEAR, kind=quote["kind"],
        subscription_id=quote["subscription_id"], founding=quote["founding"], amount_paise=quote["amount"],
        gst_mode=mode, gst_paise=gst, total_paise=quote["amount"] + gst, created_by=actor, created_at=now,
        updated_at=now)
    req.number = f"{REQUEST_PREFIX}{req.id:06d}"
    return req


def create_request(db: Database, shop: Shop, *, plan_code: PlanCode | str, period: Period | str = Period.YEAR,
                   actor: Actor, reason: str | None = None, now: datetime | None = None) -> PaymentRequest:
    """A payment request for a plan year, by the owner or an admin for them (with the admin's reason, kept on the
    request). The shop's other open requests are cancelled (a payment that still names one goes to the unmatched
    queue)."""
    _require_configured()
    now = now or utcnow()
    account = account_for_owner(db, shop.owner_id, shop)

    def write() -> PaymentRequest:
        req = _new_request(db, shop, account, _quote(db, shop, plan_code, period, now), actor, now)
        req.reason = reason
        requests(db).update_many({"shop_id": shop.id, "status": RequestStatus.OPEN.value},
                                 {"$set": {"status": RequestStatus.CANCELLED.value, "updated_at": now}})
        return requests(db).insert(req)

    return db.transaction(write)


def request_out(req: PaymentRequest) -> dict[str, Any]:
    """A request as the owner pays it: NearShop's UPI ID and payee name, the amount and the note."""
    return {"id": req.id, "number": req.number, "shop_id": req.shop_id, "purpose": req.purpose.value,
            "plan_code": req.plan_code.value, "plan_version": req.plan_version, "period": req.period.value,
            "kind": req.kind.value, "founding": req.founding, "amount_paise": req.amount_paise,
            "gst_mode": req.gst_mode.value, "gst_paise": req.gst_paise, "total_paise": req.total_paise,
            "status": req.status.value, "created_at": _iso(req.created_at), "payment_id": req.payment_id,
            "created_by": req.created_by.type, "reason": req.reason,
            "pay_to": {"upi_id": settings.billing_upi_id, "payee_name": settings.billing_payee_name,
                       "amount": rupees(req.total_paise), "note": req.number}}


# ---------------------------------------------------------------- documents


def _content_hash(doc: BillingDocument) -> str:
    body = doc.model_dump(mode="json", by_alias=True, exclude={"content_hash"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_document(doc: BillingDocument) -> bool:
    """Whether the stored document is exactly as it was issued."""
    return bool(doc.content_hash) and doc.content_hash == _content_hash(doc)


def _format_for(doc_type: DocType) -> tuple[str, bool, str]:
    """The format label, whether it is the draft, and the wording for a document type. Production never issues a
    draft; an approved format needs the CA's wording for the type."""
    fmt = settings.billing_document_format.strip() or DRAFT_FORMAT
    draft = fmt == DRAFT_FORMAT
    if draft and settings.environment == "production":
        raise BillingNotConfigured("Documents are still in the draft format; production issues them only in the "
                                   "format the CA approved (B22)", code="document_format_not_approved")
    wording = getattr(settings, f"billing_wording_{doc_type.value}").strip()
    if not draft and not wording:
        raise BillingNotConfigured(f"The CA's approved wording for a {doc_type.value.replace('_', ' ')} is not set "
                                   f"(NEARSHOP_BILLING_WORDING_{doc_type.value.upper()})")
    return fmt, draft, wording or WORDING_PENDING


def ensure_series(db: Database, fy: str) -> None:
    """Create the financial year's counter outside any transaction (a transaction's snapshot would not see it)."""
    db.mongo["counters"].update_one({"_id": SERIES_PREFIX + fy}, {"$setOnInsert": {"seq": 0}}, upsert=True)


def next_number(db: Database, fy: str) -> tuple[int, str]:
    """The next number of the financial year's series, in db's transaction: one increment of one counter document,
    so concurrent payments conflict and retry, and an aborted one gives its number back."""
    doc = db.mongo["counters"].find_one_and_update({"_id": SERIES_PREFIX + fy}, {"$inc": {"seq": 1}},
                                                   return_document=ReturnDocument.AFTER, session=db.session)
    if doc is None:
        raise RuntimeError(f"the document series for {fy} was not created (ensure_series)")
    seq = int(doc["seq"])
    number = f"{settings.billing_number_prefix}/{fy[2:4]}-{fy[5:7]}/{seq:06d}"
    if seq > 999_999 or len(number) > MAX_NUMBER_LENGTH:
        raise BillingRefused(f"The {fy} document series is full")
    return seq, number


def _description(req: PaymentRequest, kind: RequestKind, plan_name: str) -> str:
    """kind is what the payment became (_settle), which may differ from the request's: an advance request paid on or
    after billing starts is a shop-paid subscription."""
    what = {RequestKind.ADVANCE: "advance payment for", RequestKind.NEW: "subscription to",
            RequestKind.RENEWAL: "renewal of"}[kind]
    founding = " (founding offer, first year)" if req.founding else ""
    return f"NearShop {plan_name} plan, one year: {what} the plan{founding}"


TAX_FIELDS = ("gst_rate_pct", "cgst_paise", "sgst_paise", "igst_paise")


def _issue(db: Database, doc_type: DocType, *, payment: Payment, recipient: Party, description: str,
           taxable: int, split: dict[str, int], issued_at: datetime, payment_date: datetime,
           original: BillingDocument | None = None, refund_reference: str | None = None) -> BillingDocument:
    """Issue one document in db's transaction: the next number of the series and an immutable snapshot. split is
    gst_split's result (a refund copies the original's)."""
    fmt, draft, wording = _format_for(doc_type)
    fy = financial_year(issued_at)
    seq, number = next_number(db, fy)
    registered = doc_type in (DocType.TAX_INVOICE, DocType.CREDIT_NOTE)
    total = taxable + split["cgst_paise"] + split["sgst_paise"] + split["igst_paise"]
    doc = BillingDocument(
        id=db.next_id(DOCUMENTS), number=number, financial_year=fy, seq=seq, type=doc_type, issued_at=issued_at,
        payment_date=payment_date, format=fmt, draft_line=DRAFT_LINE if draft else None, wording=wording,
        supplier=_supplier(draft), recipient=recipient,
        place_of_supply=f"{recipient.state_code} {STATE_NAMES.get(recipient.state_code, '')}".strip(),
        description=description, sac=(settings.billing_sac.strip() or None) if registered else None,
        taxable_paise=taxable, total_paise=total, payment_id=payment.id, request_number=payment.request_number,
        upi_reference=payment.upi_reference, original_document_id=original.id if original else None,
        original_number=original.number if original else None, refund_reference=refund_reference, **split)
    doc.content_hash = _content_hash(doc)
    return documents(db).insert(doc)


def document_out(doc: BillingDocument) -> dict[str, Any]:
    return {**doc.model_dump(mode="json", exclude={"id"}), "id": doc.id, "issued_at": _iso(doc.issued_at),
            "payment_date": _iso(doc.payment_date), "intact": verify_document(doc)}


# ---------------------------------------------------------------- matching


def _settle(db: Database, credit: BankCredit, req: PaymentRequest, *, actor: Actor, how: CreditSource, reason: str,
            now: datetime) -> Payment:
    """Inside db's transaction: the payment, its subscription change and its document for a credit and a request.
    Raises AppError (the whole transaction is rolled back) when they do not fit."""
    if req.status != RequestStatus.OPEN:
        raise BillingRefused(f"Request {req.number} is {req.status.value}")
    if credit.amount_paise != req.total_paise:
        raise BillingRefused(f"The amount {rupees(credit.amount_paise)} is not the request's "
                             f"{rupees(req.total_paise)}")
    mode = gst_mode_at(credit.credited_at)
    if mode != req.gst_mode:
        raise BillingRefused(f"The GST position changed since request {req.number} was made")
    note = f"Payment {req.number} ({credit.upi_reference}): {reason}"
    sub: Subscription | None
    if req.kind == RequestKind.RENEWAL and req.subscription_id is not None:
        kind = RequestKind.RENEWAL
        sub = plans.renew(db, req.subscription_id, actor=actor, reason=note, paid_at=credit.credited_at, now=now)
    elif credit.credited_at < plans.billing_start():
        # The money arrived before billing started, so it is an advance, however late it is matched (a payment on
        # 31 Jul matched from the statement on 2 Aug). Matched after the start, it is recorded as of its payment
        # date (an advance cannot be taken any later), and the subscriptions job activates it on its next tick.
        kind = RequestKind.ADVANCE
        sub = plans.create_subscription(db, shop_ids=[req.shop_id], plan_code=req.plan_code, period=req.period,
                                        source=SubscriptionSource.ADVANCE, actor=actor, reason=note,
                                        paid_at=credit.credited_at,
                                        now=now if now < plans.billing_start() else credit.credited_at)
    else:
        kind = RequestKind.NEW
        sub = plans.create_subscription(db, shop_ids=[req.shop_id], plan_code=req.plan_code, period=req.period,
                                        source=SubscriptionSource.SHOP_PAID, actor=actor, reason=note,
                                        paid_at=credit.credited_at, now=now)
    if sub is None or sub.price_paise != req.amount_paise:
        raise BillingRefused(f"The price of request {req.number} has changed (the founding offer may be full, or a "
                             f"new catalogue is in force)")
    pay = Payment(id=db.next_id(PAYMENTS), request_id=req.id, request_number=req.number,
                  billing_account_id=req.billing_account_id, shop_id=req.shop_id, credit_id=credit.id,
                  upi_reference=credit.upi_reference, amount_paise=credit.amount_paise, paid_at=credit.credited_at,
                  matched_at=now, matched_by=actor, match=how, reason=reason, subscription_id=sub.id)
    account = accounts(db).get(req.billing_account_id)
    if account is None:
        raise NotFound("Billing account not found")
    plan = plans.plans(db).get(sub.plan_id)
    doc = _issue(db, DocType.TAX_INVOICE if mode == GstMode.REGISTERED else DocType.RECEIPT, payment=pay,
                 recipient=_party(account),
                 description=_description(req, kind, plan.name if plan else req.plan_code.value),
                 taxable=req.amount_paise, split=gst_split(req.amount_paise, mode, account.state_code), issued_at=now,
                 payment_date=credit.credited_at)
    if doc.total_paise != credit.amount_paise:
        raise BillingRefused(f"The document's total {rupees(doc.total_paise)} is not the amount received")
    pay.document_id = doc.id
    payments(db).insert(pay)
    credits(db).set(credit, status=CreditStatus.MATCHED, payment_id=pay.id, note=None,
                    request_number=credit.request_number or req.number)
    requests(db).set(req, status=RequestStatus.PAID, payment_id=pay.id, updated_at=now)
    notify(db, req.owner_id, KIND_PAYMENT_RECEIVED, "Payment received",
           f"We received ₹{rupees(credit.amount_paise)} for {req.number}. Your {doc.type.value.replace('_', ' ')} is "
           f"{doc.number}.", BILLING_LINK)
    return pay


def _prepare(db: Database, now: datetime) -> None:
    """The counter documents a matching transaction increments, created before it begins."""
    plans.ensure_founding_counter(db)
    ensure_series(db, financial_year(now))


def _match(db: Database, credit_id: int, request_number: str, *, actor: Actor, how: CreditSource, reason: str,
           now: datetime) -> Payment:
    def write() -> Payment:
        credit = credits(db).get(credit_id)
        if credit is None:
            raise NotFound("Credit not found")
        if credit.status != CreditStatus.UNMATCHED:
            raise BillingRefused(f"Credit {credit.upi_reference} is already {credit.status.value}")
        req = requests(db).find_one({"number": request_number})
        if req is None:
            raise BillingRefused(f"There is no payment request {request_number}")
        return _settle(db, credit, req, actor=actor, how=how, reason=reason, now=now)

    _prepare(db, now)
    return db.transaction(write)


def _request_number(narration: str) -> str | None:
    found = REQUEST_NUMBER.search(narration or "")
    return f"{REQUEST_PREFIX}{int(found.group(1)):06d}" if found else None


def match_by_hand(db: Database, *, upi_reference: str, request_number: str, reason: str, actor: Actor,
                  amount_paise: int | None = None, credited_on: date | None = None,
                  now: datetime | None = None) -> Payment:
    """An admin matches a credit to a request by its UPI reference, with a reason. A credit not yet on any uploaded
    statement is recorded from the amount and date given, in the same transaction (nothing is kept if it fails)."""
    now = now or utcnow()
    reference = upi_reference.strip()
    number = _request_number(request_number) or request_number.strip().upper()
    existing = credits(db).find_one({"upi_reference": reference})
    if existing is not None:
        return _match(db, existing.id, number, actor=actor, how=CreditSource.MANUAL, reason=reason, now=now)
    if amount_paise is None or credited_on is None:
        raise NotFound(f"No credit {reference} has been uploaded: give its amount and date to record it")
    credit_id = db.next_id(CREDITS)

    def write() -> Payment:
        credit = credits(db).insert(BankCredit(
            id=credit_id, upi_reference=reference, credited_at=leads.ist_start(credited_on), amount_paise=amount_paise,
            narration=f"Recorded by hand: {reason}", source=CreditSource.MANUAL, request_number=number,
            created_at=now))
        req = requests(db).find_one({"number": number})
        if req is None:
            raise BillingRefused(f"There is no payment request {number}")
        return _settle(db, credit, req, actor=actor, how=CreditSource.MANUAL, reason=reason, now=now)

    _prepare(db, now)
    try:
        return db.transaction(write)
    except DuplicateKeyError:
        raise BillingRefused(f"Credit {reference} was recorded at the same moment; match it again") from None


def return_credit(db: Database, credit_id: int, *, reason: str, bank_reference: str, actor: Actor,
                  now: datetime | None = None) -> BankCredit:
    """An unmatched credit sent back to the payer by bank transfer: it leaves the unmatched queue."""
    now = now or utcnow()

    def write() -> BankCredit:
        credit = credits(db).get(credit_id)
        if credit is None:
            raise NotFound("Credit not found")
        if credit.status != CreditStatus.UNMATCHED:
            raise BillingRefused(f"Credit {credit.upi_reference} is {credit.status.value}")
        return credits(db).set(credit, status=CreditStatus.RETURNED, returned=CreditReturn(
            at=now, by=actor, reason=reason, bank_reference=bank_reference))

    return db.transaction(write)


# ---------------------------------------------------------------- bank statements

# The statement layout (docs/runbooks/billing.md): a CSV export with a header row naming at least a date, a narration,
# a reference and a credit column, in any order, after any number of preamble lines. Accepted header names:
COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("date", "txn date", "transaction date", "value date", "tran date"),
    "narration": ("narration", "description", "particulars", "remarks", "transaction remarks"),
    "reference": ("upi reference", "utr", "utr no", "ref no", "reference", "reference no", "chq/ref no",
                  "chq / ref no", "ref no./cheque no.", "cheque/ref no"),
    "credit": ("credit", "deposit", "deposits", "credit amount", "cr amount", "deposit amt"),
    "debit": ("debit", "withdrawal", "withdrawals", "debit amount", "dr amount", "withdrawal amt"),
}
REQUIRED_COLUMNS = ("date", "narration", "reference", "credit")
DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y", "%d-%m-%y", "%d-%b-%y")
UPI_RRN = re.compile(r"(?<!\d)(\d{12})(?!\d)")  # a UPI transaction's 12-digit reference, when only the narration has it
HEADER_SEARCH_ROWS = 40


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", (cell or "").strip().lower().replace("﻿", ""))


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_paise(value: str) -> int | None:
    text = (value or "").strip().replace(",", "").replace("₹", "").replace("INR", "").strip()
    if text.upper().endswith("CR"):
        text = text[:-2].strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_statement(data: bytes) -> dict[str, Any]:
    """The credit rows of a statement export: [{credited_on, narration, reference, amount_paise}], with counts of the
    debit rows skipped and the rows that could not be read."""
    if len(data) > settings.import_max_mb * 1024 * 1024:
        raise AppError(f"The statement is larger than {settings.import_max_mb} MB", code="file_too_large",
                       status_code=413)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    rows = list(csv.reader(io.StringIO(text)))
    columns: dict[str, int] = {}
    start = 0
    for i, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        names = [_norm_header(c) for c in row]
        found: dict[str, int] = {}
        for key, aliases in COLUMNS.items():  # the first alias present wins (Txn Date over Value Date)
            hit = next((names.index(a) for a in aliases if a in names), None)
            if hit is not None:
                found[key] = hit
        if all(k in found for k in REQUIRED_COLUMNS):
            columns, start = found, i + 1
            break
    if not columns:
        raise AppError("No header row with a date, narration, reference and credit column was found; see the layout "
                       "in docs/runbooks/billing.md", code="statement_layout", status_code=422)
    body = rows[start:]
    if len(body) > settings.import_max_rows:
        raise AppError(f"The statement has more than {settings.import_max_rows} rows; upload it in parts",
                       code="statement_too_long", status_code=422)

    def cell(row: list[str], key: str) -> str:
        i = columns.get(key)
        return row[i] if i is not None and i < len(row) else ""

    credit_rows: list[dict[str, Any]] = []
    debits = unreadable = 0
    for row in body:
        if not any((c or "").strip() for c in row):
            continue
        amount = _parse_paise(cell(row, "credit"))
        if not amount:
            if _parse_paise(cell(row, "debit")):
                debits += 1
            else:
                unreadable += 1  # neither a credit nor a debit: a total or a footer line
            continue
        day = _parse_date(cell(row, "date"))
        narration = cell(row, "narration").strip()
        reference = cell(row, "reference").strip()
        if not reference or set(reference) <= {"0", "-"}:
            rrn = UPI_RRN.search(narration)
            reference = rrn.group(1) if rrn else ""
        if day is None or amount < 0 or not reference:
            unreadable += 1
            continue
        credit_rows.append({"credited_on": day, "narration": narration[:500], "reference": reference[:64],
                            "amount_paise": amount})
    return {"credits": credit_rows, "debits_skipped": debits, "unreadable": unreadable}


def import_statement(db: Database, data: bytes, filename: str, *, actor: Actor, now: datetime | None = None,
                     ) -> dict[str, Any]:
    """Record every new credit of a statement and match each one by its request number and amount. Returns the
    counts and, per credit, what happened: matched, unmatched (with the reason) or duplicate."""
    now = now or utcnow()
    parsed = parse_statement(data)
    statement_id = db.next_id(STATEMENTS)
    results: list[dict[str, Any]] = []
    for row in parsed["credits"]:
        out = {"upi_reference": row["reference"], "amount_paise": row["amount_paise"],
               "credited_on": row["credited_on"].isoformat()}
        number = _request_number(row["narration"])
        credit = BankCredit(id=db.next_id(CREDITS), upi_reference=row["reference"],
                            credited_at=leads.ist_start(row["credited_on"]), amount_paise=row["amount_paise"],
                            narration=row["narration"], source=CreditSource.STATEMENT, statement_id=statement_id,
                            request_number=number, created_at=now)
        try:
            credits(db).insert(credit)
        except DuplicateKeyError:
            seen = credits(db).find_one({"upi_reference": row["reference"]})
            results.append({**out, "result": "duplicate", "credit_id": seen.id if seen else None,
                            "status": seen.status.value if seen else None})
            continue
        out["credit_id"] = credit.id
        if number is None:
            credits(db).set(credit, note="No payment request number in the narration")
            results.append({**out, "result": "unmatched", "reason": credit.note})
            continue
        try:
            pay = _match(db, credit.id, number, actor=actor, how=CreditSource.STATEMENT,
                         reason=f"Bank statement {filename}", now=now)
        except AppError as exc:
            credits(db).update_one({"_id": credit.id, "status": CreditStatus.UNMATCHED.value},
                                   {"$set": {"note": exc.message[:300]}})
            results.append({**out, "result": "unmatched", "request_number": number, "reason": exc.message})
            continue
        results.append({**out, "result": "matched", "request_number": number, "payment_id": pay.id})
    counts = {k: sum(r["result"] == k for r in results) for k in ("matched", "unmatched", "duplicate")}
    statements(db).insert(BankStatement(
        id=statement_id, filename=filename[:200], uploaded_by=actor, uploaded_at=now, credits=len(results),
        matched=counts["matched"], unmatched=counts["unmatched"], duplicates=counts["duplicate"],
        debits_skipped=parsed["debits_skipped"], unreadable=parsed["unreadable"]))
    log.info("bank statement %d: %d credits, %d matched, %d unmatched, %d duplicates", statement_id, len(results),
             counts["matched"], counts["unmatched"], counts["duplicate"])
    return {"statement_id": statement_id, "credits": len(results), "matched": counts["matched"],
            "unmatched": counts["unmatched"], "duplicates": counts["duplicate"],
            "debits_skipped": parsed["debits_skipped"], "unreadable": parsed["unreadable"], "rows": results}


# ---------------------------------------------------------------- refunds


def _renewal_term(db: Database, sub_id: int, request_number: str) -> timedelta:
    """How far a renewal paid by request_number moved the subscription's end (its renewed event): the term to take
    off when that renewal is refunded. After a lapse it is from the old end, so taking it off restores that end."""
    event = plans.events(db).find_one({"subscription_id": sub_id, "kind": "renewed",
                                       "reason": {"$regex": f"^Payment {re.escape(request_number)} "}})
    if event is None or event.ends_at_before is None or event.ends_at_after is None:
        raise BillingRefused(f"The renewal paid by {request_number} was not found on subscription {sub_id}")
    return event.ends_at_after - event.ends_at_before


def refund_payment(db: Database, payment_id: int, *, reason: str, bank_reference: str, actor: Actor,
                   now: datetime | None = None) -> Payment:
    """Refund a payment in full, sent back by bank transfer (bank_reference), with a refund receipt or a credit note
    that references the original document. The subscription loses only this payment's term (module docstring): it
    becomes refunded when no other payment on it is still matched, else the renewal's term is taken off. A payment
    with a later matched payment on the same subscription is refused until that one is refunded."""
    now = now or utcnow()
    ensure_series(db, financial_year(now))

    def write() -> Payment:
        pay = payments(db).get(payment_id)
        if pay is None:
            raise NotFound("Payment not found")
        if pay.status != PaymentStatus.MATCHED:
            raise BillingRefused(f"Payment {pay.id} is already {pay.status.value}")
        original = documents(db).get(pay.document_id)
        if original is None:
            raise NotFound("The payment's document was not found")
        others = payments(db).find({"subscription_id": pay.subscription_id, "status": PaymentStatus.MATCHED.value,
                                    "_id": {"$ne": pay.id}}, sort=[("_id", 1)])
        later = [p for p in others if p.id > pay.id]
        if later:
            raise BillingRefused(f"Payment {later[-1].id} ({later[-1].request_number}) paid a later term of the same "
                                 f"subscription: refund it first", code="refund_later_first")
        note = f"Refund of payment {pay.request_number}: {reason}"
        partial = bool(others)

        if not partial:
            def apply(sub: Subscription) -> dict[str, Any]:
                if sub.status == SubscriptionStatus.REFUNDED:
                    raise BillingRefused(f"Subscription {sub.id} is already refunded")
                return {"status": SubscriptionStatus.REFUNDED}

            sub = plans._change(db, pay.subscription_id, "refunded", actor, note, now, apply)
        else:
            term = _renewal_term(db, pay.subscription_id, pay.request_number)

            def apply(sub: Subscription) -> dict[str, Any]:
                if sub.status == SubscriptionStatus.REFUNDED:
                    raise BillingRefused(f"Subscription {sub.id} is already refunded")
                ends_at, paid_until = sub.ends_at - term, min(sub.paid_until - term, sub.ends_at - term)
                terms = [PaidTerm(start=t.start, end=min(t.end, paid_until)) for t in sub.paid_terms
                         if t.start < paid_until]
                fields: dict[str, Any] = {"ends_at": ends_at, "paid_until": paid_until, "reminded": [],
                                          **plans._cursor(terms, sub.quarters_done if terms else 0)}
                if sub.status == SubscriptionStatus.ACTIVE and ends_at <= now:
                    fields["status"] = SubscriptionStatus.LAPSED
                return fields

            sub = plans._change(db, pay.subscription_id, "renewal_refunded", actor, note, now, apply)
        doc_type = DocType.CREDIT_NOTE if original.type == DocType.TAX_INVOICE else DocType.REFUND_RECEIPT
        doc = _issue(db, doc_type, payment=pay, recipient=original.recipient,
                     description=f"Refund of {original.number}: {original.description}",
                     taxable=original.taxable_paise, split={f: getattr(original, f) for f in TAX_FIELDS},
                     issued_at=now, payment_date=now, original=original, refund_reference=bank_reference)
        payments(db).set(pay, status=PaymentStatus.REFUNDED, refund=Refund(
            at=now, by=actor, reason=reason, bank_reference=bank_reference, document_id=doc.id))
        req = requests(db).get(pay.request_id)
        if req is not None:
            requests(db).set(req, status=RequestStatus.REFUNDED, updated_at=now)
            plan_now = (f"Your plan now runs until {plans._date(sub.ends_at)}." if partial and sub is not None
                        and sub.ends_at > now else "Your shop is on the Free plan, with every stock tool and the "
                        "same place in search.")
            notify(db, req.owner_id, KIND_REFUNDED, "Payment refunded",
                   f"₹{rupees(pay.amount_paise)} for {pay.request_number} is being returned to your bank account "
                   f"(reference {bank_reference}). Your {doc.type.value.replace('_', ' ')} is {doc.number}. "
                   f"{plan_now}", BILLING_LINK)
        return pay

    return db.transaction(write)


# ---------------------------------------------------------------- the pilot ledger


def import_pilot_ledger(db: Database, rows: list[dict[str, Any]], *, reason: str, actor: Actor,
                        now: datetime | None = None) -> dict[str, Any]:
    """The advance payments R1 recorded by hand before this release ({paid_on, shop_id, amount_paise,
    upi_reference}): each becomes a matched payment, an advance subscription and a receipt showing the original
    payment date, in order of payment, all in one transaction. A UPI reference seen before (in the file, or already
    recorded) refuses the whole import, as does a row whose amount is not the advance's price."""
    now = now or utcnow()
    _require_configured()
    if now >= plans.billing_start():
        raise BillingRefused("Billing has started: the pilot ledger holds advances only, imported before it starts")
    today = leads.ist_day(now)
    future = [i + 1 for i, r in enumerate(rows) if r["paid_on"] > today]
    if future:
        raise BillingRefused(f"Row(s) {', '.join(map(str, future))}: the payment date is later than today "
                             f"({today:%d %b %Y}, IST)", code="ledger_future_date")
    refs = [str(r["upi_reference"]).strip() for r in rows]
    repeated = sorted({r for r in refs if refs.count(r) > 1})
    known = sorted({c["upi_reference"] for c in credits(db).find_raw({"upi_reference": {"$in": refs}},
                                                                    {"upi_reference": 1})})
    if repeated or known:
        raise BillingRefused(f"UPI reference(s) already recorded or repeated: {', '.join(repeated + known)}",
                             code="repeated_reference")
    ordered = sorted(enumerate(rows), key=lambda r: (r[1]["paid_on"], r[0]))
    shops = db.shops.by_ids(int(r["shop_id"]) for r in rows)
    missing = sorted({int(r["shop_id"]) for r in rows} - set(shops))
    if missing:
        raise NotFound(f"Shop(s) not found: {', '.join(map(str, missing))}")
    accounts_by_owner = {s.owner_id: account_for_owner(db, s.owner_id, s) for s in shops.values()}
    _prepare(db, now)
    ids = [(db.next_id(CREDITS), db.next_id(REQUESTS)) for _ in rows]

    def write() -> list[Payment]:
        done: list[Payment] = []
        for (index, row), (credit_id, request_id) in zip(ordered, ids):
            shop = shops[int(row["shop_id"])]
            paid_at = leads.ist_start(row["paid_on"])
            quote = _quote(db, shop, PlanCode.GROWTH, Period.YEAR, now)
            req = _new_request(db, shop, accounts_by_owner[shop.owner_id], quote, actor, now, request_id=request_id,
                               gst_at=paid_at)
            if int(row["amount_paise"]) != req.total_paise:
                raise BillingRefused(f"Row {index + 1} (shop {shop.id}): {rupees(int(row['amount_paise']))} is not the "
                                     f"advance's price {rupees(req.total_paise)}", code="ledger_amount")
            requests(db).insert(req)
            credit = credits(db).insert(BankCredit(
                id=credit_id, upi_reference=str(row["upi_reference"]).strip(), credited_at=paid_at,
                amount_paise=int(row["amount_paise"]), narration=f"Pilot ledger: {reason}"[:500],
                source=CreditSource.PILOT_LEDGER, request_number=req.number, created_at=now))
            done.append(_settle(db, credit, req, actor=actor, how=CreditSource.PILOT_LEDGER, reason=reason, now=now))
        return done

    try:
        done = db.transaction(write)
    except DuplicateKeyError:
        raise BillingRefused("A UPI reference in the ledger was recorded at the same moment",
                             code="repeated_reference") from None
    return {"imported": len(done), "payments": [payment_out(p) for p in done]}


# ---------------------------------------------------------------- the daily reconciliation alert


def alert_unmatched(db: Database, now: datetime | None = None) -> dict[str, int]:
    """Alert every admin, once per credit, about each credit still unmatched NEARSHOP_BILLING_UNMATCHED_ALERT_
    WORKING_DAYS working days after the day it was credited."""
    now = now or utcnow()
    today = leads.ist_day(now)
    waiting = credits(db).find({"status": CreditStatus.UNMATCHED.value, "alerted_at": None},
                               sort=[("credited_at", 1)])
    due = [c for c in waiting if working_days_since(leads.ist_day(c.credited_at), today)
           >= settings.billing_unmatched_alert_working_days]
    if not due:
        return {"unmatched": len(waiting), "alerted": 0}
    admins = [u["_id"] for u in db.users.find_raw({"role": "admin", "is_active": True}, {"_id": 1})]
    alerted = 0
    for credit in due:
        def write(credit_id: int = credit.id) -> bool:
            row = credits(db).find_one({"_id": credit_id, "status": CreditStatus.UNMATCHED.value, "alerted_at": None})
            if row is None:
                return False
            for admin_id in admins:
                notify(db, admin_id, KIND_UNMATCHED, f"Bank credit {row.upi_reference} is still unmatched",
                       f"₹{rupees(row.amount_paise)} credited on {_day(row.credited_at)}: {row.note or 'not matched'}."
                       f" Match it by hand or return it (docs/runbooks/billing.md).",
                       "/admin/billing/credits?status=unmatched")
            credits(db).set(row, alerted_at=now)
            return True

        if db.transaction(write):
            alerted += 1
            log.warning("%s: credit %s of %s paise, credited %s", UNMATCHED_WARNING, credit.id, credit.amount_paise,
                        leads.ist_day(credit.credited_at).isoformat())
    return {"unmatched": len(waiting), "alerted": alerted}


# ---------------------------------------------------------------- views


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() + "Z" if at else None


def _day(at: datetime) -> str:
    day = leads.ist_day(at)
    return f"{day.day} {day:%B %Y}"


def credit_out(c: BankCredit) -> dict[str, Any]:
    return {"id": c.id, "upi_reference": c.upi_reference, "credited_on": leads.ist_day(c.credited_at).isoformat(),
            "amount_paise": c.amount_paise, "narration": c.narration, "source": c.source.value,
            "statement_id": c.statement_id, "request_number": c.request_number, "status": c.status.value,
            "note": c.note, "payment_id": c.payment_id, "alerted_at": _iso(c.alerted_at),
            "returned": c.returned.model_dump(mode="json") if c.returned else None, "created_at": _iso(c.created_at)}


def payment_out(p: Payment) -> dict[str, Any]:
    return {"id": p.id, "request_id": p.request_id, "request_number": p.request_number, "shop_id": p.shop_id,
            "credit_id": p.credit_id, "upi_reference": p.upi_reference, "amount_paise": p.amount_paise,
            "paid_at": _iso(p.paid_at), "matched_at": _iso(p.matched_at), "matched_by": p.matched_by.model_dump(),
            "match": p.match.value, "reason": p.reason, "subscription_id": p.subscription_id,
            "document_id": p.document_id, "status": p.status.value,
            "refund": p.refund.model_dump(mode="json") if p.refund else None}


def account_out(a: BillingAccount) -> dict[str, Any]:
    return {"id": a.id, "kind": a.kind.value, "legal_name": a.legal_name, "address": a.address,
            "state_code": a.state_code, "gstin": a.gstin, "contact_user_id": a.contact_user_id}


def _doc_brief(d: BillingDocument) -> dict[str, Any]:
    return {"id": d.id, "number": d.number, "type": d.type.value, "issued_at": _iso(d.issued_at),
            "total_paise": d.total_paise, "draft": d.draft_line is not None}


def owner_billing(db: Database, shop: Shop) -> dict[str, Any]:
    """The owner's billing screen: the account, open requests (with how to pay), payments and documents."""
    account = accounts(db).find_one({"kind": AccountKind.SHOP_OWNER.value, "contact_user_id": shop.owner_id})
    open_requests = requests(db).find({"shop_id": shop.id, "status": RequestStatus.OPEN.value},
                                      sort=[("_id", -1)], limit=5)
    paid = payments(db).find({"shop_id": shop.id}, sort=[("_id", -1)], limit=50)
    doc_ids = [d for p in paid for d in (p.document_id, p.refund.document_id if p.refund else None) if d]
    docs = documents(db).by_ids(doc_ids)
    return {"configured": configured(), "account": account_out(account) if account else None,
            "gst_mode": gst_mode_at(utcnow()).value, "requests": [request_out(r) for r in open_requests],
            "payments": [{**payment_out(p), "documents": [_doc_brief(docs[d]) for d in
                                                          (p.document_id, p.refund.document_id if p.refund else None)
                                                          if d in docs]} for p in paid]}


def owner_document(db: Database, shop: Shop, document_id: int) -> dict[str, Any]:
    doc = documents(db).get(document_id)
    pay = payments(db).get(doc.payment_id) if doc else None
    if doc is None or pay is None or pay.shop_id != shop.id:
        raise NotFound("Document not found")
    return document_out(doc)

