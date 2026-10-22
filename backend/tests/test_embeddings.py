"""Embedding model selection, per-model cache keys, query/passage prefixes and calibration (P5.3).

No model is downloaded: load_fastembed is replaced with a fake that records what it was asked to embed.
The real provider and the global index are left as they were (monkeypatch restores the module globals).
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest
from pydantic import ValidationError

from app.ai import embeddings
from app.ai.embeddings import FastEmbedProvider, TfidfProvider, get_provider, with_prefix
from app.ai.semantic_index import SemanticIndex, cache_key, to_search_scale
from app.core.config import (
    EMBEDDING_MODELS,
    SEARCH_SIMILARITY_SCALE,
    EmbeddingModelSpec,
    Settings,
    embedding_spec,
    settings,
)
from app.services import search

CURRENT = "sentence-transformers/all-MiniLM-L6-v2"
MULTILINGUAL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
E5 = "intfloat/multilingual-e5-large"


class FakeModel:
    """Stands in for fastembed's TextEmbedding: a deterministic 8-dimensional vector per text."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.seen: list[str] = []

    def embed(self, texts, batch_size: int = 64):
        for t in texts:
            self.seen.append(t)
            seed = int(hashlib.sha256(f"{self.model_name}|{t}".encode()).hexdigest()[:8], 16)
            yield np.random.default_rng(seed).normal(size=8).astype(np.float32)


@pytest.fixture
def fake_fastembed(monkeypatch):
    """Swap in the fake model and keep the process-wide provider untouched by the test."""
    loaded: dict[str, FakeModel] = {}

    def load(model_name: str, cache_dir: str, threads: int | None = None) -> FakeModel:
        loaded[model_name] = FakeModel(model_name)
        return loaded[model_name]

    monkeypatch.setattr(embeddings, "load_fastembed", load)
    monkeypatch.setattr(embeddings, "_provider", None)
    monkeypatch.setattr(embeddings, "_provider_key", None)
    monkeypatch.setattr(settings, "embedding_provider", "fastembed")
    monkeypatch.setattr(settings, "embedding_model", CURRENT)
    return loaded


def test_the_provider_and_model_are_chosen_by_config(fake_fastembed, monkeypatch):
    first = get_provider()
    assert isinstance(first, FastEmbedProvider) and first.name == CURRENT
    assert get_provider() is first  # built once while the settings stay the same

    monkeypatch.setattr(settings, "embedding_model", MULTILINGUAL)
    second = get_provider()
    assert isinstance(second, FastEmbedProvider) and second.name == MULTILINGUAL
    assert set(fake_fastembed) == {CURRENT, MULTILINGUAL}

    monkeypatch.setattr(settings, "embedding_provider", "tfidf")
    fallback = get_provider(fallback_corpus=["usb charger", "steel tape", "rice bag"])
    assert isinstance(fallback, TfidfProvider)


def test_a_failed_model_load_falls_back_to_tfidf(fake_fastembed, monkeypatch):
    def broken(model_name: str, cache_dir: str, threads: int | None = None):
        raise OSError("offline")

    monkeypatch.setattr(embeddings, "load_fastembed", broken)
    assert isinstance(get_provider(fallback_corpus=["usb charger", "steel tape", "rice bag"]), TfidfProvider)


def test_only_listed_models_can_be_selected():
    assert Settings(embedding_model=MULTILINGUAL).embedding_model == MULTILINGUAL
    with pytest.raises(ValidationError, match="NEARSHOP_EMBEDDING_MODEL"):
        Settings(embedding_model="sentence-transformers/not-a-listed-model")


def test_every_listed_model_is_supported_by_the_installed_fastembed():
    from fastembed import TextEmbedding

    supported = {m["model"] for m in TextEmbedding.list_supported_models()}
    assert set(EMBEDDING_MODELS) <= supported


def test_cache_keys_differ_per_model():
    text = "USB-C fast charger. Mi. phone charger adapter"
    assert cache_key(CURRENT, text) == cache_key(CURRENT, text)
    assert cache_key(CURRENT, text) != cache_key(MULTILINGUAL, text)
    assert cache_key(CURRENT, text).startswith(f"{CURRENT}:")
    assert cache_key(CURRENT, text) != cache_key(CURRENT, text + " ")


