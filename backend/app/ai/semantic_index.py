"""In-memory semantic index over active product listings.

Many shops list the same item, so vectors are cached per unique text in the `text_embeddings`
collection (`_id` = "<model>:<sha256 of text>") and only new texts are embedded. With a few
thousand listings a NumPy matrix product is fast enough; Atlas Vector Search can replace this later.
Because the key starts with the model name, several models' vectors coexist and switching
NEARSHOP_EMBEDDING_MODEL (P5.3) only embeds what the new model has not seen.

Similarities are returned on the scale search ranks on (config.SEARCH_SIMILARITY_SCALE, the calibration of
all-MiniLM-L6-v2): another model's cosine is mapped there linearly with that model's own floor and ceiling
from config.EMBEDDING_MODELS. With equal calibration, as for every model today, the mapping is the identity.
"""
from __future__ import annotations

import hashlib
import threading
import time

import numpy as np
from bson.binary import Binary

from app.ai.embeddings import get_provider
from app.core.config import SEARCH_SIMILARITY_SCALE, embedding_spec
from app.core.database import Database


def product_text(name: str, brand: str | None, keywords: str, description: str | None) -> str:
    parts = [name]
    if brand and brand.lower() not in name.lower():
        parts.append(brand)
    if keywords:
        parts.append(keywords)
    if description:
        parts.append(description[:240])
    return ". ".join(parts)


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


class SemanticIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dirty = True
        self.product_ids = np.zeros(0, dtype=np.int64)
        self.matrix = np.zeros((0, 1), dtype=np.float32)
        self.provider_name = ""
        self.built_at = 0.0

    def mark_dirty(self) -> None:
        self._dirty = True

    def ensure(self, db: Database) -> None:
        if self._dirty:
            with self._lock:
                if self._dirty:
                    self._build(db)

    def _build(self, db: Database) -> None:
        rows = db.products.find_raw({"is_active": True}, {"name": 1, "brand": 1, "keywords": 1, "description": 1},
                                    sort=[("_id", 1)])
        texts = [product_text(r["name"], r.get("brand"), r.get("keywords") or "", r.get("description")) for r in rows]
        provider = get_provider(fallback_corpus=texts)
        hashes = [_hash(t) for t in texts]

        store = db.mongo["text_embeddings"]
        cached = {
            e["_id"].split(":", 1)[1]: np.frombuffer(e["vector"], dtype=np.float32)
            for e in store.find({"model": provider.name}, {"vector": 1})
        }
        missing = {h: t for h, t in zip(hashes, texts) if h not in cached}
        if missing:
            keys = list(missing)
            vecs = provider.embed([missing[k] for k in keys], "passage")
            cached.update(zip(keys, vecs))
            docs = [{"_id": cache_key(provider.name, missing[k]), "model": provider.name, "dim": provider.dim,
                     "vector": Binary(v.tobytes())} for k, v in zip(keys, vecs)]
            for i in range(0, len(docs), 500):
                store.insert_many(docs[i:i + 500], ordered=False)

        self.product_ids = np.array([r["_id"] for r in rows], dtype=np.int64)
        self.matrix = (
            np.vstack([cached[h] for h in hashes]) if hashes else np.zeros((0, provider.dim), dtype=np.float32)
        )
        self.provider_name = provider.name
        self.built_at = time.time()
        self._dirty = False

    def query(self, db: Database, text: str) -> dict[int, float]:
        """Cosine similarity of `text` against every active listing: {product_id: score}."""
        self.ensure(db)
        provider = get_provider()
        if provider.name != self.provider_name:  # the model setting changed since the build
            self.mark_dirty()
            self.ensure(db)
            provider = get_provider()
        if len(self.product_ids) == 0:
            return {}
        q = provider.embed([text], "query")[0]
        scores = to_search_scale(provider.name, self.matrix @ q)
        return dict(zip(self.product_ids.tolist(), scores.tolist()))


index = SemanticIndex()
