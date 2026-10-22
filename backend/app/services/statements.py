"""Monthly statements: the shadow bill, the ROI statement and the shops' replies (P9.9; plan section 8.18; business
model section 10.1, step 5; docs/whatsapp-templates.md).

Every active shop hears, once a month, what NearShop did for it, in counts only: never a rupee value of its sales,
which NearShop cannot measure. Its replies are recorded so that the willingness-to-pay readout (B25) can be computed.

Building
--------
The statements job (app/jobs.py), on the 1st of each month at 10:00 IST or on the first tick after it, runs
build_month for the previous IST month (statement_month), under its lease. One statement per active shop: verified
and live (live_shop_filter) with an active listing whose details or stock changed in the last ACTIVE_LISTING_DAYS
(14) days. Nothing is built from demo data: not in the demo environment or with NEARSHOP_DEMO_MODE on, and never for a
shop the seed made (an owner on the demo email domain). Nothing is built for a month before FIRST_MONTH (May 2027:
the first statements go out on 1 Jun 2027).

A statement holds the month's counts from app/services/leads.py month_counts (reservation requests, completed
pickups, delivery orders, call taps and directions taps, with the rest of leads.COUNT_FIELDS), and, while the P9.7
report is shown (demand_report.owner_module), its top TOP_ASKED (3) "asked for near you" items. Then, by the shop's
plan (plans.effective_plan) when the statement is built:

  * shadow_bill: before billing starts (NEARSHOP_BILLING_STARTS_ON), and afterwards for a shop whose plan has no ROI
    statement (Free): ₹0 due, what Growth would cost at the price the shop qualifies for (the founding price while
    plans.founding_eligibility says so: "₹83 a month at the founding price (₹999 for the first year)"), and pay_link,
    the owner's billing page, where a P9.5 payment request for an advance (or, after billing starts, a plan) is made.
    A shop that already paid in advance is told when its plan starts instead, with no link.
  * roi: a shop whose plan has the roi_statement entitlement (Growth and above): the same counts with the quarter's
    customer requests, the plan and its end date, and the quarter's outcome-guarantee progress
    (plans.guarantee_progress).

The statement, its in-app notification (notify, kind statement_free or statement_roi) and, when the owner can get
WhatsApp, its outbox row are written in one transaction. The unique index on (shop_id, month) makes a re-run for the
month skip every shop that has its statement: a statement is never sent twice.

Delivery
--------
The outbox row (app/services/whatsapp.py) carries the approved template's variables (whatsapp.STATEMENT_VARIABLES),
rendered in the owner's language, and quick-reply buttons whose payloads are statement:<id>:<reply>. When the
owner's own service window is open now and for at least whatsapp.WINDOW_MARGIN more, the row goes as a free-form text
inside it instead (route in_window, category service: no charge), which asks for the reply in words (REPLY_WORDS). An
owner without a verified phone or the WhatsApp consent gets the statement in the app only (delivery not_sent).

Each statement keeps its delivery status: not_sent, queued, sent, delivered (or read), failed or cancelled.
sync_delivery copies it from the outbox row on every tick of the statements job (the row itself is removed after 30
days), and the admin summary syncs its month before counting.

Replies
-------
Accurate, Not accurate, I'd pay and Not now (the shadow bill's four buttons; the ROI statement has the first two) are
each stored once per statement in statement_replies (unique on statement_id and kind); a second tap of the same reply
changes nothing. Not accurate asks for a short reason: the owner's next text message (within REASON_WAIT) is stored on
that reply as its reason (REASON_MAX characters). I'd pay makes a P9.5 payment request for Growth by the year
(billing.create_request) and answers with NearShop's UPI ID, the amount and the request number to use as the note,
and the billing page link; when no request can be made (billing not set up, or refused) it answers with the link.
A reply typed inside the window (REPLY_WORDS, English or Kannada) answers the shop's latest statement built in the
last REPLY_WINDOW.

Views
-----
owner_statements gives the shop's statements by month with their counts, and the current month so far
(GET /api/owner/statements); admin_summary gives, for a month, the statements built, sent, delivered and failed, the
replies by kind, and the share of active shops (the statements built) whose answer was Accurate, the B25 readout
measure (GET /api/admin/statements). A shop that answered both Accurate and Not accurate counts by its later answer.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import AppError, NotFound
from app.models import Shop, User
from app.models.billing import Actor, Period, PlanCode, SubscriptionStatus
from app.models.core import live_shop_filter
from app.services import billing, demand_report, leads, plans, whatsapp
from app.services import notification_text as words
from app.services.notifications import notify

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
STATEMENTS = "statements"
REPLIES = "statement_replies"

SHADOW_BILL, ROI = "shadow_bill", "roi"
KINDS = (SHADOW_BILL, ROI)
NOTIFICATION_KINDS = {SHADOW_BILL: "statement_free", ROI: "statement_roi"}
ACCURATE, NOT_ACCURATE, WOULD_PAY, NOT_NOW = "accurate", "not_accurate", "would_pay", "not_now"
REPLY_KINDS = (ACCURATE, NOT_ACCURATE, WOULD_PAY, NOT_NOW)
BUTTONS = {SHADOW_BILL: REPLY_KINDS, ROI: (ACCURATE, NOT_ACCURATE)}
PAYLOAD_PREFIX = "statement:"
DELIVERY = ("not_sent", "queued", "sent", "delivered", "failed", "cancelled")
OPEN_DELIVERY = ("queued", "sent")  # may still change: synced from the outbox row
# An outbox row's status -> the statement's delivery status.
FROM_OUTBOX = {"queued": "queued", "sending": "queued", "sent": "sent", "delivered": "delivered", "read": "delivered",
               "failed": "failed", "cancelled": "cancelled"}

STATEMENT_AT = time(10, 0)  # IST, on the 1st of each month
FIRST_MONTH = (2027, 5)  # the first month stated: sent on 1 Jun 2027 (business model section 10.1, step 5)
ACTIVE_LISTING_DAYS = 14
TOP_ASKED = 3
REASON_MAX = 300
REASON_WAIT = timedelta(hours=24)
REPLY_WINDOW = timedelta(days=35)
KEEP_YEARS = leads.COUNTS_KEEP_YEARS  # business records, kept as long as the lead counts they state
LINK = "/owner/statements"
SYNC_BATCH = 500
# Seeded accounts: x@nearshop.demo and <shop>@shops.nearshop.demo (as app/ai/anomaly.py and admin_tasks.py).
DEMO_EMAIL_PATTERN = r"@([^@]+\.)?nearshop\.demo$"
# A demand item that names money is left out: a statement never states a rupee value beyond its price lines.
_MONEY = re.compile(r"₹|\b(rs|inr|rupees?)\b", re.IGNORECASE)

# A reply typed inside the window, in English or Kannada (the words free_text and roi_text ask for).
REPLY_WORDS: dict[str, str] = {
    "accurate": ACCURATE, "correct": ACCURATE, "ನಿಖರ": ACCURATE,
    "not accurate": NOT_ACCURATE, "inaccurate": NOT_ACCURATE, "not correct": NOT_ACCURATE,
    "ನಿಖರವಾಗಿಲ್ಲ": NOT_ACCURATE,
    "i'd pay": WOULD_PAY, "id pay": WOULD_PAY, "i would pay": WOULD_PAY, "ಪಾವತಿಸುತ್ತೇನೆ": WOULD_PAY,
    "not now": NOT_NOW, "ಈಗ ಬೇಡ": NOT_NOW,
}


# ---------------------------------------------------------------- months and shops


def statement_month(now: datetime) -> tuple[int, int]:
    """The month the statements job states at now: the one before the most recent slot (the 1st at STATEMENT_AT IST)
    at or before now."""
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(IST)
    year, month = local.year, local.month
    if local < datetime.combine(date(year, month, 1), STATEMENT_AT, tzinfo=IST):
        year, month = leads.previous_month(year, month)  # this month's slot is still ahead
    return leads.previous_month(year, month)


def demo_data() -> bool:
    """Whether this deployment runs on generated data, from which no statement is ever built."""
    return settings.environment == "demo" or settings.demo_mode


def demo_shop_ids(db: Database) -> set[int]:
    owners = db.users.distinct("_id", {"email": {"$regex": DEMO_EMAIL_PATTERN, "$options": "i"}})
    return set(db.shops.distinct("_id", {"owner_id": {"$in": owners}})) if owners else set()


def active_shops(db: Database, now: datetime) -> list[Shop]:
    """Verified live shops with an active listing whose details or stock changed in the last ACTIVE_LISTING_DAYS,
    leaving out the seed's shops."""
    since = now - timedelta(days=ACTIVE_LISTING_DAYS)
    live = db.shops.find(live_shop_filter(now), sort=[("_id", 1)])
    if not live:
        return []
    fresh = set(db.products.distinct("shop_id", {"shop_id": {"$in": [s.id for s in live]}, "is_active": True,
                                                 "$or": [{"updated_at": {"$gte": since}},
                                                         {"stock_updated_at": {"$gte": since}}]}))
    demo = demo_shop_ids(db)
    return [s for s in live if s.id in fresh and s.id not in demo]