def test_two_models_vectors_coexist_in_the_cache(fake_fastembed, monkeypatch, db, ids):
    store = db.mongo["text_embeddings"]
    sem = SemanticIndex()
    sem.ensure(db)
    assert sem.provider_name == CURRENT
    first_count = store.count_documents({"model": CURRENT})
    assert 0 < first_count <= len(sem.product_ids)

    monkeypatch.setattr(settings, "embedding_model", MULTILINGUAL)
    scores = sem.query(db, "phone charger")  # notices the model change and rebuilds for it
    assert sem.provider_name == MULTILINGUAL and ids["charger"] in scores
    assert store.count_documents({"model": MULTILINGUAL}) == first_count
    assert store.count_documents({"model": CURRENT}) == first_count  # the old model's vectors stay
    ids_current = {d["_id"] for d in store.find({"model": CURRENT}, {"_id": 1})}
    ids_multi = {d["_id"] for d in store.find({"model": MULTILINGUAL}, {"_id": 1})}
    assert not ids_current & ids_multi
    store.delete_many({"model": {"$in": [CURRENT, MULTILINGUAL]}})


def test_prefixes_are_applied_only_to_models_that_need_them(fake_fastembed, monkeypatch):
    assert with_prefix(CURRENT, ["phone charger"], "query") == ["phone charger"]
    assert with_prefix(MULTILINGUAL, ["phone charger"], "passage") == ["phone charger"]
    assert with_prefix(E5, ["phone charger"], "query") == ["query: phone charger"]
    assert with_prefix(E5, ["phone charger"], "passage") == ["passage: phone charger"]
    needing = {name for name, spec in EMBEDDING_MODELS.items() if spec.query_prefix or spec.passage_prefix}
    assert needing == {E5}

    plain = FastEmbedProvider(CURRENT)
    plain.embed(["ಚಾರ್ಜರ್"], "query")
    plain.embed(["Phone charger. Mi"], "passage")
    assert fake_fastembed[CURRENT].seen == ["probe", "ಚಾರ್ಜರ್", "Phone charger. Mi"]

    e5 = FastEmbedProvider(E5)
    e5.embed(["ಚಾರ್ಜರ್"], "query")
    e5.embed(["Phone charger. Mi"], "passage")
    assert fake_fastembed[E5].seen == ["passage: probe", "query: ಚಾರ್ಜರ್", "passage: Phone charger. Mi"]


def test_the_semantic_index_embeds_listings_as_passages_and_searches_as_queries(fake_fastembed, monkeypatch, db, ids):
    monkeypatch.setattr(settings, "embedding_model", E5)
    sem = SemanticIndex()
    sem.query(db, "phone charger")
    seen = fake_fastembed[E5].seen
    assert seen[-1] == "query: phone charger"
    assert all(t.startswith("passage: ") for t in seen[:-1])
    db.mongo["text_embeddings"].delete_many({"model": E5})


def test_calibration_lives_in_config_and_matches_search():
    assert (SEARCH_SIMILARITY_SCALE.sem_floor, SEARCH_SIMILARITY_SCALE.sem_ceil) == (search.SEM_FLOOR, search.SEM_CEIL)
    assert embedding_spec(CURRENT) == SEARCH_SIMILARITY_SCALE
    # Not tuned in P5.3: every model is on the current calibration, so similarities pass through unchanged.
    sims = np.array([0.1, 0.22, 0.5, 0.62, 0.9], dtype=np.float32)
    for name in EMBEDDING_MODELS:
        assert np.array_equal(to_search_scale(name, sims), sims)
    assert embedding_spec("tfidf-char-svd") == EmbeddingModelSpec()


def test_another_calibration_maps_onto_the_search_scale(monkeypatch):
    monkeypatch.setitem(EMBEDDING_MODELS, MULTILINGUAL, EmbeddingModelSpec(sem_floor=0.40, sem_ceil=0.80))
    mapped = to_search_scale(MULTILINGUAL, np.array([0.40, 0.60, 0.80], dtype=np.float32))
    assert np.allclose(mapped, [search.SEM_FLOOR, (search.SEM_FLOOR + search.SEM_CEIL) / 2, search.SEM_CEIL])
