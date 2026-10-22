"""Search synonyms managed by admins (P5.4; plan section 8.5).

The synonyms collection holds one document per term a customer may type: _id (an integer from counters),
term (normalised with app/utils/text.py normalize_query, unique), expansions (the extra words OR'd into the
keyword search, at most MAX_EXPANSIONS, each MAX_EXPANSION_LENGTH characters or fewer), languages (tags such
as en or hi-Latn), active, created_by (the admin's id; null for entries imported by migration 5), created_at,
updated_at and updated_by. Migration 5 imported the 42 entries that used to be a dictionary in code.

Every write bumps two version numbers: the shared synonyms version in cache_versions (app/core/cache_version.py,
P8.1), which every process reads at most once every 5 seconds, and the older counters document synonyms_version,
which migration 5 also bumps and each process still checks at most once every CHECK_SECONDS. Each process keeps
the active entries in memory and re-reads them when either version changes, so an admin's change reaches every
API process within 5 seconds, and immediately in the process that made it. When the database cannot be read,
search keeps the entries the process already has; a process that has none yet searches without synonyms and
tries again after RETRY_SECONDS rather than CHECK_SECONDS.
"""
from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from pymongo import ReturnDocument
from pymongo.database import Database as MongoDatabase
from pymongo.errors import DuplicateKeyError

from app.core import cache_version
from app.core.database import Database, utcnow
from app.core.errors import AppError, Conflict, NotFound

log = logging.getLogger(__name__)

COLLECTION = "synonyms"
VERSION_ID = "synonyms_version"  # the counters document whose seq is the synonyms' version
CHECK_SECONDS = 30.0  # the counters version; the shared version is checked every cache_version.CHECK_SECONDS
RETRY_SECONDS = 5.0  # how soon a failed read is tried again when entries or a shared change are not loaded
MAX_ENTRIES = 500  # every entry, active or not
MAX_EXPANSIONS = 20
MAX_EXPANSION_LENGTH = 40
MAX_TERM_LENGTH = 40
MAX_LANGUAGES = 5
LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?$")  # en, kn, hi-Latn, ...

Table = dict[str, list[str]]


class SynonymInvalid(AppError):
    status_code = 422
    code = "synonym_invalid"


def _normalize(value: str) -> str:
    from app.utils.text import normalize_query  # app.utils.text imports this module lazily

    return normalize_query(value)


def clean_entry(term: str, expansions: list[str], languages: list[str] | None = None) -> dict[str, Any]:
    """The stored form of an entry: term and expansions normalised, duplicates dropped (order kept).
    Raises SynonymInvalid for an empty or too long term, no expansions, too many or too long expansions,
    an expansion equal to the term (a self-loop), or a malformed language tag."""
    norm = _normalize(term or "")
    if not norm:
        raise SynonymInvalid("The term is empty once normalised.")
    if len(norm) > MAX_TERM_LENGTH:
        raise SynonymInvalid(f"The term may be at most {MAX_TERM_LENGTH} characters.")
    values: list[str] = []
    for raw in expansions or []:
        value = _normalize(raw or "")
        if not value:
            continue
        if len(value) > MAX_EXPANSION_LENGTH:
            raise SynonymInvalid(f"Each expansion may be at most {MAX_EXPANSION_LENGTH} characters: {value[:50]!r}.")
        if value == norm:
            raise SynonymInvalid(f"{norm!r} cannot be a synonym of itself.", code="synonym_self_loop")
        if value not in values:
            values.append(value)
    if not values:
        raise SynonymInvalid("Add at least one expansion.")
    if len(values) > MAX_EXPANSIONS:
        raise SynonymInvalid(f"An entry may have at most {MAX_EXPANSIONS} expansions.")
    tags = list(dict.fromkeys(t.strip() for t in (languages or []) if t and t.strip()))
    if len(tags) > MAX_LANGUAGES or any(not LANGUAGE_TAG.match(t) for t in tags):
        raise SynonymInvalid(f"Languages are up to {MAX_LANGUAGES} tags such as en, kn, te, hi or hi-Latn.")
    return {"term": norm, "expansions": values, "languages": tags}


# ---------------------------------------------------------------- reading, with the per-process cache

_override: contextvars.ContextVar[Table | None] = contextvars.ContextVar("nearshop_synonyms_override", default=None)


def load_table(mongo: MongoDatabase) -> Table:
    """term -> expansions for every active entry."""
    return {d["term"]: list(d["expansions"])
            for d in mongo[COLLECTION].find({"active": True}, {"term": 1, "expansions": 1}).sort("term", 1)}


def read_version(mongo: MongoDatabase) -> int:
    doc = mongo["counters"].find_one({"_id": VERSION_ID}, {"seq": 1})
    return int(doc["seq"]) if doc else 0


