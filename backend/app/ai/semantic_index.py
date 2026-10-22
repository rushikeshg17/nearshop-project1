"""In-memory semantic index over active product listings.

Many shops list the same item, so vectors are cached per unique text in the `text_embeddings`
collection (`_id` = "<model>:<sha256 of text>") and only new texts are embedded. With a few
thousand listings a NumPy matrix product is fast enough; Atlas Vector Search can replace this later.
Because the key starts with the model name, several models' vectors coexist and switching
NEARSHOP_EMBEDDING_MODEL (P5.3) only embeds what the new model has not seen.

Similarities are returned on the scale search ranks on (config.SEARCH_SIMILARITY_SCALE, the calibration of
all-MiniLM-L6-v2): another model's cosine is mapped there linearly with that model's own floor and ceiling
from config.EMBEDDING_MODELS. With equal calibration, as for every model today, the mapping is the identity.

Keeping every process in step (P8.1)
------------------------------------
Each API process holds its own index. Whatever changes listings bumps the shared listings version
(app/core/cache_version.py) once the change is committed. Before a search, the index compares the version it
was built for with the current one, which every process reads at most once every 5 seconds, and when they
differ it refreshes incrementally: it re-reads the listings whose updated_at is at or after its watermark,
drops those from the matrix and adds back the ones still active, re-using the cached vectors and embedding
only new texts. The watermark is the time the previous refresh started less OVERLAP, longer than any
transaction may run (60 s) plus the clock difference between instances, so a change committed late is still
caught. Listings are deactivated, never deleted, so the incremental update gives the same index as a full
rebuild. A version that went down (a database reset) or another database rebuilds in full.

A full rebuild still runs on the first search, after mark_dirty (a retrain, a model change, the evaluation
harness) and nightly: once the last full build is older than the latest NIGHTLY_REBUILD_AT (UTC), a search
starts it in a background thread and searches keep using the current matrix until it is done. It also catches
any write that changed a listing without bumping the version.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as clock_time

import numpy as np
from bson.binary import Binary
from pymongo.errors import BulkWriteError

from app.ai.embeddings import EmbeddingProvider, get_provider
from app.core import cache_version
from app.core.config import SEARCH_SIMILARITY_SCALE, embedding_spec
from app.core.database import Database, utcnow

log = logging.getLogger(__name__)

TEXT_FIELDS = {"name": 1, "brand": 1, "keywords": 1, "description": 1}
OVERLAP = timedelta(seconds=120)
NIGHTLY_REBUILD_AT = clock_time(21, 30)  # UTC: 03:00 IST, after the nightly retrain slot (app/jobs.py)
NIGHTLY_RETRY_SECONDS = 600.0  # a nightly rebuild that failed is tried again this much later
VECTOR_BATCH = 1000
DUPLICATE_KEY = 11000


def product_text(name: str, brand: str | None, keywords: str, description: str | None) -> str:
    parts = [name]
    if brand and brand.lower() not in name.lower():
        parts.append(brand)
    if keywords:
        parts.append(keywords)
    if description:
        parts.append(description[:240])
    return ". ".join(parts)


def _row_text(row: dict) -> str:
    return product_text(row["name"], row.get("brand"), row.get("keywords") or "", row.get("description"))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cache_key(model_name: str, text: str) -> str:
    """The text_embeddings _id of a text's vector under one model: "<model>:<sha256 of text>"."""
    return f"{model_name}:{_hash(text)}"


def to_search_scale(model_name: str, sims: np.ndarray) -> np.ndarray:
    """Map one model's cosine similarities onto the scale search ranks on (see the module docstring)."""
    spec, ref = embedding_spec(model_name), SEARCH_SIMILARITY_SCALE
    if (spec.sem_floor, spec.sem_ceil) == (ref.sem_floor, ref.sem_ceil):
        return sims
    return ref.sem_floor + (sims - spec.sem_floor) * ((ref.sem_ceil - ref.sem_floor) / (spec.sem_ceil - spec.sem_floor))


def nightly_slot(now: datetime) -> datetime:
    """The latest NIGHTLY_REBUILD_AT at or before now (naive UTC)."""
    slot = datetime.combine(now.date(), NIGHTLY_REBUILD_AT)
    return slot if slot <= now else slot - timedelta(days=1)


