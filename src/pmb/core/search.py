"""
Hybrid Search - BM25 + dense vector retrieval.

Strategy:
- Embeddings: sentence-transformers (local)
- BM25: rank-bm25
- Fusion: weighted normalized scores (default 50/50)
- Ranking signals: similarity + importance + recency

Storage:
- Vectors in LanceDB (ulid → embedding)
- BM25 - in-memory rebuild on cold start (fast, ~100-1000 events)
"""

from __future__ import annotations

import os
import pickle
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from rank_bm25 import BM25Okapi

# Bump when the on-disk pickle layout becomes incompatible
_BM25_CACHE_VERSION = 2

# Heavy deps loaded lazily - importing them at module top makes any CLI
# command cold-start in 30-60s on Windows even when no search is needed.
if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer  # noqa

# Default multilingual embedding model - handles 50+ languages including
# English, Russian, Spanish, Chinese, etc. Cross-lingual recall works: a
# Russian query matches an English memory and vice versa.
#
# Model size: ~120 MB (vs 80 MB for English-only). One-time download.
# Speed: ~5% slower than English-only on CPU. Negligible.
#
# Why not e5 / bge multilingual? Those are 200-500 MB. This balances quality
# vs disk footprint. Users who want stronger models can override via:
#   pmb config embedding.model_name "intfloat/multilingual-e5-base"
#
# IMPORTANT: changing the model after events are indexed requires re-encoding
# (run `pmb reindex`). Different models produce incompatible vectors.
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_MODEL_ENGLISH_ONLY = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"  # ~80 MB, ~50ms/25 pairs CPU
EMBED_DIM = 384


def _lancedb():
    import lancedb  # local import - first call pays the cost
    return lancedb


def _SentenceTransformer():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer


def _CrossEncoder():
    from sentence_transformers import CrossEncoder
    return CrossEncoder


def _TextEmbeddingFE():
    """Lazy import of fastembed.TextEmbedding."""
    from fastembed import TextEmbedding
    return TextEmbedding


class _FastEmbedAdapter:
    """Make fastembed.TextEmbedding look like a sentence-transformers model.

    sentence-transformers offers `.encode(text|texts) -> np.ndarray`.
    fastembed offers `.embed(texts) -> generator of np.ndarray`. We wrap
    it so the rest of HybridSearch doesn't need to know the difference.
    """

    def __init__(self, model_name: str):
        # Default fastembed model id maps the same MiniLM weights via ONNX.
        TextEmbedding = _TextEmbeddingFE()
        self._impl = TextEmbedding(model_name=model_name)

    def encode(self, texts, show_progress_bar=False, batch_size: int = 32):
        if isinstance(texts, str):
            single = True
            inputs = [texts]
        else:
            single = False
            inputs = list(texts)
        vecs = list(self._impl.embed(inputs, batch_size=batch_size))
        arr = np.stack(vecs).astype(np.float32) if vecs else np.zeros(
            (0, EMBED_DIM), dtype=np.float32,
        )
        return arr[0] if single else arr


