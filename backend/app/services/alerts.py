"""Low-stock alerts (P9.14; plan section 8.18; docs/whatsapp-templates.md, nearshop_shop_low_stock).

A shop whose plan has the low_stock_alerts entitlement (Growth and above, P9.4: asked through plans.entitled, nothing
else) gets one message a day listing its active listings at or below their low-stock level (quantity at or below
low_stock_threshold, as the inventory page's low-stock filter counts them, out-of-stock listings included): up to
MAX_ITEMS of them, lowest count first, with "and N more" when there are more, and a link to the inventory page.

Settings (on Shop, changed through PATCH /api/owner/shop): low_stock_alerts_on (true by default, so an entitled shop
gets the alert without doing anything) and low_stock_alert_hour (the IST hour, 18 by default). A Free shop keeps
the setting but gets nothing; its settings page says the alert is part of Growth (settings_out).

When: the low_stock_alerts job (app/jobs.py, every 30-second tick under its lease, P1.7) considers each active shop
with the alert on once per IST day, from its hour until GRACE after it (a tick missed while no process ran is caught
up, never sent twice). Considering a shop records the day on it (low_stock_alert_day, with low_stock_alert_outcome:
sent, nothing_low or not_entitled), with a write conditional on the day not being recorded yet; for a sent alert that
write and the notification are one transaction. So a shop gets at most one alert a day, whatever the number of
processes, and a failed send leaves the day unrecorded for the next tick.

How: notify() writes the in-app notification (kind low_stock_alert, link /shop/inventory) and, while WhatsApp is on
and the owner opted in with a verified phone, its outbox row (app/services/whatsapp.py). Inside the owner's open
24-hour service window (open for WINDOW_MARGIN more, as for the stock check) the row goes as a free-form text, route
in_window, Meta's service category; otherwise as the nearshop_shop_low_stock template, whose two variables are the
shop's name and the list. The words are in notification_text.py (low_stock_alert, low_stock_item, low_stock_more);
their Kannada is still awaited from the team, so every owner gets them in English and the template in English.

The alert never replaces the stock check (whatsapp.send_stock_checks runs on its own job, unchanged), and stock updates
by WhatsApp stay free for every plan (plans.FREE_TOOLS): only this alert asks for the entitlement.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.database import Database, utcnow
from app.models import Shop
from app.services import notification_text as words
from app.services import plans, whatsapp
from app.services.notifications import notify

KIND = "low_stock_alert"  # the notification's kind (and its WhatsApp template, whatsapp.TEMPLATES)
MESSAGE = "low_stock_alert"  # notification_text.MESSAGES
ENTITLEMENT = "low_stock_alerts"  # app/models/billing.py Entitlements
MAX_ITEMS = 10
GRACE = timedelta(hours=3)  # sent from the hour until this long after it, the same IST day
DEFAULT_HOUR = 18  # Shop.low_stock_alert_hour's default: 18:00 IST
INVENTORY_LINK = "/shop/inventory"
BATCH = 50  # shops considered per run
NAME_MAX = 60  # characters of a listing's name, shortened further until the list fits one template variable
NAME_MIN = 12
LOW = {"$expr": {"$lte": ["$quantity", "$low_stock_threshold"]}}  # at or below the low-stock level
DAY_FIELD = "low_stock_alert_day"
OUTCOME_FIELD = "low_stock_alert_outcome"


def due_hours(local: datetime) -> list[int]:
    """The alert hours whose slot is open at this IST time: from the hour until GRACE after it, the same day."""
    span = int(GRACE.total_seconds() // 3600)
    return [local.hour - i for i in range(span) if local.hour - i >= 0]


def low_listings(db: Database, shop_id: int) -> tuple[list[dict[str, Any]], int]:
    """The shop's active listings at or below their low-stock level, lowest count first (up to MAX_ITEMS), and how
    many there are in all."""
    query = {"shop_id": shop_id, "is_active": True, **LOW}
    rows = db.products.find_raw(query, {"name": 1, "quantity": 1, "low_stock_threshold": 1},
                                sort=[("quantity", 1), ("name", 1), ("_id", 1)], limit=MAX_ITEMS)
    total = len(rows) if len(rows) < MAX_ITEMS else db.products.count(query)
    return rows, total


def text_language(db: Database, owner_id: int) -> str:
    """The language the alert is written in: the owner's, when the alert has a text in it, else English."""
    language = words.recipient_language(db, owner_id)
    return language if language in words.MESSAGES[MESSAGE] else words.DEFAULT_LANGUAGE


