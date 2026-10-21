"""Sentence-embedding providers behind one small interface.

The rest of the app only calls `get_provider().embed(texts, kind)`, so the model can be swapped
(Sentence Transformers via fastembed/ONNX today; OpenAI, BGE, or a PyTorch model later)
without touching search code. The model is chosen by name with NEARSHOP_EMBEDDING_MODEL, from the models
listed in app/core/config.py's EMBEDDING_MODELS (P5.3), each with the query and passage prefixes it needs.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Literal, Protocol

import numpy as np

from app.core.config import TFIDF_MODEL_NAME, embedding_spec, settings

log = logging.getLogger(__name__)


# What a text is: a search query, or a listing's text (a passage). Models trained with prefixes need to know.
TextKind = Literal["query", "passage"]


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str], kind: TextKind = "passage") -> np.ndarray:
        """Return L2-normalised float32 vectors, shape (len(texts), dim)."""
        ...


def _normalise(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


def with_prefix(model_name: str, texts: list[str], kind: TextKind) -> list[str]:
    """The texts as the model expects them: with its query or passage prefix, for models that need one."""
    spec = embedding_spec(model_name)
    prefix = spec.query_prefix if kind == "query" else spec.passage_prefix
    return [prefix + t for t in texts] if prefix else list(texts)


def load_fastembed(model_name: str, cache_dir: str, threads: int | None = None) -> Any:
    """The fastembed model (downloaded into cache_dir on first use). Tests replace this with a fake."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name, cache_dir=cache_dir, threads=threads)


class FastEmbedProvider:
    """A fastembed model run through ONNX Runtime (no PyTorch needed); all-MiniLM-L6-v2 by default."""

    def __init__(self, model_name: str, threads: int | None = None):
        cache = settings.model_dir / "fastembed"
        cache.mkdir(parents=True, exist_ok=True)
        self._model = load_fastembed(model_name, str(cache), threads)
        self.name = model_name
        self.dim = int(self.embed(["probe"]).shape[1])

    def embed(self, texts: list[str], kind: TextKind = "passage") -> np.ndarray:
        vecs = np.array(list(self._model.embed(with_prefix(self.name, texts, kind), batch_size=64)),
                        dtype=np.float32)
        return _normalise(vecs)


class TfidfProvider:
    """Offline fallback: character n-gram TF-IDF projected with SVD. Catches spelling variants,
    not meaning. Used only if the neural model cannot be downloaded/loaded."""

    def __init__(self, corpus: list[str]):
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.name = TFIDF_MODEL_NAME
        self._vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1, sublinear_tf=True)
        x = self._vec.fit_transform(corpus or ["empty"])
        n_comp = max(2, min(256, x.shape[1] - 1, x.shape[0] - 1))
        self._svd = TruncatedSVD(n_components=n_comp, random_state=7).fit(x)
        self.dim = n_comp

    def embed(self, texts: list[str], kind: TextKind = "passage") -> np.ndarray:
        return _normalise(self._svd.transform(self._vec.transform(texts)))


_provider: EmbeddingProvider | None = None
_provider_key: tuple[str, str] | None = None  # the (provider, model) settings _provider was built for
_lock = threading.Lock()


def get_provider(fallback_corpus: list[str] | None = None) -> EmbeddingProvider:
    """The provider named by NEARSHOP_EMBEDDING_PROVIDER and NEARSHOP_EMBEDDING_MODEL, built once per process
    (and again if those settings change)."""
    global _provider, _provider_key
    key = (settings.embedding_provider, settings.embedding_model)
    if _provider is not None and _provider_key == key:
        return _provider
    with _lock:
        if _provider is not None and _provider_key == key:
            return _provider
        _provider_key = key
        if settings.embedding_provider == "fastembed":
            try:
                _provider = FastEmbedProvider(settings.embedding_model)
                log.info("Embedding provider: %s (dim=%d)", _provider.name, _provider.dim)
                return _provider
            except Exception as exc:  # offline / model download failed
                log.warning("fastembed unavailable (%s); falling back to TF-IDF embeddings", exc)
        _provider = TfidfProvider(fallback_corpus or [])
        return _provider


def reset_provider() -> None:
    global _provider, _provider_key
    _provider = None
    _provider_key = None
