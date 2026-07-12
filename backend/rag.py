"""
rag.py — the retrieval layer (the "R" and the vector store in RAG).

Design choices, stated plainly for interviews:

  - Embeddings are computed LOCALLY with sentence-transformers
    (all-MiniLM-L6-v2, 384-dim). No paid embedding API — the whole system
    runs on one free Groq chat key.

  - NON-BLOCKING MODEL LOAD. The neural model is loaded on a background
    thread. This matters: platforms like Azure App Service kill a container
    that doesn't answer a health probe within ~230s, and loading a model on
    the startup path is exactly how you blow that budget. So the app comes up
    instantly using a dependency-free hashing embedder, then transparently
    upgrades to the neural embedder (re-indexing once) the moment it's ready.

  - The vector store is an in-memory NumPy matrix with brute-force cosine
    similarity. For a catalog of hundreds/thousands of chunks this is instant
    and exact; a production system would swap this for FAISS / a vector DB,
    but the retrieval *interface* here is identical, so that swap is
    localized. This is the honest scope line.
"""

import hashlib
import os
import re
import threading
from dataclasses import dataclass

import numpy as np

_MODEL_NAME = "all-MiniLM-L6-v2"
_HASH_DIM = 512

_model = None                       # SentenceTransformer, once loaded
_model_ready = threading.Event()    # set when the neural model is usable
_load_started = False
_load_lock = threading.Lock()

# Set NEURAL_EMBEDDINGS=0 to force the hashing embedder (e.g. tiny containers).
_NEURAL_ENABLED = os.getenv("NEURAL_EMBEDDINGS", "1").lower() not in ("0", "false", "no")

# Invoked once the neural model becomes ready so the app can re-index with the
# better embeddings. Assigned by app.py.
on_model_ready = None


def _load_model_bg() -> None:
    """Load the neural model off the startup path."""
    global _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
        _model_ready.set()
        if on_model_ready:
            try:
                on_model_ready()
            except Exception:
                pass
    except Exception:
        # library missing, no network, or low memory — stay on the hashing path
        pass


def start_model_load() -> None:
    """Kick off the background load exactly once. Safe to call repeatedly."""
    global _load_started
    if not _NEURAL_ENABLED:
        return
    with _load_lock:
        if _load_started:
            return
        _load_started = True
    threading.Thread(target=_load_model_bg, daemon=True).start()


def _hash_embed(texts: list[str]) -> np.ndarray:
    """
    Dependency-free fallback embedding: hashed bag-of-words (the "hashing
    trick"). Each token is hashed into a fixed-width vector; vectors are
    L2-normalized so dot product == cosine similarity. Lower quality than a
    neural embedder, but zero downloads and zero warmup — which is what lets
    the service answer its health probe immediately.
    """
    vecs = np.zeros((len(texts), _HASH_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for tok in re.findall(r"[a-z0-9_.]+", t.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vecs[i, h % _HASH_DIM] += 1.0
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def _encode(texts: list[str]) -> np.ndarray:
    """Use the neural model if it has finished loading; otherwise hash."""
    if _model_ready.is_set() and _model is not None:
        return _model.encode(texts, convert_to_numpy=True,
                             normalize_embeddings=True).astype(np.float32)
    return _hash_embed(texts)


def embedding_backend() -> str:
    """Which embedder is currently serving — surfaced in /api/health."""
    if _model_ready.is_set():
        return "neural"
    if _NEURAL_ENABLED and _load_started:
        return "hashing (neural loading...)"
    return "hashing"


@dataclass
class Doc:
    id: str            # e.g. "mart.fct_orders"
    text: str          # the chunk of catalog knowledge
    meta: dict         # anything useful for the answer (layer, owner, ...)


class VectorStore:
    """Brute-force cosine-similarity store over normalized embeddings."""

    def __init__(self) -> None:
        self.docs: list[Doc] = []
        self._matrix: np.ndarray | None = None   # (N, dim), L2-normalized

    def build(self, docs: list[Doc]) -> None:
        self.docs = docs
        self._matrix = _encode([d.text for d in docs]) if docs else None

    def search(self, query: str, k: int = 4) -> list[tuple[Doc, float]]:
        if self._matrix is None or not self.docs:
            return []
        q = _encode([query])
        # If the backend flipped between build and query, dimensions differ —
        # rebuild the index with the current encoder, then retry.
        if q.shape[1] != self._matrix.shape[1]:
            self.build(self.docs)
            q = _encode([query])
        scores = self._matrix @ q[0]   # cosine sim on normalized vectors
        top = np.argsort(-scores)[:k]
        return [(self.docs[i], float(scores[i])) for i in top]