def items_text(rows: list[dict[str, Any]], total: int, language: str) -> str:
    """The listings as one line ("Tape: 1 (alert at 2); Bulb: 0 (alert at 3); and 4 more"), long names shortened
    until it fits one template variable (whatsapp.PARAM_MAX), so no listing is cut off the end."""
    more = words.text("low_stock_more", language, {"count": total - len(rows)}) if total > len(rows) else ""

    def joined(cap: int) -> str:
        def name(row: dict[str, Any]) -> str:
            flat = " ".join(str(row.get("name") or "").split())
            return flat if len(flat) <= cap else flat[:cap - 1].rstrip() + "…"

        parts = [words.text("low_stock_item", language, {"name": name(r), "quantity": r["quantity"],
                                                         "threshold": r["low_stock_threshold"]}) for r in rows]
        return "; ".join(parts + ([more] if more else []))

    cap = NAME_MAX
    while len(joined(cap)) > whatsapp.PARAM_MAX and cap > NAME_MIN:
        cap -= 1
    return joined(cap)


def _record(db: Database, shop_id: int, day: str, outcome: str) -> bool:
    """Record the day's decision on the shop, unless one is recorded already (in db's transaction, if any)."""
    return db.shops.update_one({"_id": shop_id, DAY_FIELD: {"$ne": day}},
                               {"$set": {DAY_FIELD: day, OUTCOME_FIELD: outcome}}) == 1


def _send(db: Database, shop: dict[str, Any], day: str, rows: list[dict[str, Any]], total: int,
          now: datetime) -> str | None:
    """Inside one transaction: record the day as sent and write the alert, with its outbox row's route. Returns the
    route (in_window, template, or app when no WhatsApp row was written), or None when another process recorded the
    day first."""
    def run() -> str | None:
        if not _record(db, shop["_id"], day, "sent"):
            return None
        owner_id = shop["owner_id"]
        language = text_language(db, owner_id)
        shop_name = " ".join(str(shop.get("name") or "").split()) or words.text("your_shop", language)
        items = items_text(rows, total, language)
        note = notify(db, owner_id, KIND, link=INVENTORY_LINK, message=MESSAGE,
                      params={"shop": shop_name, "items": items})
        out = db.mongo[whatsapp.OUTBOX]
        row = out.find_one({"notification_id": note.id, "kind": KIND}, {"_id": 1}, session=db.session)
        if row is None:
            return "app"
        # The template in the language the words were written in (English while the Kannada is awaited).
        fields: dict[str, Any] = {"language": whatsapp.template_language(language)}
        if whatsapp.window_open(db, owner_id, now, whatsapp.WINDOW_MARGIN):
            fields.update(route=whatsapp.ROUTE_IN_WINDOW, category=whatsapp.CATEGORY_SERVICE)
        else:  # the template's variables: {{1}} the shop, {{2}} the list
            fields["params"] = [whatsapp._one_line(shop_name), whatsapp._one_line(items)]
        out.update_one({"_id": row["_id"]}, {"$set": fields}, session=db.session)
        return fields.get("route", whatsapp.ROUTE_TEMPLATE)

    return db.transaction(run)


def send_low_stock_alerts(db: Database, now: datetime | None = None, limit: int = BATCH) -> dict[str, Any]:
    """One run of the low_stock_alerts job (module docstring): every active shop with the alert on whose hour's slot
    is open now and whose day is not recorded yet. Returns the IST day and the counts of shops sent (with the route
    of each: in_window, template, app), nothing_low and not_entitled."""
    now = now or utcnow()
    local = now.replace(tzinfo=UTC).astimezone(whatsapp.IST)
    day = local.date().isoformat()
    hours = due_hours(local)
    counts = {"sent": 0, whatsapp.ROUTE_IN_WINDOW: 0, whatsapp.ROUTE_TEMPLATE: 0, "app": 0, "nothing_low": 0,
              "not_entitled": 0}
    when: list[dict[str, Any]] = [{"low_stock_alert_hour": {"$in": hours}}]
    if DEFAULT_HOUR in hours:
        when.append({"low_stock_alert_hour": {"$exists": False}})  # shops written before P9.14
    due = db.shops.find_raw({"is_active": True, "low_stock_alerts_on": {"$ne": False}, DAY_FIELD: {"$ne": day},
                             "$or": when}, {"_id": 1, "owner_id": 1, "name": 1}, sort=[("_id", 1)], limit=limit)
    for shop in due:
        if not plans.entitled(db, shop["_id"], ENTITLEMENT, now):
            counts["not_entitled"] += _record(db, shop["_id"], day, "not_entitled")
            continue
        rows, total = low_listings(db, shop["_id"])
        if not rows:
            counts["nothing_low"] += _record(db, shop["_id"], day, "nothing_low")
            continue
        route = _send(db, shop, day, rows, total, now)
        if route is not None:
            counts["sent"] += 1
            counts[route] += 1
    return {"day": day, **counts}


def settings_out(db: Database, shop: Shop) -> dict[str, Any]:
    """The owner's alert settings, and whether the shop's plan includes the alert (the settings page says it is part
    of Growth when not)."""
    return {"low_stock_alerts_on": shop.low_stock_alerts_on, "low_stock_alert_hour": shop.low_stock_alert_hour,
            "low_stock_alerts_entitled": bool(plans.entitled(db, shop.id, ENTITLEMENT))}
