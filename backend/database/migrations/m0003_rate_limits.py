"""Migration 3: rate_limits (P2.3, app/core/ratelimit.py; plan section 8.3).

The fixed-window counters of the rate limiter, shared by every API instance. The collection is new, so
this only adds: it is created with its validator and its TTL index on expires_at from database/schema.py,
or, where it exists already (a database built by ensure_schema from this release, or the limiter's first
write racing the deploy), given them. Idempotent: a second run changes nothing."""
from __future__ import annotations

import logging

from pymongo.database import Database as MongoDatabase

from database.schema import INDEXES, VALIDATORS

NUMBER = 3
NAME = "rate_limits"

COLLECTION = "rate_limits"

log = logging.getLogger(__name__)


def apply(mongo: MongoDatabase) -> None:
    options = {"validator": VALIDATORS[COLLECTION], "validationLevel": "strict", "validationAction": "error"}
    if COLLECTION in set(mongo.list_collection_names()):
        mongo.command("collMod", COLLECTION, **options)
    else:
        mongo.create_collection(COLLECTION, **options)
        log.info("created collection %s", COLLECTION)
    for model in INDEXES[COLLECTION]:
        mongo[COLLECTION].create_indexes([model])  # an index already built is left as it is