# ---------------------------------------------------------------- the statement's content


def _rupees(paise: int) -> str:
    """Whole rupees, rounded half up, with thousands separators: 1,499."""
    return f"{(paise + 50) // 100:,}"


def _date(day: date, language: str) -> str:
    """31 Jul 2028 (the month's name in full in Kannada)."""
    name = words.statement_text(f"month_{day.month:02d}", language)
    return f"{day.day} {name[:3] if words.language_of(language) == 'en' else name} {day.year}"


def asked_for(db: Database, shop: Shop, now: datetime) -> list[str]:
    """The top TOP_ASKED "asked for near you" queries while the P9.7 report is shown, else none."""
    module = demand_report.owner_module(db, shop, now)
    return [i["query"] for i in module["items"] if not _MONEY.search(i["query"])][:TOP_ASKED]


def _price(db: Database, shop: Shop, now: datetime) -> dict[str, Any] | None:
    """What Growth would cost the shop now (the founding price when it qualifies), or that it paid in advance."""
    if now < plans.billing_start() and plans.subscriptions(db).exists(
            {"shop_ids": shop.id, "status": SubscriptionStatus.ADVANCE.value}):
        return {"advance_paid": True, "starts_on": settings.billing_starts_on.isoformat()}
    growth = plans.catalogue(db, now).get(PlanCode.GROWTH.value)
    if growth is None:
        return None
    founding = growth.founding_year_paise is not None and plans.founding_eligibility(db, shop)["eligible"]
    year = growth.founding_year_paise if founding else growth.price_year_paise
    if year is None:
        return None
    return {"advance_paid": False, "founding": founding, "year_paise": year, "month_paise": round(year / 12)}


