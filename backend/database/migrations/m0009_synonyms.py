"""Migration 9: the synonyms collection, with the 42 entries that were a dictionary in code (P5.4).

Search synonyms used to be SYNONYMS in app/utils/text.py, so any change needed a code deploy. Admins now
manage them on the Synonyms page (app/services/synonyms.py). This migration only adds:
  * the synonyms collection with its validator and unique index on term (database/schema.py), or, where
    ensure_schema already made it, gives it them;
  * the 42 entries below, each inserted only when its term is not there yet, so an admin's later edit or
    deactivation is never overwritten by a rerun;
  * the synonyms_version counter, bumped when anything was inserted, so running processes re-read.

The entries are a frozen copy of the old dictionary: changing them here would not reach a database that
has already run this migration. Idempotent: a second run inserts nothing."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from pymongo.database import Database as MongoDatabase

from database.schema import INDEXES, VALIDATORS

NUMBER = 9
NAME = "synonyms"

COLLECTION = "synonyms"
VERSION_ID = "synonyms_version"

# Colloquial and regional words people type -> words that appear in listings: term, expansions, languages.
EN, HI = ["en"], ["hi-Latn"]
ENTRIES: list[tuple[str, list[str], list[str]]] = [
    ("chappal", ["slipper", "sandal", "flip flop"], HI),
    ("chappals", ["slipper", "sandal"], HI),
    ("hawai", ["slipper", "flip flop"], HI),
    ("nali", ["tap", "faucet"], HI),
    ("nal", ["tap", "faucet"], HI),
    ("adapter", ["charger", "adaptor"], EN),
    ("adaptor", ["charger", "adapter"], EN),
    ("plug", ["charger", "socket"], EN),
    ("lead", ["cable"], EN),
    ("wire", ["cable"], EN),
    ("screen guard", ["tempered glass"], EN),
    ("screenguard", ["tempered glass"], EN),
    ("cover", ["case"], EN),
    ("pouch", ["case", "cover"], EN),
    ("bulb", ["led", "lamp"], EN),
    ("light", ["led", "lamp", "bulb"], EN),
    ("tubelight", ["batten", "tube light"], EN),
    ("pankha", ["fan"], HI),
    ("veshti", ["dhoti"], ["ta-Latn"]),
    ("dhothi", ["dhoti"], HI),
    ("banian", ["vest", "innerwear"], EN),
    ("baniyan", ["vest", "innerwear"], HI),
    ("chaddi", ["brief", "innerwear"], HI),
    ("kurti", ["kurta"], HI),
    ("copy", ["notebook"], EN),
    ("note book", ["notebook"], EN),
    ("pipe leak", ["teflon tape", "m-seal", "sealant"], EN),
    ("leak", ["teflon tape", "sealant", "m-seal"], EN),
    ("bike", ["motorcycle", "two wheeler"], EN),
    ("scooty", ["scooter", "two wheeler"], EN),
    ("gaadi", ["bike", "motorcycle"], HI),
    ("tyre", ["tube", "tire"], EN),
    ("tire", ["tyre"], EN),
    ("mobile", ["phone", "smartphone"], EN),
    ("cell", ["phone", "battery"], EN),
    ("jhadu", ["broom"], HI),
    ("pocha", ["mop"], HI),
    ("balti", ["bucket"], HI),
    ("soap powder", ["detergent"], EN),
    ("machhar", ["mosquito"], HI),
    ("mosquito coil", ["mosquito repellent"], EN),
    ("tape", ["measuring tape", "insulation tape", "teflon tape"], EN),
]

log = logging.getLogger(__name__)


def _now() -> datetime:
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def import_entries(mongo: MongoDatabase) -> int:
    """Insert every entry whose term is missing. Returns the number inserted."""
    coll = mongo[COLLECTION]
    present = {d["term"] for d in coll.find({"term": {"$in": [t for t, _, _ in ENTRIES]}}, {"term": 1})}
    missing = [e for e in ENTRIES if e[0] not in present]
    if not missing:
        return 0
    counter = mongo["counters"].find_one_and_update({"_id": COLLECTION}, {"$inc": {"seq": len(missing)}},
                                                    upsert=True, return_document=True)
    first = counter["seq"] - len(missing) + 1
    now = _now()
    coll.insert_many([
        {"_id": first + n, "term": term, "expansions": list(expansions), "languages": list(languages),
         "active": True, "created_by": None, "updated_by": None, "created_at": now, "updated_at": now}
        for n, (term, expansions, languages) in enumerate(missing)])
    mongo["counters"].update_one({"_id": VERSION_ID}, {"$inc": {"seq": 1}}, upsert=True)
    return len(missing)


def apply(mongo: MongoDatabase) -> None:
    options = {"validator": VALIDATORS[COLLECTION], "validationLevel": "strict", "validationAction": "error"}
    if COLLECTION in mongo.list_collection_names():
        mongo.command("collMod", COLLECTION, **options)
    else:
        mongo.create_collection(COLLECTION, **options)
        log.info("created collection %s", COLLECTION)
    for model in INDEXES[COLLECTION]:
        mongo[COLLECTION].create_indexes([model])  # an index already built is left as it is
    log.info("synonyms: %d entries imported", import_entries(mongo))