def vectors(db: Database, provider: EmbeddingProvider, texts: list[str]) -> np.ndarray:
    """One vector per text, in order: from text_embeddings where cached, otherwise embedded as passages and
    cached. Another process may cache the same new text at the same moment; that duplicate key is harmless.
    A cached vector of another dimension (a TF-IDF fallback fitted in another process) is not used."""
    dim = provider.dim
    if not texts:
        return np.zeros((0, dim), dtype=np.float32)
    store = db.mongo["text_embeddings"]
    by_key = {cache_key(provider.name, t): t for t in texts}
    keys = list(by_key)
    found: dict[str, np.ndarray] = {}
    for i in range(0, len(keys), VECTOR_BATCH):
        for e in store.find({"_id": {"$in": keys[i:i + VECTOR_BATCH]}}, {"vector": 1}):
            vec = np.frombuffer(e["vector"], dtype=np.float32)
            if vec.shape[0] == dim:
                found[e["_id"]] = vec
    missing = [k for k in keys if k not in found]
    if missing:
        vecs = provider.embed([by_key[k] for k in missing], "passage")
        found.update(zip(missing, vecs))
        docs = [{"_id": k, "model": provider.name, "dim": dim, "vector": Binary(v.tobytes())}
                for k, v in zip(missing, vecs)]
        for i in range(0, len(docs), 500):
            try:
                store.insert_many(docs[i:i + 500], ordered=False)
            except BulkWriteError as exc:
                if any(e.get("code") != DUPLICATE_KEY for e in exc.details.get("writeErrors", [])):
                    raise
    return np.vstack([found[cache_key(provider.name, t)] for t in texts]).astype(np.float32, copy=False)


@dataclass(frozen=True)
class Snapshot:
    """One state of the index: the active listings' ids in ascending order, and their vectors as rows."""

    product_ids: np.ndarray
    matrix: np.ndarray
    provider_name: str
    db_name: str | None  # the database it was built from
    version: int | None  # the shared listings version it reflects (None: never built)
    since: datetime  # the next refresh re-reads the listings updated at or after this (naive UTC)
    full_at: datetime  # when the full build it descends from started (naive UTC)
    built_at: float  # time.time() of its build or refresh


EMPTY = Snapshot(np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32), "", None, None,
                 datetime.min, datetime.min, 0.0)