def compose(db: Database, shop: Shop, year: int, month: int, now: datetime) -> dict[str, Any]:
    """The statement of a shop for a month, as stored (module docstring), without its ids and delivery."""
    counts = leads.month_counts(db, shop.id, year, month, now)
    effective = plans.effective_plan(db, shop.id, now)
    roi = now >= plans.billing_start() and bool(effective["entitlements"].get("roi_statement"))
    doc: dict[str, Any] = {
        "shop_id": shop.id, "owner_id": shop.owner_id, "month": leads.month_key(year, month),
        "kind": ROI if roi else SHADOW_BILL, "counts": {f: int(counts[f]) for f in leads.COUNT_FIELDS},
        "asked_for": asked_for(db, shop, now), "plan_code": effective["code"], "price": None, "pay_link": None,
        "plan": None, "guarantee": None}
    if roi:
        sub = plans.subscriptions(db).get(effective["subscription_id"])
        progress = plans.guarantee_progress(db, sub, now)
        doc["plan"] = {"code": effective["code"], "name": effective["name"], "ends_at": effective["ends_at"]}
        doc["guarantee"] = {k: progress[k] for k in ("applies", "customer_requests", "needed", "met", "credit_months")
                            if k in progress}
    else:
        doc["price"] = _price(db, shop, now)
        if doc["price"] is not None and not doc["price"]["advance_paid"]:
            doc["pay_link"] = site_link(billing.BILLING_LINK)
    return doc


