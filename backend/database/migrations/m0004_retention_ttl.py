"""Migration 4: retention TTL indexes (P3.2; plan section 8.4; docs/legal/retention.md).

  * search_history: the plain index "created" on created_at is replaced by the TTL index created_ttl_180d,
    which deletes a search 180 days after it was logged. replace_index builds the new index first and drops
    "created" only once it exists, so queries on created_at are served throughout.
  * notifications: a new TTL index, created_ttl_90d, deletes a notification 90 days after it was created.

On a database that already holds old rows, MongoDB's TTL monitor removes the expired ones within about a
minute of the index being built. Idempotent: an index already built is left as it is, and "created" already
gone is not an error. On a fresh database (reset), the baseline migration has already built both indexes
from database/schema.py and this changes nothing."""
from __future__ import annotations

from pymongo.database import Database as MongoDatabase

from database.migrations import replace_index
from database.schema import NOTIFICATIONS_TTL, SEARCH_HISTORY_TTL

NUMBER = 4
NAME = "retention_ttl"

OLD_SEARCH_HISTORY_INDEX = "created"


def apply(mongo: MongoDatabase) -> None:
    replace_index(mongo, "search_history", OLD_SEARCH_HISTORY_INDEX, SEARCH_HISTORY_TTL)
    mongo["notifications"].create_indexes([NOTIFICATIONS_TTL])