class SynonymCache:
    """The active entries of each database this process searches, re-read when a version changes: the shared
    synonyms version (read through watcher, at most once every 5 seconds) or the counters version (checked at
    most once every CHECK_SECONDS per database)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 watcher: cache_version.VersionWatcher | None = None):
        self.clock = clock
        self._watcher = watcher  # None: the process-wide cache_version.watcher
        self._lock = threading.Lock()
        # db name -> (table, counters version, checked at, shared version loaded or None, retry at). After a
        # failed read, "retry at" holds the cached entries until RETRY_SECONDS have passed, and the shared
        # version kept is the one the entries were loaded for, so a change not yet loaded is tried again then.
        self._state: dict[str, tuple[Table, int, float, int | None, float]] = {}

    @staticmethod
    def _fresh(state: tuple[Table, int, float, int | None, float] | None, shared: int, now: float) -> bool:
        return state is not None and (now < state[4] or (state[3] == shared and now - state[2] < CHECK_SECONDS))

    def table(self, mongo: MongoDatabase | None = None) -> Table:
        override = _override.get()
        if override is not None:
            return override
        mongo = mongo if mongo is not None else Database().mongo
        shared = (self._watcher or cache_version.watcher).version(mongo, cache_version.SYNONYMS)  # never raises
        now = self.clock()
        state = self._state.get(mongo.name)
        if self._fresh(state, shared, now):
            return state[0]
        with self._lock:
            state = self._state.get(mongo.name)
            if self._fresh(state, shared, now):
                return state[0]
            kept: int | None = shared
            retry_at = 0.0
            try:
                version = read_version(mongo)
                unchanged = state is not None and state[1] == version and state[3] == shared
                table = state[0] if unchanged else load_table(mongo)
            except Exception as exc:  # a database hiccup must not fail a search: keep what we had
                if state is not None and state[1] >= 0:
                    log.warning("synonyms: could not refresh (%s); using the previous entries", exc)
                    table, version, kept = state[0], state[1], state[3]
                else:  # nothing loaded yet: search without synonyms
                    log.warning("synonyms: could not load (%s); retrying in %.0f s", exc, RETRY_SECONDS)
                    table, version, kept = {}, -1, None
                if kept != shared:  # entries not loaded yet, or a shared change not loaded: soon, not in 30 s
                    retry_at = now + RETRY_SECONDS
            self._state[mongo.name] = (table, version, now, kept, retry_at)
            return table

    def invalidate(self, name: str | None = None) -> None:
        """Forget the cached entries (of one database, or all), so the next search re-reads them."""
        with self._lock:
            if name is None:
                self._state.clear()
            else:
                self._state.pop(name, None)


cache = SynonymCache()


@contextmanager
def using_table(table: Table) -> Iterator[None]:
    """Search with this table instead of the database's entries, in this context only (the admin preview)."""
    token = _override.set(table)
    try:
        yield
    finally:
        _override.reset(token)


def expand(q: str, table: Mapping[str, list[str]]) -> list[str]:
    """Expansions of every term that occurs in q as whole words. Never replaces the user's own words."""
    extra: list[str] = []
    for key, values in table.items():
        if re.search(rf"\b{re.escape(key)}\b", q):
            extra.extend(values)
    return extra


# ---------------------------------------------------------------- admin writes


def serialize(doc: Mapping[str, Any]) -> dict[str, Any]:
    from app.schemas.serializers import iso

    return {"id": doc["_id"], "term": doc["term"], "expansions": doc["expansions"],
            "languages": doc.get("languages") or [], "active": doc["active"], "created_by": doc.get("created_by"),
            "created_at": iso(doc.get("created_at")), "updated_at": iso(doc.get("updated_at"))}


def _bump(db: Database) -> None:
    db.mongo["counters"].update_one({"_id": VERSION_ID}, {"$inc": {"seq": 1}}, upsert=True)
    _changed(db)


def _changed(db: Database) -> None:
    """After a committed write: every process re-reads the entries within 5 seconds, this one at once."""
    cache_version.bump(db.mongo, cache_version.SYNONYMS)
    cache.invalidate(db.mongo.name)


def list_entries(db: Database, q: str | None = None) -> dict[str, Any]:
    query: dict = {}
    if q and (norm := _normalize(q)):
        pattern = {"$regex": re.escape(norm)}
        query = {"$or": [{"term": pattern}, {"expansions": pattern}]}
    coll = db.mongo[COLLECTION]
    rows = [serialize(d) for d in coll.find(query).sort([("active", -1), ("term", 1)])]
    return {"entries": rows, "total": coll.count_documents({}), "active": coll.count_documents({"active": True}),
            "limit": MAX_ENTRIES}