def site_link(path: str) -> str:
    """A link to a page of the site: the first CORS origin (the site's own origin) and the path."""
    origin = next((o.rstrip("/") for o in settings.cors_origins if o.startswith("http")), "")
    return f"{origin}{path}"


def parts(st: dict[str, Any], shop_name: str, language: str) -> dict[str, str]:
    """The statement's words in the language: month_shop, counts, and amount_due and price (shadow bill) or plan and
    guarantee (ROI). The only rupee amounts are in amount_due and price."""
    year, month = (int(x) for x in st["month"].split("-"))
    counts = words.statement_text("counts", language, st["counts"])
    if st.get("asked_for"):
        counts += words.statement_text("asked_for", language, {"items": ", ".join(st["asked_for"])})
    out = {"month_shop": words.statement_text("month_shop", language, {
        "month": words.month_name(year, month, language), "shop": shop_name}), "counts": counts}
    if st["kind"] == ROI:
        guarantee = st.get("guarantee") or {}
        if guarantee.get("applies"):
            out["counts"] += words.statement_text("quarter_requests", language,
                                                  {"requests": guarantee["customer_requests"]})
            met = words.statement_text("guarantee_met" if guarantee.get("met") else "guarantee_not_met", language)
            out["guarantee"] = words.statement_text("guarantee_line", language, {
                "requests": guarantee["customer_requests"], "needed": guarantee["needed"], "met": met,
                "months": guarantee.get("credit_months", settings.plan_guarantee_credit_months)})
        else:
            out["guarantee"] = words.statement_text("guarantee_none", language)
        plan = st.get("plan") or {}
        ends = plan.get("ends_at")
        last_day = leads.ist_day(ends - timedelta(microseconds=1)) if ends else None
        out["plan"] = (words.statement_text("plan_line", language, {"plan": plan.get("name", ""),
                                                                    "date": _date(last_day, language)})
                       if last_day else plan.get("name", ""))
        return out
    out["amount_due"] = words.statement_text("amount_due", language)
    price = st.get("price")
    if price is None:
        out["price"] = ""
    elif price.get("advance_paid"):
        out["price"] = words.statement_text("price_advance_paid", language, {
            "date": _date(date.fromisoformat(price["starts_on"]), language)})
    else:
        out["price"] = words.statement_text("price_founding" if price["founding"] else "price_standard", language, {
            "month_price": _rupees(price["month_paise"]), "year_price": _rupees(price["year_paise"])})
    return out


def notification_text(st: dict[str, Any], shop_name: str, language: str) -> tuple[str, str]:
    """The in-app notification's title and body."""
    year, month = (int(x) for x in st["month"].split("-"))
    p = parts(st, shop_name, language)
    title = words.statement_text("title", language, {"month": words.month_name(year, month, language)})
    body = words.statement_text("roi_body" if st["kind"] == ROI else "free_body", language, p)
    return title, body


def template_params(st: dict[str, Any], shop_name: str, language: str) -> list[str]:
    """The WhatsApp template's variables, in order (whatsapp.STATEMENT_VARIABLES), one line each."""
    p = parts(st, shop_name, language)
    names = whatsapp.STATEMENT_VARIABLES[NOTIFICATION_KINDS[st["kind"]]]
    return [whatsapp._one_line(p.get(name)) for name in names]


# ---------------------------------------------------------------- building and sending


def _keep_until(month_key: str) -> datetime:
    year, month = (int(x) for x in month_key.split("-"))
    _, end = leads.month_bounds(year, month)
    return end.replace(year=end.year + KEEP_YEARS)


