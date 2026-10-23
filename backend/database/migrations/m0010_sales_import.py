"""Migration 10: sales-report imports (P7.2).

Shops upload the sales report their billing app exports (app/services/importer.py), and each line becomes a
sales_history row with source import. This migration only adds:
  * the value import to the sales_history validator's source enum, before the code that writes it serves;
  * the partial unique index uq_import_sale on shop_id, product_id, sold_at and basket_id for rows with source
    import (declared in database/schema.py), so a report cannot be imported twice.

Idempotent: a value or index already present is left as it is. On a new database the baseline migration has
already created both from the current schema, and this one changes nothing."""
from __future__ import annotations

from pymongo.database import Database as MongoDatabase

from app.models.enums import SaleSource
from database.migrations import add_enum_values
from database.schema import INDEXES

NUMBER = 10
NAME = "sales_import"

COLLECTION = "sales_history"
INDEX_NAME = "uq_import_sale"


def apply(mongo: MongoDatabase) -> None:
    if COLLECTION not in mongo.list_collection_names():
        return  # ensure_schema, which runs next, creates it with both
    add_enum_values(mongo, COLLECTION, "source", [SaleSource.IMPORT.value])
    model = next(m for m in INDEXES[COLLECTION] if m.document["name"] == INDEX_NAME)
    mongo[COLLECTION].create_indexes([model])  # an index already built is left as it is