def _check_term_free(db: Database, term: str, entry_id: int | None = None) -> None:
    other = db.mongo[COLLECTION].find_one({"term": term, **({"_id": {"$ne": entry_id}} if entry_id else {})},
                                          {"_id": 1})
    if other:
        raise Conflict(f"There is already an entry for {term!r}.", code="synonym_exists")


def create_entry(db: Database, admin_id: int, term: str, expansions: list[str],
                 languages: list[str] | None = None) -> dict[str, Any]:
    entry = clean_entry(term, expansions, languages)
    entry_id = db.next_id(COLLECTION)  # outside the transaction, like every id (a gap after a refusal is harmless)

    def write() -> dict[str, Any]:
        s = db.session
        coll = db.mongo[COLLECTION]
        # The version bump comes first. Every create writes this same counters document, so two creates at
        # once conflict and the driver retries the later one, which then counts the other's entry: the limit
        # check and the insert act as one step and the collection never passes MAX_ENTRIES.
        db.mongo["counters"].update_one({"_id": VERSION_ID}, {"$inc": {"seq": 1}}, upsert=True, session=s)
        if coll.count_documents({}, session=s) >= MAX_ENTRIES:
            raise SynonymInvalid(f"There are already {MAX_ENTRIES} entries, the most allowed. Edit or reuse a "
                                 f"deactivated entry instead.", code="synonym_limit")
        if coll.find_one({"term": entry["term"]}, {"_id": 1}, session=s):
            raise Conflict(f"There is already an entry for {entry['term']!r}.", code="synonym_exists")
        now = utcnow()
        doc = {"_id": entry_id, **entry, "active": True, "created_by": admin_id, "created_at": now,
               "updated_at": now, "updated_by": admin_id}
        coll.insert_one(doc, session=s)
        return doc

    try:
        doc = db.transaction(write)
    except DuplicateKeyError as exc:  # the unique index, when two admins add the same term at once
        raise Conflict(f"There is already an entry for {entry['term']!r}.", code="synonym_exists") from exc
    _changed(db)  # the counters version was bumped inside the transaction
    return serialize(doc)


def update_entry(db: Database, admin_id: int, entry_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
    coll = db.mongo[COLLECTION]
    doc = coll.find_one({"_id": entry_id})
    if doc is None:
        raise NotFound("Synonym entry not found")
    def given(key: str, default: Any) -> Any:
        value = changes.get(key)
        return default if value is None else value

    entry = clean_entry(given("term", doc["term"]), given("expansions", doc["expansions"]),
                        given("languages", doc.get("languages") or []))
    if entry["term"] != doc["term"]:
        _check_term_free(db, entry["term"], entry_id)
    update = {**entry, "updated_at": utcnow(), "updated_by": admin_id}
    if "active" in changes and changes["active"] is not None:
        update["active"] = bool(changes["active"])
    try:
        doc = coll.find_one_and_update({"_id": entry_id}, {"$set": update}, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError as exc:
        raise Conflict(f"There is already an entry for {entry['term']!r}.", code="synonym_exists") from exc
    _bump(db)
    return serialize(doc)


def preview(db: Database, term: str, expansions: list[str], query: str | None = None,
            entry_id: int | None = None, limit: int = 10) -> dict[str, Any]:
    """The top `limit` results for query (default: the term) at the city centre, without the entry and with it
    as given. Without means the active entries minus this term (and minus the entry being edited)."""
    from app.services.meta import city_config
    from app.services.search import SearchParams, run_search

    entry = clean_entry(term, expansions)
    current = dict(cache.table(db.mongo))
    if entry_id is not None:
        old = db.mongo[COLLECTION].find_one({"_id": entry_id}, {"term": 1})
        if old:
            current.pop(old["term"], None)
    current.pop(entry["term"], None)
    with_entry = {**current, entry["term"]: entry["expansions"]}
    center = city_config()["center"]
    params = SearchParams(q=query or entry["term"], lat=center["lat"], lng=center["lng"], page_size=limit)

    def top(table: Table) -> dict[str, Any]:
        with using_table(table):
            res = run_search(db, params, log=False)
        return {"total": res["total"], "expanded_terms": res["expanded_terms"],
                "results": [{"key": g["key"], "name": g["name"], "category": g["category"],
                             "relevance": g["relevance"], "shop_count": g["shop_count"]}
                            for g in res["groups"][:limit]]}

    return {"query": params.q, "term": entry["term"], "expansions": entry["expansions"],
            "center": {"lat": center["lat"], "lng": center["lng"], "radius_km": params.radius_km},
            "without": top(current), "with": top(with_entry)}