def _write(db: Database, shop: Shop, doc: dict[str, Any], statement_id: int, now: datetime) -> dict[str, Any]:
    """Inside the transaction: the statement, its notification and its outbox row's variables, buttons and route."""
    language = words.recipient_language(db, shop.owner_id)
    st = {"_id": statement_id, **doc, "language": language, "notification_id": None, "outbox_id": None,
          "route": None, "delivery": "not_sent", "built_at": now, "keep_until": _keep_until(doc["month"])}
    db.mongo[STATEMENTS].insert_one(st, session=db.session)  # the unique (shop_id, month) index: once per month
    kind = NOTIFICATION_KINDS[st["kind"]]
    title, body = notification_text(st, shop.name, language)
    note = notify(db, shop.owner_id, kind, title, body, LINK)
    row = db.mongo[whatsapp.OUTBOX].find_one({"notification_id": note.id, "kind": kind}, {"_id": 1},
                                             session=db.session)
    update: dict[str, Any] = {"notification_id": note.id}
    if row is not None:
        in_window = whatsapp.window_open(db, shop.owner_id, now, whatsapp.WINDOW_MARGIN)
        route = whatsapp.ROUTE_IN_WINDOW if in_window else whatsapp.ROUTE_TEMPLATE
        db.mongo[whatsapp.OUTBOX].update_one({"_id": row["_id"]}, {"$set": {
            "params": template_params(st, shop.name, language), "subject": f"statement:{statement_id}",
            "buttons": [f"{PAYLOAD_PREFIX}{statement_id}:{b}" for b in BUTTONS[st["kind"]]],
            "route": route, "category": whatsapp.CATEGORY_SERVICE if in_window else whatsapp.TEMPLATES[kind].category,
        }}, session=db.session)
        update.update(outbox_id=row["_id"], route=route, delivery="queued")
    db.mongo[STATEMENTS].update_one({"_id": statement_id}, {"$set": update}, session=db.session)
    st.update(update)
    return st


def build_month(db: Database, year: int, month: int, now: datetime | None = None) -> dict[str, Any]:
    """One statement per active shop for the month (module docstring). A shop that already has the month's statement
    is skipped, so a re-run never sends one twice. Returns the counts built, by kind, and skipped."""
    now = now or utcnow()
    key = leads.month_key(year, month)
    if demo_data():
        return {"month": key, "enabled": False, "reason": "demo_data"}
    if (year, month) < FIRST_MONTH:
        return {"month": key, "enabled": False, "reason": "before_first_month"}
    done = set(db.mongo[STATEMENTS].distinct("shop_id", {"month": key}))
    result = {"month": key, "enabled": True, "built": 0, SHADOW_BILL: 0, ROI: 0, "queued": 0, "existing": 0}
    for shop in active_shops(db, now):
        if shop.id in done:
            result["existing"] += 1
            continue
        doc = compose(db, shop, year, month, now)
        statement_id = db.next_id(STATEMENTS)
        try:
            st = db.transaction(lambda s=shop, d=doc, i=statement_id: _write(db, s, d, i, now))
        except DuplicateKeyError:  # another run built it in the meantime
            result["existing"] += 1
            continue
        result["built"] += 1
        result[st["kind"]] += 1
        result["queued"] += st["delivery"] == "queued"
    return result


