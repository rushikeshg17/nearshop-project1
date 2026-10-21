"""Migration 6: review integrity (P3.5; plan section 8.2).

  * Orders the shop has confirmed but not yet delivered get a random 4-digit delivery code, since delivering now
    needs one (app/services/orders.py). The customer sees it on their order. An order the previous release
    confirms while this runs gets its code from orders.ensure_delivery_code on its next step.
  * Every shop's rating is recomputed under the new rules (app/services/reviews.py, recompute_shop): one counted
    review per customer per shop in 30 days, self-dealing reviews held out, and rating_customers stored so that
    no rating is shown below 5 distinct customers. Shops are done in batches of SHOP_BATCH, each shop on its own.

The new fields are optional in the validators (database/schema.py), which ensure_schema applies after this
migration. Idempotent: orders that have a code are skipped, and a recompute of an already recomputed shop
changes nothing."""
from __future__ import annotations

import logging

from pymongo.database import Database as MongoDatabase

from app.models.enums import OrderStatus
from app.services.reviews import recompute_shop
from database.migrations import backfill

NUMBER = 6
NAME = "review_integrity"
SHOP_BATCH = 200

log = logging.getLogger(__name__)

# A random 4-digit string per document ($rand is evaluated for each document an update pipeline touches).
RANDOM_CODE = {"$substrCP": [{"$toString": {"$add": [10_000, {"$floor": {"$multiply": [{"$rand": {}}, 10_000]}}]}},
                             1, 4]}


def apply(mongo: MongoDatabase) -> None:
    awaiting = [OrderStatus.SHOP_CONFIRMED.value, OrderStatus.PREPARING.value, OrderStatus.OUT_FOR_DELIVERY.value]
    backfill(mongo, "orders", {"status": {"$in": awaiting}, "delivery_code": {"$in": [None, ""]}},
             [{"$set": {"delivery_code": RANDOM_CODE}}])
    done, last_id = 0, None
    while True:
        page = {} if last_id is None else {"_id": {"$gt": last_id}}
        ids = [d["_id"] for d in mongo["shops"].find(page, {"_id": 1}).sort("_id", 1).limit(SHOP_BATCH)]
        if not ids:
            break
        for shop_id in ids:
            recompute_shop(mongo, shop_id)
        done, last_id = done + len(ids), ids[-1]
        log.info("review integrity: %d shops recomputed so far", done)