class _OllamaEmbedAdapter:
    """Embed via a local Ollama server - fully offline, no Python ML deps.

    Exposes the same `.encode()` contract as sentence-transformers so the
    rest of HybridSearch is backend-agnostic. Vector dim depends on the
    model (nomic-embed-text → 768), so switching to this on a workspace that
    already has 384-dim vectors requires a fresh workspace / reindex - the
    dimension guard in HybridSearch.add enforces this loudly.
    """

    def __init__(self, model_name: str = "nomic-embed-text",
                 base_url: str = "http://localhost:11434"):
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self._dim: int | None = None

    def _embed_one(self, text: str) -> np.ndarray:
        import json as _json
        import urllib.request
        body = _json.dumps({"model": self.model_name, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/embeddings", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = _json.loads(r.read())
        vec = np.asarray(data.get("embedding") or [], dtype=np.float32)
        if vec.size == 0:
            raise RuntimeError(f"Ollama returned empty embedding for model {self.model_name!r}")
        self._dim = int(vec.shape[0])
        return vec

    def encode(self, texts, show_progress_bar=False, batch_size: int = 32):
        single = isinstance(texts, str)
        inputs = [texts] if single else list(texts)
        if not inputs:
            return np.zeros((0, self._dim or EMBED_DIM), dtype=np.float32)
        out = np.stack([self._embed_one(t) for t in inputs]).astype(np.float32)
        return out[0] if single else out

    def get_sentence_embedding_dimension(self) -> int | None:
        return self._dim


class _OpenAIEmbedAdapter:
    """Embed via the OpenAI embeddings API. Needs OPENAI_API_KEY in the env.

    Uses urllib (no openai SDK dependency). text-embedding-3-small → 1536-dim.
    """

    def __init__(self, model_name: str = "text-embedding-3-small"):
        self.model_name = model_name
        self._dim: int | None = None
        self._key = os.environ.get("OPENAI_API_KEY")

    def encode(self, texts, show_progress_bar=False, batch_size: int = 32):
        import json as _json
        import urllib.request
        single = isinstance(texts, str)
        inputs = [texts] if single else list(texts)
        if not inputs:
            return np.zeros((0, self._dim or EMBED_DIM), dtype=np.float32)
        if not self._key:
            raise RuntimeError(
                "backend=openai needs OPENAI_API_KEY in the environment"
            )
        vecs: list[np.ndarray] = []
        for i in range(0, len(inputs), batch_size):
            chunk = inputs[i:i + batch_size]
            body = _json.dumps({"model": self.model_name, "input": chunk}).encode()
            req = urllib.request.Request(
                "https://api.openai.com/v1/embeddings", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self._key}"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                data = _json.loads(r.read())
            for row in data.get("data", []):
                vecs.append(np.asarray(row["embedding"], dtype=np.float32))
        if not vecs:
            raise RuntimeError("OpenAI returned no embeddings")
        self._dim = int(vecs[0].shape[0])
        out = np.stack(vecs).astype(np.float32)
        return out[0] if single else out

    def get_sentence_embedding_dimension(self) -> int | None:
        return self._dim


def _read_table_vector_dim(tbl) -> int | None:
    """Read the fixed vector dimension of an existing LanceDB 'events' table.

    Returns None if it can't be determined (we then skip the dim guard rather
    than blocking the user on an introspection quirk)."""
    try:
        field = tbl.schema.field("vector")
        size = getattr(field.type, "list_size", None)
        return int(size) if size and size > 0 else None
    except Exception:
        return None


def _model_dim(model) -> int:
    """Best-effort embedding dimension for any backend's model object."""
    # sentence-transformers renamed the method; try the new name first to
    # avoid a FutureWarning, then the legacy name, then a probe.
    for attr in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                d = fn()
                if d:
                    return int(d)
            except Exception:
                pass
    try:
        v = np.asarray(model.encode(["x"], show_progress_bar=False))
        return int(v.reshape(v.shape[0], -1).shape[1]) if v.ndim > 1 else int(v.shape[0])
    except Exception:
        return EMBED_DIM


@dataclass
class SearchHit:
    ulid: str
    score: float
    bm25_score: float
    vec_score: float
    importance: float
    recency_score: float
    # R3: ABSOLUTE evidence - the un-normalized vector similarity 1/(1+dist) in
    # [0,1]. `score`/`vec_score` are min-max normalized over the candidate set
    # (great for RANKING, meaningless as an absolute threshold). raw_vec is the
    # real "how close is this actually" signal that gates can trust.
    raw_vec: float = 0.0


def tokenize(text: str) -> list[str]:
    """Simple tokenizer. Preserves abbreviations via case-folding."""
    return re.findall(r"\w+", text.lower())


def normalize(scores: np.ndarray) -> np.ndarray:
    """Min-max into [0, 1]."""
    if len(scores) == 0:
        return scores
    lo, hi = float(scores.min()), float(scores.max())
    if hi - lo < 1e-10:
        return np.zeros_like(scores, dtype=np.float32)
    return ((scores - lo) / (hi - lo)).astype(np.float32)


def cosine_similarity(q: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Cosine similarity between q (1d) and m (2d)."""
    if len(m) == 0:
        return np.array([], dtype=np.float32)
    q_norm = q / (np.linalg.norm(q) + 1e-10)
    m_norm = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-10)
    return np.dot(m_norm, q_norm).astype(np.float32)


class _ModelCache:
    """Global embedding-model cache. Supports both backends."""

    _model = None  # type: ignore[var-annotated]
    _name: str | None = None
    _backend: str | None = None
    _base_url: str | None = None
    _lock = threading.RLock()

    @classmethod
    def peek(
        cls, model_name: str = DEFAULT_MODEL,
        backend: str = "sentence-transformers",
        base_url: str | None = None,
    ):
        """Return the cached model ONLY if already constructed for this exact
        (name, backend, base_url) - NEVER triggers a load. Lets the passive
        embed-queue worker attach instantly when another consumer in this
        process already paid the load, while a truly cold process stays
        load-free (the Codex 120s-timeout fix)."""
        with cls._lock:
            if (cls._model is not None
                    and cls._name == model_name
                    and cls._backend == backend
                    and cls._base_url == base_url):
                return cls._model
            return None

    @classmethod
    def get(
        cls, model_name: str = DEFAULT_MODEL,
        backend: str = "sentence-transformers",
        base_url: str | None = None,
    ):
        with cls._lock:
            if (cls._model is None
                    or cls._name != model_name
                    or cls._backend != backend
                    or cls._base_url != base_url):
                if backend == "fastembed":
                    cls._model = _FastEmbedAdapter(model_name)
                elif backend == "ollama":
                    cls._model = _OllamaEmbedAdapter(
                        model_name, base_url or "http://localhost:11434")
                elif backend == "openai":
                    cls._model = _OpenAIEmbedAdapter(model_name)
                else:
                    cls._model = _SentenceTransformer()(model_name)
                cls._name = model_name
                cls._backend = backend
                cls._base_url = base_url
            return cls._model


class _CrossEncoderCache:
    """Global cross-encoder reranker cache. Separate from the embedder's."""

    _model = None  # type: ignore[var-annotated]
    _name: str | None = None
    _lock = threading.RLock()

    @classmethod
    def get(cls, model_name: str = DEFAULT_RERANK_MODEL):
        with cls._lock:
            if cls._model is None or cls._name != model_name:
                cls._model = _CrossEncoder()(model_name)
                cls._name = model_name
            return cls._model


class HybridSearch:
    """
    Hybrid index: BM25 + vector search.

    Lifecycle:
      add(ulid, text) - add a document
      build() - rebuild BM25 (call after a bulk add)
      search(query, top_k) - find

    Vectors are stored in LanceDB (persistent), BM25 - in memory (rebuild on init).
    """

    def __init__(
        self,
        vector_path: Path,
        model_name: str = DEFAULT_MODEL,
        bm25_weight: float = 0.5,
        rerank_model_name: str | None = None,
        embedding_backend: str = "sentence-transformers",
        embedding_base_url: str | None = None,
    ):
        self.vector_path = vector_path
        self.model_name = model_name
        self.bm25_weight = bm25_weight
        self.vec_weight = 1.0 - bm25_weight
        self.embedding_backend = embedding_backend
        self.embedding_base_url = embedding_base_url
        # Vector dimension of the active LanceDB table - set when the table is
        # opened/created. None until then. Guards against mixing embedders of
        # different dims in one workspace (would corrupt recall).
        self._table_dim: int | None = None
        # Reranker is fully opt-in - None disables it entirely (no load)
        self.rerank_model_name = rerank_model_name

        # Model is lazy - avoid the 1-3s load for CLI commands that never embed
        # (stats, list, pin, forget, session, schedule, workspaces, doctor).
        self._model = None
        self._reranker = None

        # LanceDB is lazy too: `import lancedb` alone takes ~22 s on Windows
        # (pulls in pyarrow + torch transitively). We defer the import and the
        # `connect()` call to the first read/write operation that needs vectors.
        # CLI commands like `pmb stats`, `pmb list`, `pmb pin`, `pmb forget`,
        # and `pmb config` never touch vectors, so they construct an Engine
        # in <500 ms instead of 14 s.
        self.vector_path.parent.mkdir(parents=True, exist_ok=True)
        self._lance = None
        self._table_obj = None

        # BM25 (in-memory). Built lazily: the first call to `search()` or
        # `add()` triggers `reload_bm25()` so we don't pay the LanceDB import
        # cost during Engine() construction.
        self._bm25: BM25Okapi | None = None
        self._bm25_ulids: list[str] = []
        self._bm25_tokens: list[list[str]] = []
        self._bm25_reloaded = False
        # mtime of the on-disk BM25 cache the last time WE loaded/saved it.
        # A long-running daemon uses this to notice out-of-band writes (a CLI
        # `pmb fact` / `index` / `track` while the daemon is warm) and refresh,
        # instead of serving a stale in-RAM index until restart.
        self._bm25_cache_mtime = 0.0

    @property
    def _table(self):
        """Lazy LanceDB table accessor. First call triggers `import lancedb`
        (~22 s) + opens/creates the on-disk table (~100 ms). Subsequent
        calls are constant-time attribute reads.
        """
        if self._table_obj is None:
            self._lance = _lancedb().connect(str(self.vector_path))
            self._table_obj = self._open_or_create_table()
        return self._table_obj

    def is_vector_index_loaded(self) -> bool:
        """True if LanceDB has been opened and table is ready."""
        return self._table_obj is not None

    def _open_or_create_table(self):
        # Newer lancedb exposes list_tables() returning a response object with
        # a `.tables` attribute; older versions only have table_names().
        existing: list[str] = []
        used_modern = False
        if hasattr(self._lance, "list_tables"):
            try:
                resp = self._lance.list_tables()
                existing = list(getattr(resp, "tables", resp) or [])
                used_modern = True
            except Exception:
                used_modern = False
        if not used_modern and hasattr(self._lance, "table_names"):
            existing = list(self._lance.table_names() or [])
        if "events" in existing:
            tbl = self._lance.open_table("events")
            self._table_dim = _read_table_vector_dim(tbl)
            return tbl
        # Create with the ACTIVE embedder's dimension (not a hardcoded 384):
        # by the time the table is created the query/event has already been
        # encoded, so the model is loaded and we can ask it its real dim. This
        # lets ollama (768) / openai (1536) backends work on a fresh workspace.
        dim = EMBED_DIM
        if self._model is not None:
            try:
                dim = _model_dim(self._model)
            except Exception:
                dim = EMBED_DIM
        self._table_dim = dim
        empty_data = [{
            "ulid": "_init",
            "vector": np.zeros(dim, dtype=np.float32).tolist(),
            "text": "",
        }]
        tbl = self._lance.create_table("events", data=empty_data)
        # Remove the dummy row
        tbl.delete("ulid = '_init'")
        return tbl

    @property
    def model(self):
        """Lazy embedding-model accessor (first call loads ~80MB)."""
        if self._model is None:
            self._model = _ModelCache.get(
                self.model_name, self.embedding_backend,
                base_url=self.embedding_base_url,
            )
        return self._model

    def attach_cached_model(self) -> bool:
        """Adopt the process-wide cached model if (and only if) it is ALREADY
        constructed - never triggers a load. True when ready afterwards."""
        if self._model is not None:
            return True
        cached = _ModelCache.peek(
            self.model_name, self.embedding_backend,
            base_url=self.embedding_base_url,
        )
        if cached is not None:
            self._model = cached
            return True
        return False

    def is_ready(self) -> bool:
        """True if the embedding model is loaded into memory and ready.

        Improvement W: callers can use this to decide between inline embed
        (fast) and deferred-async embed (write SQLite now, embed later).
        """
        return self._model is not None

    @property
    def reranker(self):
        """Lazy cross-encoder accessor; returns None if reranker disabled."""
        if not self.rerank_model_name:
            return None
        if self._reranker is None:
            self._reranker = _CrossEncoderCache.get(self.rerank_model_name)
        return self._reranker

    def rerank(self, query: str, hits: list[SearchHit],
               text_for_ulid) -> list[SearchHit]:
        """
        Re-score `hits` using the cross-encoder, return them sorted desc by score.

        `text_for_ulid(ulid) -> str` should return the indexable text of an
        event (callers usually pass `lambda u: events.get_by_ulid(u).to_text()`
        or use an already-built map).

        If the reranker isn't configured, returns hits unchanged. If only one
        hit is given, returns it unchanged (nothing to reorder).
        """
        if self.reranker is None or len(hits) <= 1:
            return hits
        pairs: list[tuple[str, str]] = []
        for h in hits:
            try:
                text = text_for_ulid(h.ulid) or ""
            except Exception:
                text = ""
            pairs.append((query, text[:512]))  # cap each side for speed
        try:
            scores = self.reranker.predict(pairs, show_progress_bar=False)
        except Exception:
            # Never fail recall because reranker tripped
            return hits
        # The CrossEncoder returns logits; higher = more relevant. Update score
        # field so callers can see the new ordering basis.
        rescored: list[SearchHit] = []
        for h, s in zip(hits, scores):
            rescored.append(SearchHit(
                ulid=h.ulid, score=float(s),
                bm25_score=h.bm25_score, vec_score=h.vec_score,
                importance=h.importance, recency_score=h.recency_score,
                raw_vec=h.raw_vec,
            ))
        rescored.sort(key=lambda x: -x.score)
        return rescored

    def embed(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        # Every workspace shares the cached model. Concurrent inference from
        # recall and the embed worker can crash the native MPS runtime.
        with _ModelCache._lock:
            return self.model.encode(texts, show_progress_bar=False, batch_size=32).astype(np.float32)

    def _guard_dim(self, vec_dim: int) -> None:
        """Refuse to write a vector whose dimension doesn't match the table.

        Mixing embedders of different dims (e.g. MiniLM=384 then OpenAI=1536)
        in one workspace silently corrupts recall. We fail loudly with a fix
        instead. Touching `self._table` here also resolves `self._table_dim`.
        """
        _ = self._table  # ensure table opened → _table_dim populated
        td = self._table_dim
        if td is not None and vec_dim != td:
            raise RuntimeError(
                f"Embedding dimension mismatch: this workspace's vector index "
                f"is {td}-dim but the active embedder produces {vec_dim}-dim "
                f"vectors. Switching embedding backends needs a FRESH workspace "
                f"(set PMB_WORKSPACE to a new id) or a full re-embed into one. "
                f"Mixing dims would corrupt recall."
            )

    def add(self, ulid: str, text: str) -> None:
        """Add a single document. Does not rebuild BM25 - call build() after a batch."""
        # Make sure BM25 in-memory state is loaded before we append to it,
        # otherwise the first write after a process start would silently
        # drop pre-existing events.
        if not self._bm25_reloaded:
            self.reload_bm25()
        emb = self.embed(text)
        self._guard_dim(len(emb))
        self._table.add([{
            "ulid": ulid,
            "vector": emb.tolist(),
            "text": text,
        }])
        # Append into BM25 in-memory state. Skip the BM25 append if a prior
        # add_bm25_only() already listed this ulid (cold-write fast path), so
        # the deferred vector embed doesn't double-list it; still persist the
        # vector add above.
        if ulid not in self._bm25_ulids:
            self._bm25_ulids.append(ulid)
            self._bm25_tokens.append(tokenize(text))
            self._bm25 = None  # invalidate, rebuild on next search
        self._save_bm25_cache()

    def add_bm25_only(self, ulid: str, text: str) -> None:
        """Add a doc to the BM25 index WITHOUT embedding (no model needed).

        Lets a COLD writer (a one-shot CLI with no model loaded) make a new
        event lexically searchable immediately and persist it to the on-disk
        pickle, while the vector embed stays deferred to the durable queue. The
        pickle mtime bump also lets a warm daemon notice the out-of-band write
        and reload (see _refresh_if_stale). Idempotent: a later full add() for
        the same ulid adds only the vector."""
        if not self._bm25_reloaded:
            self.reload_bm25()
        if ulid in self._bm25_ulids:
            return
        self._bm25_ulids.append(ulid)
        self._bm25_tokens.append(tokenize(text))
        self._bm25 = None
        self._save_bm25_cache()

    def add_batch(self, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        if not self._bm25_reloaded:
            self.reload_bm25()
        texts = [t for _, t in items]
        embs = self.embed_batch(texts)
        if len(embs):
            self._guard_dim(int(embs.shape[1]))
        rows = [
            {"ulid": ulid, "vector": emb.tolist(), "text": text}
            for (ulid, text), emb in zip(items, embs)
        ]
        self._table.add(rows)
        for ulid, text in items:
            self._bm25_ulids.append(ulid)
            self._bm25_tokens.append(tokenize(text))
        self._bm25 = None
        self._save_bm25_cache()

    def remove(self, ulid: str) -> None:
        self._table.delete(f"ulid = '{ulid}'")
        if ulid in self._bm25_ulids:
            idx = self._bm25_ulids.index(ulid)
            self._bm25_ulids.pop(idx)
            self._bm25_tokens.pop(idx)
            self._bm25 = None
            self._save_bm25_cache()

    def reindex_all(self, events_iter, batch_size: int = 64) -> int:
        """Re-embed all events with the current model. Use when switching
        embedding models (e.g. English-only → multilingual).

        Args:
          events_iter: iterable of Event objects (from engine.events.list_active)
          batch_size: how many to embed at once

        Returns: number of events re-encoded.
        """
        # Drop all vectors - table will be repopulated. If the active embedder
        # changed dimensions, recreate the table too; LanceDB vector dimensions
        # are fixed in the schema, so deleting rows is not enough.
        target_dim = _model_dim(self.model)
        try:
            _ = self._table  # open existing table and populate _table_dim
            if self._table_dim is not None and self._table_dim != target_dim:
                self._lance.drop_table("events")
                self._table_obj = None
                self._table_dim = None
            else:
                self._table.delete("ulid IS NOT NULL")  # drop all rows
        except Exception:
            pass
        # Reset BM25 state
        self._bm25_ulids = []
        self._bm25_tokens = []
        self._bm25 = None

        n = 0
        batch: list[tuple[str, str]] = []
        for ev in events_iter:
            text = ev.to_text() if hasattr(ev, "to_text") else (ev.content or "")
            batch.append((ev.ulid, text))
            if len(batch) >= batch_size:
                self.add_batch(batch)
                n += len(batch)
                batch.clear()
        if batch:
            self.add_batch(batch)
            n += len(batch)
        return n

    @property
    def _bm25_cache_path(self) -> Path:
        return self.vector_path.parent / "bm25_index.pkl"

    def _save_bm25_cache(self) -> None:
        """Persist BM25 state - both tokens AND the fitted BM25Okapi.

        Storing the fitted index (not just tokens) lets cold-start skip the
        BM25 rebuild on the first search. For 5000+ events this saves
        100-300ms per CLI invocation. We always serialise the tokens too in
        case BM25Okapi unpickling fails on a different rank_bm25 version -
        the loader will rebuild from tokens.
        """
        try:
            payload = {
                "version": _BM25_CACHE_VERSION,
                "ulids": self._bm25_ulids,
                "tokens": self._bm25_tokens,
                "fitted": self._bm25,  # may be None if not yet built
            }
            tmp = self._bm25_cache_path.with_suffix(".pkl.tmp")
            with open(tmp, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(self._bm25_cache_path)
            # Remember our own write so the staleness check doesn't mistake it
            # for an out-of-band one and reload needlessly.
            try:
                self._bm25_cache_mtime = self._bm25_cache_path.stat().st_mtime
            except OSError:
                pass
        except Exception:
            # Cache is purely a perf hint; never fail writes because of it
            pass

    def _load_bm25_cache(self) -> bool:
        p = self._bm25_cache_path
        if not p.exists():
            return False
        try:
            with open(p, "rb") as f:
                payload = pickle.load(f)
            if payload.get("version") != _BM25_CACHE_VERSION:
                return False
            self._bm25_ulids = list(payload["ulids"])
            self._bm25_tokens = list(payload["tokens"])
            # `fitted` is optional - if absent or unpickling fails for some
            # reason, _ensure_bm25() rebuilds from tokens at first search.
            fitted = payload.get("fitted")
            if isinstance(fitted, BM25Okapi):
                self._bm25 = fitted
            else:
                self._bm25 = None
            try:
                self._bm25_cache_mtime = p.stat().st_mtime
            except OSError:
                pass
            return True
        except Exception:
            return False

    def reload_bm25(self) -> None:
        """
        Restore BM25 state. Tries the on-disk pickle first (cheap), falls back
        to a full LanceDB scan if the cache is missing or corrupt.

        Idempotent: subsequent calls after the first successful one are no-ops,
        so search() / add() can call this defensively.

        MCP cold-start fix: a fresh workspace has NO BM25 pickle AND NO
        LanceDB table. The LanceDB-scan fallback used to trigger
        `import lancedb` (22s on Windows) just to discover there's nothing
        to load. We now check the SQLite events count first - if zero,
        skip the LanceDB step entirely.
        """
        self._bm25_reloaded = True
        if self._load_bm25_cache():
            return
        # Cheap check: peek at SQLite to avoid triggering 22s `import lancedb`
        # for an empty workspace (the common cold-start case in MCP).
        if self._workspace_has_no_events():
            self._bm25_ulids = []
            self._bm25_tokens = []
            self._bm25 = None
            return
        try:
            tbl = self._table.to_arrow()
            ulids = tbl.column("ulid").to_pylist() if "ulid" in tbl.column_names else []
            texts = tbl.column("text").to_pylist() if "text" in tbl.column_names else []
        except Exception:
            ulids, texts = [], []
        self._bm25_ulids = list(ulids)
        self._bm25_tokens = [tokenize(t) for t in texts]
        self._bm25 = None
        # Seed the cache so the next process avoids the LanceDB scan
        if self._bm25_ulids:
            self._save_bm25_cache()

    def _refresh_if_stale(self) -> bool:
        """Pick up out-of-band writes. If another process appended to the
        on-disk BM25 cache since we last loaded/saved it (a CLI `pmb fact` /
        `index` / `track` while this engine - typically the warm daemon - is
        live), reload BM25 and drop the cached LanceDB handle so the next
        vector query re-opens the table. Without this a long-running daemon
        serves a stale index for externally-added memory until it restarts.
        Cheap: one stat() per search; the reload only fires when the file
        actually changed. Returns True when it detected a change and reloaded,
        so the caller can also invalidate its recall cache."""
        try:
            m = self._bm25_cache_path.stat().st_mtime
        except OSError:
            return False
        if m > self._bm25_cache_mtime:
            self.reload_bm25()
            # Force LanceDB to re-open on next access so new vectors are visible.
            self._table_obj = None
            return True
        return False

    def _workspace_has_no_events(self) -> bool:
        """Cheap SQLite check to skip the 22s LanceDB import on cold-start
        for fresh workspaces. Returns True only when we can confirm the
        events table is empty; conservative on any error (returns False so
        the existing fallback runs)."""
        try:
            import sqlite3 as _sql

            # Vector path is on a separate path; we look for the events DB
            # by walking up from vector_path
            from pathlib import Path
            vp = Path(self.vector_path)
            # vector_path looks like <ws_dir>/vector ; events.sqlite is sibling
            db_path = vp.parent / "events.sqlite"
            if not db_path.exists():
                return True  # nothing at all → trivially empty
            with _sql.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()
            return int(row[0] or 0) == 0
        except Exception:
            return False

    def _ensure_bm25(self):
        if self._bm25 is None and self._bm25_tokens:
            # rank_bm25 divides by zero in _calc_idf when the corpus has NO
            # terms at all - every doc tokenized to empty, or a transient empty
            # index during a fresh-workspace recall before the embed/index
            # queue drained. Require at least one non-empty doc, and fall back
            # to no-BM25 (vector search still answers) on any error, instead of
            # crashing recall.
            if not any(self._bm25_tokens):
                return
            try:
                self._bm25 = BM25Okapi(self._bm25_tokens)
            except (ZeroDivisionError, ValueError):
                self._bm25 = None
                return
            # Persist the fitted index so the next process skips this rebuild
            self._save_bm25_cache()

    def size(self) -> int:
        return len(self._bm25_ulids)

    def _bm25_vocab(self) -> set[str]:
        """Cached set of all BM25 corpus tokens, for cross-lingual / OOV
        detection. Rebuilt only when the token-list length changes (cheap)."""
        n = len(self._bm25_tokens)
        if getattr(self, "_vocab_cache_n", -1) != n:
            vocab: set[str] = set()
            for toks in self._bm25_tokens:
                vocab.update(toks)
            self._vocab_cache = vocab
            self._vocab_cache_n = n
        return self._vocab_cache

    def search(
        self,
        query: str,
        top_k: int = 10,
        importance_map: dict[str, float] | None = None,
        timestamp_map: dict[str, float] | None = None,
        recency_half_life_days: float = 30.0,
        crosslingual_damp: float = 1.0,
    ) -> list[SearchHit]:
        """
        Hybrid search.

        importance_map: ulid -> importance [0..1]; multiplies score
        timestamp_map: ulid -> timestamp (seconds); used for recency boost
        recency_half_life_days: number of days over which the recency boost halves

        Improvement DD: when the embedding model is not yet loaded (cold start),
        skip vector search entirely and return BM25-only results. This keeps
        the FIRST recall after a process restart fast (~100ms) instead of
        blocking 30-120s on model load. Result quality is lower without
        vector similarity but the call doesn't timeout.
        """
        # Lazy BM25 reload - first search triggers it (cheap if pickle exists,
        # otherwise scans LanceDB once). Subsequent searches skip via the flag,
        # but still check whether another process wrote since (out-of-band CLI
        # writes vs a warm daemon) and refresh if so.
        if not self._bm25_reloaded:
            self.reload_bm25()
        else:
            self._refresh_if_stale()

        if self.size() == 0:
            return []

        # Improvement DD: if model not loaded yet, skip vector search
        # and use BM25-only to avoid blocking the recall on cold-start.
        if not self.is_ready():
            return self._search_bm25_only(
                query=query, top_k=top_k,
                importance_map=importance_map,
                timestamp_map=timestamp_map,
                recency_half_life_days=recency_half_life_days,
            )

        # Embed query
        q_emb = self.embed(query)

        # Vector search (LanceDB)
        try:
            vec_results = (
                self._table.search(q_emb.tolist())
                .limit(min(top_k * 5, self.size()))
                .to_list()
            )
        except Exception:
            vec_results = []

        vec_scores: dict[str, float] = {}
        for r in vec_results:
            # LanceDB returns _distance (L2 by default) - convert to similarity
            dist = r.get("_distance", 0.0)
            ulid = r.get("ulid", "")
            if ulid:
                # cosine similarity ≈ 1 - L2_normalized_dist^2 / 2
                # for normalized vectors; LanceDB defaults to L2
                vec_scores[ulid] = 1.0 / (1.0 + dist)

        # BM25
        self._ensure_bm25()
        bm25_scores: dict[str, float] = {}
        if self._bm25 is not None:
            q_tokens = tokenize(query)
            scores = self._bm25.get_scores(q_tokens)
            for ulid, score in zip(self._bm25_ulids, scores):
                bm25_scores[ulid] = float(score)

        # Combine
        all_ulids = set(vec_scores.keys()) | set(bm25_scores.keys())
        if not all_ulids:
            return []

        ulid_list = list(all_ulids)
        bm25_arr = np.array([bm25_scores.get(u, 0.0) for u in ulid_list], dtype=np.float32)
        vec_arr = np.array([vec_scores.get(u, 0.0) for u in ulid_list], dtype=np.float32)

        norm_bm25 = normalize(bm25_arr)
        norm_vec = normalize(vec_arr)

        # Cross-lingual / out-of-vocabulary damping: when the query's CONTENT
        # tokens are ENTIRELY absent from the BM25 corpus vocabulary (an English
        # question over a foreign-language fact, or a Russian query over an
        # English corpus), the lexical channel can only fire on coincidental
        # function-word overlap with WRONG-language distractors - min-max then
        # lifts a distractor to 1.0 and it beats the true cross-lingual vector
        # match. In that case lean on the vector channel. By construction this
        # NEVER touches in-language queries (their content words are in vocab),
        # so it cannot regress in-language ranking. Gated: 1.0 (default) = no-op.
        eff_bm25_w, eff_vec_w = self.bm25_weight, self.vec_weight
        if crosslingual_damp < 1.0:
            from pmb.core.text_match import distinctive_tokens
            q_content = distinctive_tokens(query)
            if q_content and not (q_content & self._bm25_vocab()):
                eff_bm25_w = self.bm25_weight * float(crosslingual_damp)
                eff_vec_w = 1.0 - eff_bm25_w

        hybrid = eff_bm25_w * norm_bm25 + eff_vec_w * norm_vec

        # Importance multiplier
        importance_arr = np.array(
            [importance_map.get(u, 0.5) if importance_map else 0.5 for u in ulid_list],
            dtype=np.float32,
        )
        # Apply as multiplier: importance 1.0 = full, 0.0 = halved
        importance_factor = 0.5 + 0.5 * importance_arr
        hybrid *= importance_factor

        # Recency boost
        recency_arr = self._compute_recency(ulid_list, timestamp_map, recency_half_life_days)
        hybrid = hybrid * (1.0 + 0.2 * recency_arr)

        order = np.argsort(hybrid)[::-1][:top_k]
        return [
            SearchHit(
                ulid=ulid_list[i],
                score=float(hybrid[i]),
                bm25_score=float(norm_bm25[i]),
                vec_score=float(norm_vec[i]),
                importance=float(importance_arr[i]),
                recency_score=float(recency_arr[i]),
                raw_vec=float(vec_scores.get(ulid_list[i], 0.0)),  # R3 abs cosine
            )
            for i in order
        ]

    def _compute_recency(
        self,
        ulid_list: list[str],
        timestamp_map: dict[str, float] | None,
        recency_half_life_days: float,
    ) -> np.ndarray:
        recency_arr = np.zeros(len(ulid_list), dtype=np.float32)
        if not timestamp_map or not ulid_list:
            return recency_arr
        now = time.time()
        half_life_sec = recency_half_life_days * 86400.0
        ts_vals = np.array(
            [timestamp_map.get(u, float("nan")) for u in ulid_list], dtype=np.float64,
        )
        mask = ~np.isnan(ts_vals)
        if mask.any():
            age_sec = np.maximum(0.0, now - ts_vals[mask])
            recency_arr[mask] = np.exp(
                -age_sec * np.log(2) / half_life_sec
            ).astype(np.float32)
        return recency_arr

    def _search_bm25_only(
        self,
        query: str,
        top_k: int,
        importance_map: dict[str, float] | None = None,
        timestamp_map: dict[str, float] | None = None,
        recency_half_life_days: float = 30.0,
    ) -> list[SearchHit]:
        """Improvement DD: BM25-only fallback for cold-start recall.
        Returns instantly without waiting for the embedding model to load.
        Result quality is lower than hybrid (no semantic similarity) but the
        user gets text-match results immediately.
        """
        self._ensure_bm25()
        if self._bm25 is None:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)
        if len(scores) == 0:
            return []
        ulid_list = self._bm25_ulids
        bm25_arr = np.asarray(scores, dtype=np.float32)
        max_b = float(bm25_arr.max()) if bm25_arr.size else 0.0
        norm = bm25_arr / (max_b + 1e-9) if max_b > 0 else bm25_arr

        # Importance boost
        importance_arr = np.array(
            [float((importance_map or {}).get(u, 0.5)) for u in ulid_list],
            dtype=np.float32,
        )
        importance_factor = 0.5 + 0.5 * importance_arr
        hybrid = norm * importance_factor

        # Recency boost
        recency_arr = self._compute_recency(ulid_list, timestamp_map, recency_half_life_days)
        hybrid = hybrid * (1.0 + 0.2 * recency_arr)

        order = np.argsort(hybrid)[::-1][:top_k]
        return [
            SearchHit(
                ulid=ulid_list[i],
                score=float(hybrid[i]),
                bm25_score=float(norm[i]),
                vec_score=0.0,
                importance=float(importance_arr[i]),
                recency_score=float(recency_arr[i]),
            )
            for i in order
        ]