def sync_delivery(db: Database, month: str | None = None) -> int:
    """Copy the outbox rows' statuses onto statements still queued or sent (module docstring). Returns how many
    statements changed."""
    match: dict[str, Any] = {"delivery": {"$in": list(OPEN_DELIVERY)}, "outbox_id": {"$ne": None}}
    if month is not None:
        match["month"] = month
    rows = list(db.mongo[STATEMENTS].find(match, {"outbox_id": 1, "delivery": 1}, limit=SYNC_BATCH))
    if not rows:
        return 0
    outbox = {r["_id"]: r for r in db.mongo[whatsapp.OUTBOX].find(
        {"_id": {"$in": [r["outbox_id"] for r in rows]}},
        {"status": 1, "sent_at": 1, "delivered_at": 1, "failed_at": 1, "error": 1})}
    changed = 0
    for st in rows:
        row = outbox.get(st["outbox_id"])
        if row is None:
            continue  # removed after 30 days: the last status copied stands
        status = FROM_OUTBOX.get(row.get("status"), st["delivery"])
        fields = {"delivery": status, **{k: row.get(k) for k in ("sent_at", "delivered_at", "failed_at")
                                         if row.get(k) is not None}}
        if status in ("failed", "cancelled"):
            fields["delivery_error"] = (row.get("error") or "")[:80] or None
        if db.mongo[STATEMENTS].update_one({"_id": st["_id"], "delivery": st["delivery"]},
                                           {"$set": fields}).modified_count and status != st["delivery"]:
            changed += 1
    return changed


# ---------------------------------------------------------------- replies


def _reply(key: str, language: str, params: dict[str, Any] | None = None) -> str:
    return words.statement_text(key, language, params)


def _ask_reason(db: Database, owner: User, statement_id: int, reply_id: int, now: datetime) -> None:
    db.mongo[whatsapp.SESSIONS].update_one({"_id": owner.id}, {"$set": {"statement_reason": {
        "statement_id": statement_id, "reply_id": reply_id, "expires_at": now + REASON_WAIT}}})


def _would_pay(db: Database, owner: User, shop: Shop, now: datetime) -> tuple[int | None, str]:
    """The answer to I'd pay: a P9.5 payment request for Growth by the year and how to pay it, or the billing page."""
    language = owner.language
    link = site_link(billing.BILLING_LINK)
    if now < plans.billing_start() and plans.subscriptions(db).exists(
            {"shop_ids": shop.id, "status": SubscriptionStatus.ADVANCE.value}):
        return None, _reply("price_advance_paid", language, {
            "date": _date(settings.billing_starts_on, language)})
    try:
        req = billing.create_request(db, shop, plan_code=PlanCode.GROWTH, period=Period.YEAR,
                                     actor=Actor(type="owner", id=owner.id), now=now)
    except AppError as exc:  # not set up, or this shop cannot buy Growth by the year now
        log.info("statement I'd pay: no payment request for shop %s (%s)", shop.id, exc.code)
        return None, _reply("reply_would_pay_link", language, {"link": link})
    plan_price = (_reply("price_founding" if req.founding else "price_standard", language, {
        "month_price": _rupees(round(req.amount_paise / 12)), "year_price": _rupees(req.amount_paise)})
                  .rstrip("."))
    return req.id, _reply("reply_would_pay", language, {
        "plan_price": plan_price, "amount": billing.rupees(req.total_paise), "upi_id": settings.billing_upi_id,
        "payee": settings.billing_payee_name, "number": req.number, "link": link})


def answer(db: Database, owner: User, shop: Shop, statement_id: int, kind: str,
           now: datetime | None = None) -> tuple[str, str]:
    """Record one reply to a statement, once (module docstring). Returns (outcome, the bot's answer) in the owner's
    language."""
    now = now or utcnow()
    language = owner.language
    st = db.mongo[STATEMENTS].find_one({"_id": statement_id}, {"shop_id": 1, "month": 1, "kind": 1})
    if st is None or st["shop_id"] != shop.id:
        return "statement_not_found", _reply("reply_not_found", language)
    if kind not in BUTTONS[st["kind"]]:
        return "statement_bad_reply", _reply("reply_not_found", language)
    reply_id = db.next_id(REPLIES)
    try:
        db.mongo[REPLIES].insert_one({"_id": reply_id, "statement_id": statement_id, "shop_id": shop.id,
                                      "month": st["month"], "kind": kind, "at": now, "reason": None,
                                      "reason_at": None, "payment_request_id": None,
                                      "keep_until": _keep_until(st["month"])})
    except DuplicateKeyError:
        text = _reply("reply_duplicate", language)
        if kind == WOULD_PAY:
            text += " " + _reply("reply_would_pay_link", language, {"link": site_link(billing.BILLING_LINK)})
        return "statement_duplicate", text
    if kind == NOT_ACCURATE:
        _ask_reason(db, owner, statement_id, reply_id, now)
        return "statement_not_accurate", _reply("reply_not_accurate", language)
    if kind == WOULD_PAY:
        request_id, text = _would_pay(db, owner, shop, now)
        if request_id is not None:
            db.mongo[REPLIES].update_one({"_id": reply_id}, {"$set": {"payment_request_id": request_id}})
        return "statement_would_pay", text
    return f"statement_{kind}", _reply("reply_accurate" if kind == ACCURATE else "reply_not_now", language)