class SemanticIndex:
    def __init__(self, watcher: cache_version.VersionWatcher | None = None, background: bool = True) -> None:
        self._lock = threading.Lock()
        self._dirty = True
        self._watcher = watcher  # None: the process-wide cache_version.watcher
        self._background = background  # False: the nightly rebuild runs in the search that finds it due
        self._rebuilding: threading.Thread | None = None
        self._nightly_retry_at = 0.0
        self.full_builds = 0
        self.refreshes = 0
        self._install(EMPTY)

    def _install(self, snap: Snapshot) -> None:
        self._snap = snap
        self.product_ids, self.matrix, self.provider_name = snap.product_ids, snap.matrix, snap.provider_name
        self.built_at = snap.built_at

    @property
    def snapshot(self) -> Snapshot:
        return self._snap

    @property
    def watcher(self) -> cache_version.VersionWatcher:
        return self._watcher or cache_version.watcher

    def mark_dirty(self) -> None:
        """Rebuild this process's index in full on its next search (a retrain, a model change). A change to
        listings bumps the shared version instead (cache_version.bump with LISTINGS), which every process sees."""
        self._dirty = True

    # ---- keeping up to date

    def _needs_full(self, db: Database, version: int) -> bool:
        snap = self._snap
        return self._dirty or snap.version is None or snap.db_name != db.mongo.name or version < snap.version

    def ensure(self, db: Database) -> None:
        # The version is read before the listings, so a bump made meanwhile is seen on the next search.
        version = self.watcher.version(db.mongo, cache_version.LISTINGS)
        if self._needs_full(db, version) or self._snap.version != version:
            with self._lock:
                # Again, under the lock: another thread may have refreshed to a newer version meanwhile.
                version = self.watcher.version(db.mongo, cache_version.LISTINGS)
                if self._needs_full(db, version):
                    self._install(self._full(db, version))
                    self._dirty = False
                    return
                if self._snap.version != version:
                    self._install(self._incremental(db, self._snap, version))
        if self.full_rebuild_due() and time.monotonic() >= self._nightly_retry_at:
            self._nightly_retry_at = time.monotonic() + NIGHTLY_RETRY_SECONDS
            self._rebuild_nightly(db.mongo.name)

    def full_rebuild_due(self, now: datetime | None = None) -> bool:
        """Whether the last full build is older than the latest nightly slot."""
        snap = self._snap
        return snap.version is not None and snap.full_at < nightly_slot(now or utcnow())

    def _full(self, db: Database, version: int) -> Snapshot:
        started = utcnow()
        rows = db.products.find_raw({"is_active": True}, TEXT_FIELDS, sort=[("_id", 1)])
        texts = [_row_text(r) for r in rows]
        provider = get_provider(fallback_corpus=texts)
        matrix = vectors(db, provider, texts)
        self.full_builds += 1
        return Snapshot(np.array([r["_id"] for r in rows], dtype=np.int64), matrix, provider.name, db.mongo.name,
                        version, started - OVERLAP, started, time.time())

    def _incremental(self, db: Database, snap: Snapshot, version: int) -> Snapshot:
        started = utcnow()
        provider = get_provider()
        if provider.name != snap.provider_name:  # the model setting changed: every vector changes
            return self._full(db, version)
        rows = db.products.find_raw({"updated_at": {"$gte": snap.since}}, {**TEXT_FIELDS, "is_active": 1},
                                    sort=[("_id", 1)])
        active = [r for r in rows if r.get("is_active")]
        keep = ~np.isin(snap.product_ids, np.array([r["_id"] for r in rows], dtype=np.int64))
        ids = np.concatenate([snap.product_ids[keep], np.array([r["_id"] for r in active], dtype=np.int64)])
        matrix = np.vstack([snap.matrix[keep], vectors(db, provider, [_row_text(r) for r in active])])
        order = np.argsort(ids, kind="stable")
        self.refreshes += 1
        return Snapshot(ids[order], matrix[order], provider.name, snap.db_name, version, started - OVERLAP,
                        snap.full_at, time.time())

    def _rebuild_nightly(self, db_name: str) -> None:
        """The nightly full rebuild. Changes made while it runs are caught by the next incremental refresh,
        as its watermark is its own start less OVERLAP."""
        def run() -> None:
            try:
                db = Database(db_name)
                fresh = self._full(db, self.watcher.version(db.mongo, cache_version.LISTINGS))
                with self._lock:
                    if self._snap.db_name == db_name and not self._dirty and fresh.full_at > self._snap.full_at:
                        self._install(fresh)
                log.info("semantic index: nightly rebuild done, %d listings", len(fresh.product_ids))
            except Exception as exc:  # searches keep the current index; tried again NIGHTLY_RETRY_SECONDS later
                log.warning("semantic index: nightly rebuild failed: %s", exc)

        if not self._background:
            run()
            return
        with self._lock:
            if self._rebuilding is not None and self._rebuilding.is_alive():
                return
            self._rebuilding = threading.Thread(target=run, name="semantic-index-rebuild", daemon=True)
            self._rebuilding.start()

    def wait_for_rebuild(self, timeout: float | None = None) -> None:
        """Wait for a nightly rebuild running in the background, if any (tests)."""
        thread = self._rebuilding
        if thread is not None:
            thread.join(timeout)

    # ---- searching

    def query(self, db: Database, text: str) -> dict[int, float]:
        """Cosine similarity of `text` against every active listing: {product_id: score}."""
        self.ensure(db)
        provider = get_provider()
        if provider.name != self._snap.provider_name:  # the model setting changed since the build
            self.mark_dirty()
            self.ensure(db)
            provider = get_provider()
        snap = self._snap  # one consistent state, whatever another thread installs meanwhile
        if len(snap.product_ids) == 0:
            return {}
        q = provider.embed([text], "query")[0]
        scores = to_search_scale(provider.name, snap.matrix @ q)
        return dict(zip(snap.product_ids.tolist(), scores.tolist()))


index = SemanticIndex()
