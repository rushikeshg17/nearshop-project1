"""Migration 8: the plan catalogue, version 1 (P9.4; plan section 8.18; app/services/plans.py).

Inserts catalogue version 1, effective from the Unix epoch so it is in force everywhere at once, with the values of
business/BUSINESS_MODEL.md. They are assumptions under test: they live here only as the first version's data, and
every later change is a new version published by an admin (POST /api/admin/plans), never a code change. Prices are
in paise and exclude GST.

  * free: ₹0; the demand-report teaser; 1 login; always on sale.
  * growth: ₹1,499 a year or ₹149 a month (founding offer ₹999 for the first year); the full weekly demand report,
    low-stock alerts, 3 logins, the ROI statement, priority support, 25% off Boost and the outcome guarantee; on sale.
  * pro: ₹3,499 a year; Growth plus the Pro insights and 5 logins; not on sale until P10.6 and B28.
  * business: ₹7,999 a year; Pro's entitlements with up to 5 outlets and 10 logins; not on sale until P10.14 and B32.
  * partner_seat: Growth's entitlements, paid by a distributor (P10.3), so no shop price; sold only with a Partner
    plan, never on its own.

The plans collection, its validator and its indexes are declared in database/schema.py; on a database that predates
them the insert creates the collection, and ensure_schema adds the validator and indexes right after. Idempotent:
nothing is written when any version 1 row exists. Expand only: a new collection, read only by new code."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from pymongo import ReturnDocument
from pymongo.database import Database as MongoDatabase

NUMBER = 8
NAME = "plan_catalogue"
COLLECTION = "plans"
EFFECTIVE_FROM = datetime(1970, 1, 1)
REASON = "Catalogue version 1: business/BUSINESS_MODEL.md values (assumptions under test)"

log = logging.getLogger(__name__)

FREE = {"demand_report": "teaser", "low_stock_alerts": False, "logins": 1, "roi_statement": False,
        "priority_support": False, "boost_discount_pct": 0, "outcome_guarantee": False, "pro_insights": False,
        "outlets": 1}
GROWTH = {**FREE, "demand_report": "full", "low_stock_alerts": True, "logins": 3, "roi_statement": True,
          "priority_support": True, "boost_discount_pct": 25, "outcome_guarantee": True}
PRO = {**GROWTH, "pro_insights": True, "logins": 5}
BUSINESS = {**PRO, "logins": 10, "outlets": 5}

VERSION_1 = [
    {"code": "free", "name": "Free", "price_year_paise": 0, "price_month_paise": 0, "founding_year_paise": None,
     "on_sale": True, "entitlements": FREE},
    {"code": "growth", "name": "Growth", "price_year_paise": 149_900, "price_month_paise": 14_900,
     "founding_year_paise": 99_900, "on_sale": True, "entitlements": GROWTH},
    {"code": "pro", "name": "Pro", "price_year_paise": 349_900, "price_month_paise": None, "founding_year_paise": None,
     "on_sale": False, "entitlements": PRO},
    {"code": "business", "name": "Business", "price_year_paise": 799_900, "price_month_paise": None,
     "founding_year_paise": None, "on_sale": False, "entitlements": BUSINESS},
    {"code": "partner_seat", "name": "Partner seat", "price_year_paise": None, "price_month_paise": None,
     "founding_year_paise": None, "on_sale": False, "entitlements": GROWTH},
]


def _next_ids(mongo: MongoDatabase, n: int) -> range:
    """n ids from the counters collection, as app/core/database.py issues them."""
    doc = mongo["counters"].find_one_and_update({"_id": COLLECTION}, {"$inc": {"seq": n}}, upsert=True,
                                                return_document=ReturnDocument.AFTER)
    return range(doc["seq"] - n + 1, doc["seq"] + 1)


def apply(mongo: MongoDatabase) -> None:
    if mongo[COLLECTION].find_one({"version": 1}, {"_id": 1}) is not None:
        return
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    rows = [{"_id": _id, **row, "version": 1, "effective_from": EFFECTIVE_FROM, "published_at": now,
             "published_by": None, "reason": REASON} for _id, row in zip(_next_ids(mongo, len(VERSION_1)), VERSION_1)]
    mongo[COLLECTION].insert_many(rows)
    log.info("plan catalogue: version 1 inserted (%d plans)", len(rows))