def answer_button(db: Database, owner: User, shop: Shop, payload: str, now: datetime | None = None
                  ) -> tuple[str, str]:
    """A statement's quick reply: statement:<id>:<reply>."""
    _, _, rest = payload.partition(PAYLOAD_PREFIX)
    ref, _, kind = rest.partition(":")
    if not ref.isdecimal() or kind not in REPLY_KINDS:
        return "statement_bad_reply", _reply("reply_not_found", owner.language)
    return answer(db, owner, shop, int(ref), kind, now)


def _reply_word(message: str) -> str | None:
    word = " ".join(message.replace("’", "'").lower().split()).strip(" .!")
    return REPLY_WORDS.get(word)


def answer_text(db: Database, owner: User, shop: Shop, message: str, now: datetime | None = None
                ) -> tuple[str, str] | None:
    """A text from the shop that is a statement reply typed inside the window (REPLY_WORDS, for the latest statement
    built in the last REPLY_WINDOW), or the reason asked for after Not accurate. None when it is neither: the stock
    bot answers it."""
    now = now or utcnow()
    kind = _reply_word(message)
    if kind is not None:
        latest = db.mongo[STATEMENTS].find_one({"shop_id": shop.id, "built_at": {"$gte": now - REPLY_WINDOW}},
                                               {"_id": 1}, sort=[("month", -1)])
        if latest is not None:
            return answer(db, owner, shop, latest["_id"], kind, now)
    session = db.mongo[whatsapp.SESSIONS].find_one({"_id": owner.id}, {"statement_reason": 1}) or {}
    waiting = session.get("statement_reason")
    if not isinstance(waiting, dict):
        return None
    db.mongo[whatsapp.SESSIONS].update_one({"_id": owner.id}, {"$set": {"statement_reason": None}})
    reason = " ".join(message.split())[:REASON_MAX]
    if waiting.get("expires_at", now) <= now or not reason:
        return None
    done = db.mongo[REPLIES].update_one({"_id": waiting.get("reply_id"), "shop_id": shop.id, "kind": NOT_ACCURATE},
                                        {"$set": {"reason": reason, "reason_at": now}}).modified_count
    if not done:
        return None
    return "statement_reason", _reply("reply_reason", owner.language)


# ---------------------------------------------------------------- views


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() + "Z" if at else None


