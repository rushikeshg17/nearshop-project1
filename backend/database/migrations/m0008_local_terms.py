"""Migration 8: local_terms on catalog items and their listings (P5.2; plan section 8.6).

Customers in Ballari type item names in Kannada, Telugu and Hindi script, or spell those words in Latin
letters ("istri petti"). The catalogue's names and tags are English, so such a query matched nothing.
Each catalog item now carries local_terms: a list of normalised strings (app/utils/text.py normalize_query)
in Kannada, Telugu and Devanagari script plus common Latin transliterations, loaded from
database/seed/data/local_terms.json, which the team fills for the top 150 catalogue items. The keyword
stage of search (app/services/search.py) adds an exact-token match on them.

This migration only adds:
  * the multikey indexes on catalog_items.local_terms and products.local_terms (declared in
    database/schema.py, built here for a database that already has the collections);
  * catalog_items.local_terms for every slug in local_terms.json;
  * the same terms on every listing created from those catalog items, backfilled in batches.

Idempotent: a document that already holds its terms is not written again. The seed calls
apply_local_terms after inserting the demo catalogue, as a fresh database runs this migration before it
has any catalog items."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pymongo.database import Database as MongoDatabase

from app.utils.text import normalize_query
from database.migrations import backfill
from database.schema import INDEXES, LOCAL_TERMS

NUMBER = 8
NAME = "local_terms"

DATA_FILE = Path(__file__).resolve().parents[1] / "seed" / "data" / "local_terms.json"
# The groups of terms an entry of local_terms.json may hold.
TERM_GROUPS = ("kn", "te", "hi", "latin")
INDEX_NAME = "local_terms"
# The validator's limits (database/schema.py LOCAL_TERMS). This migration runs before ensure_schema applies the
# validator, so it checks them itself: a list written past them would make every later update to the
# listing fail validation.
MAX_TERMS = LOCAL_TERMS["maxItems"]
MAX_TERM_LENGTH = LOCAL_TERMS["items"]["maxLength"]

log = logging.getLogger(__name__)


def load_local_terms(path: Path = DATA_FILE) -> dict[str, list[str]]:
    """slug -> its normalised local terms (Kannada, Telugu and Devanagari first, then Latin), deduplicated,
    in file order. Entries with no terms are left out. A malformed file, or one past the validator's limits
    (MAX_TERMS terms per item, MAX_TERM_LENGTH characters per term), raises ValueError before anything is
    written."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path.name}: expected an object with an 'items' list")
    out: dict[str, list[str]] = {}
    for n, entry in enumerate(items):
        slug = entry.get("slug") if isinstance(entry, dict) else None
        if not isinstance(slug, str) or not slug:
            raise ValueError(f"{path.name}: item {n} has no slug")
        terms: list[str] = []
        for group in TERM_GROUPS:
            values = entry.get(group) or []
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{path.name}: {slug}.{group} must be a list of strings")
            terms += [t for t in (normalize_query(v) for v in values) if t]
        if terms:
            out[slug] = list(dict.fromkeys([*out.get(slug, []), *terms]))
    check_limits(out, path.name)
    return out


def check_limits(terms: dict[str, list[str]], source: str = "local_terms") -> None:
    """Raise ValueError when an item's terms break the validator's limits (MAX_TERMS terms, each a non-empty
    string of at most MAX_TERM_LENGTH characters)."""
    for slug, values in terms.items():
        if len(values) > MAX_TERMS:
            raise ValueError(f"{source}: {slug} has {len(values)} terms; at most {MAX_TERMS} are allowed")
        bad = [t for t in values if not isinstance(t, str) or not t or len(t) > MAX_TERM_LENGTH]
        if bad:
            raise ValueError(f"{source}: {slug} has a term that is empty or longer than {MAX_TERM_LENGTH} "
                             f"characters: {str(bad[0])[:40]!r}")


def apply_local_terms(mongo: MongoDatabase, terms: dict[str, list[str]] | None = None,
                      *, batch_size: int = 500) -> tuple[int, int]:
    """Write local_terms to the catalog items named in terms (default: local_terms.json), then copy each
    item's terms to its listings in batches. Returns (catalog items updated, listings updated)."""
    if terms is None:
        terms = load_local_terms()
    else:
        check_limits(terms)
    items_updated = 0
    for slug, values in terms.items():
        items_updated += mongo.catalog_items.update_one(
            {"slug": slug, "local_terms": {"$ne": values}}, {"$set": {"local_terms": values}}).modified_count
    listings_updated = 0
    for item in mongo.catalog_items.find({"local_terms.0": {"$exists": True}}, {"local_terms": 1}).sort("_id", 1):
        values = item["local_terms"]
        listings_updated += backfill(mongo, "products", {"catalog_item_id": item["_id"], "local_terms": {"$ne": values}},
                                     {"$set": {"local_terms": values}}, batch_size=batch_size)
    unknown = sorted(set(terms) - {c["slug"] for c in mongo.catalog_items.find({"slug": {"$in": list(terms)}},
                                                                                {"slug": 1})})
    if unknown:
        log.info("local_terms: %d slug(s) not in this catalogue, skipped: %s", len(unknown), ", ".join(unknown[:10]))
    log.info("local_terms: %d catalog items and %d listings updated", items_updated, listings_updated)
    return items_updated, listings_updated


def apply(mongo: MongoDatabase) -> None:
    existing = set(mongo.list_collection_names())
    for collection in ("catalog_items", "products"):
        if collection not in existing:
            continue  # ensure_schema creates it, with its indexes, after the migrations
        for model in INDEXES[collection]:
            if model.document["name"] == INDEX_NAME:
                mongo[collection].create_indexes([model])  # an index already built is left as it is
    if "catalog_items" in existing:
        apply_local_terms(mongo)
