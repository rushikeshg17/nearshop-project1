"""Migration 1: the schema as it stood before numbered migrations (P1.4).

On an empty database it creates every collection, validator and index. On a database that already
has NearShop's collections (every environment deployed before P1.4) it changes nothing and is only
recorded; the runner's ensure_schema afterwards fills in anything missing, as it did before."""
from __future__ import annotations

import logging

from pymongo.database import Database as MongoDatabase

from database.schema import VALIDATORS, ensure_schema

NUMBER = 1
NAME = "baseline"

log = logging.getLogger(__name__)


def apply(mongo: MongoDatabase) -> None:
    present = set(mongo.list_collection_names()) & set(VALIDATORS)
    if present:
        log.info("baseline: %d of %d collections already exist; recorded without changes",
                 len(present), len(VALIDATORS))
        return
    ensure_schema(mongo)