def statement_out(st: dict[str, Any], shop_name: str, language: str,
                  replies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A statement as the owner's screens show it: its counts, kind, price or plan lines and replies, with its words
    in the owner's language."""
    plan = st.get("plan")
    return {"id": st["_id"], "month": st["month"], "kind": st["kind"], "counts": st["counts"],
            "asked_for": st.get("asked_for") or [], "plan_code": st.get("plan_code"), "price": st.get("price"),
            "pay_link": st.get("pay_link"),
            "plan": {**plan, "ends_at": _iso(plan.get("ends_at"))} if plan else None,
            "guarantee": st.get("guarantee"), "delivery": st.get("delivery"), "built_at": _iso(st.get("built_at")),
            "text": parts(st, shop_name, language),
            "replies": [{"kind": r["kind"], "at": _iso(r["at"]), "reason": r.get("reason")} for r in replies or []]}


def owner_statements(db: Database, shop: Shop, language: str, now: datetime | None = None) -> dict[str, Any]:
    """The shop's statements by month, newest first, and the current month's counts so far."""
    now = now or utcnow()
    rows = list(db.mongo[STATEMENTS].find({"shop_id": shop.id}, sort=[("month", -1)]))
    replies: dict[int, list[dict]] = {}
    for r in db.mongo[REPLIES].find({"statement_id": {"$in": [s["_id"] for s in rows]}}, sort=[("at", 1)]):
        replies.setdefault(r["statement_id"], []).append(r)
    local = leads.ist_day(now)
    return {"statements": [statement_out(s, shop.name, language, replies.get(s["_id"])) for s in rows],
            "current": leads.month_counts(db, shop.id, local.year, local.month, now)}


def owner_statement(db: Database, shop: Shop, month: str, language: str,
                    now: datetime | None = None) -> dict[str, Any]:
    """One month: its statement, or the current month so far (statement null)."""
    now = now or utcnow()
    year, number = (int(x) for x in month.split("-"))
    st = db.mongo[STATEMENTS].find_one({"shop_id": shop.id, "month": month})
    if st is not None:
        replies = list(db.mongo[REPLIES].find({"statement_id": st["_id"]}, sort=[("at", 1)]))
        return {"month": month, "statement": statement_out(st, shop.name, language, replies)}
    local = leads.ist_day(now)
    if (year, number) == (local.year, local.month):
        return {"month": month, "statement": None, "current": leads.month_counts(db, shop.id, year, number, now)}
    raise NotFound("No statement for this month")


def latest_month(db: Database) -> str | None:
    row = db.mongo[STATEMENTS].find_one({}, {"month": 1}, sort=[("month", -1)])
    return row["month"] if row else None


def admin_summary(db: Database, month: str) -> dict[str, Any]:
    """For a month: statements built (one per active shop), by kind and by delivery status (sent counts every
    statement Meta accepted, delivered included), replies by kind, and the share of active shops whose answer was
    Accurate (the B25 readout: at least 60%)."""
    sync_delivery(db, month)
    rows = list(db.mongo[STATEMENTS].find({"month": month}, {"kind": 1, "delivery": 1}))
    by_delivery = {d: 0 for d in DELIVERY}
    by_kind = {k: 0 for k in KINDS}
    for st in rows:
        by_delivery[st.get("delivery") or "not_sent"] = by_delivery.get(st.get("delivery") or "not_sent", 0) + 1
        by_kind[st["kind"]] += 1
    replies = {k: 0 for k in REPLY_KINDS}
    latest_accuracy: dict[int, tuple[datetime, int, str]] = {}
    reasons = 0
    for r in db.mongo[REPLIES].find({"month": month}, {"statement_id": 1, "kind": 1, "at": 1, "reason": 1}):
        replies[r["kind"]] = replies.get(r["kind"], 0) + 1
        reasons += bool(r.get("reason"))
        if r["kind"] in (ACCURATE, NOT_ACCURATE):
            key = (r["at"], r["_id"], r["kind"])
            if r["statement_id"] not in latest_accuracy or key > latest_accuracy[r["statement_id"]]:
                latest_accuracy[r["statement_id"]] = key
    accurate = sum(1 for _, _, kind in latest_accuracy.values() if kind == ACCURATE)
    built = len(rows)
    sent = by_delivery["sent"] + by_delivery["delivered"]
    return {"month": month, "built": built, "by_kind": by_kind, "sent": sent, "delivered": by_delivery["delivered"],
            "failed": by_delivery["failed"], "by_delivery": by_delivery,
            "delivered_pct": round(100 * by_delivery["delivered"] / sent, 1) if sent else None,
            "replies": replies, "not_accurate_reasons": reasons, "answered_accurate": accurate,
            "accurate_share_pct": round(100 * accurate / built, 1) if built else None,
            "readout_target_pct": 60}
