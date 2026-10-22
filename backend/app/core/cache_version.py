"""Shared cache versions: how every API process learns that its in-memory caches are stale (P8.1).

Each process keeps search state in memory: the semantic index (app/ai/semantic_index.py), the spelling
vocabulary (app/services/search.py) and the admins' synonyms (app/services/synonyms.py). With two or more
instances, a change handled by one process must reach the others, and MongoDB is the only store they share.

The cache_versions collection holds one small document per cache: _id (the cache's name, such as listings),
version (an integer, 1 or more) and updated_at. Whatever changes what a cache holds calls bump once the
change is committed, which adds one to the version. Every process reads all the versions with one query, at
most once every CHECK_SECONDS per database (VersionWatcher), and each cache compares the version it was built
for with the current one and refreshes when they differ. A change is so visible in every process within
CHECK_SECONDS, and at once in the process that made it (bump forgets this process's copy of the versions).

The names:
  * listings: the search text of listings (created, edited, imported, deactivated or reactivated) and the
    shops admins verify or suspend. The semantic index re-embeds the listings changed since its last
    refresh (by updated_at), and the spelling vocabulary is rebuilt.
  * synonyms: the admins' synonym entries.

A version that goes down (a database reset) or a missing document reads as a change too: caches compare for
equality, never order. When the versions cannot be read, the process keeps the versions it last read, so its
caches stay as they are, and tries again after CHECK_SECONDS; a search never fails because of it.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from pymongo import ReturnDocument
from pymongo.database import Database as MongoDatabase

from app.core.database import utcnow

log = logging.getLogger(__name__)

COLLECTION = "cache_versions"
LISTINGS = "listings"
SYNONYMS = "synonyms"
NAMES = (LISTINGS, SYNONYMS)
CHECK_SECONDS = 5.0

Versions = dict[str, int]


def read_versions(mongo: MongoDatabase) -> Versions:
    """Every cache's current version, in one query (a missing cache has version 0)."""
    return {d["_id"]: int(d.get("version") or 0) for d in mongo[COLLECTION].find({}, {"version": 1})}


class VersionWatcher:
    """This process's view of the versions of each database it uses, re-read at most once every
    check_seconds per database."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, check_seconds: float = CHECK_SECONDS):
        self.clock = clock
        self.check_seconds = check_seconds
        self._lock = threading.Lock()
        self._state: dict[str, tuple[Versions, float]] = {}  # db name -> (versions, checked at)

    def versions(self, mongo: MongoDatabase) -> Versions:
        now = self.clock()
        state = self._state.get(mongo.name)
        if state is not None and now - state[1] < self.check_seconds:
            return state[0]
        with self._lock:
            state = self._state.get(mongo.name)
            if state is not None and now - state[1] < self.check_seconds:
                return state[0]
            try:
                versions = read_versions(mongo)
            except Exception as exc:  # a database hiccup must not fail a search: keep what we had
                log.warning("cache versions: could not read them (%s); keeping the previous ones", exc)
                versions = state[0] if state is not None else {}
            self._state[mongo.name] = (versions, now)
            return versions

    def version(self, mongo: MongoDatabase, name: str) -> int:
        return self.versions(mongo).get(name, 0)

    def forget(self, db_name: str | None = None) -> None:
        """Drop this process's copy of the versions (of one database, or all): the next call re-reads them."""
        with self._lock:
            if db_name is None:
                self._state.clear()
            else:
                self._state.pop(db_name, None)


watcher = VersionWatcher()


def bump(mongo: MongoDatabase, name: str, local: VersionWatcher | None = None) -> int:
    """Record a change to the cache called name, once the change is committed, and return the new version.
    Every process sees it within CHECK_SECONDS; this process (local, the process-wide watcher by default)
    sees it on its next search."""
    if name not in NAMES:
        raise ValueError(f"unknown cache {name!r}; expected one of {', '.join(NAMES)}")
    doc = mongo[COLLECTION].find_one_and_update(
        {"_id": name}, {"$inc": {"version": 1}, "$set": {"updated_at": utcnow()}},
        upsert=True, return_document=ReturnDocument.AFTER, projection={"version": 1})
    (local or watcher).forget(mongo.name)
    return int(doc["version"])
