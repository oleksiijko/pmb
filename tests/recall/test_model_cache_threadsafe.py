"""Global model caches must load each heavy model only once under concurrency."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pmb.core import search


def _exercise_cache(monkeypatch, cache, loader_name):
    created: list[object] = []
    gate = threading.Barrier(8)

    class _FakeModel:
        pass

    def _loader():
        def _build(_name):
            time.sleep(0.03)
            model = _FakeModel()
            created.append(model)
            return model
        return _build

    monkeypatch.setattr(search, loader_name, _loader)
    cache._model = None
    cache._name = None
    if hasattr(cache, "_backend"):
        cache._backend = None
        cache._base_url = None

    def _get():
        gate.wait()
        return cache.get("one-model")

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            models = list(pool.map(lambda _: _get(), range(8)))
        assert len(created) == 1
        assert all(model is models[0] for model in models)
    finally:
        cache._model = None
        cache._name = None
        if hasattr(cache, "_backend"):
            cache._backend = None
            cache._base_url = None


def test_embedding_model_cache_loads_once(monkeypatch):
    _exercise_cache(monkeypatch, search._ModelCache, "_SentenceTransformer")


def test_cross_encoder_cache_loads_once(monkeypatch):
    _exercise_cache(monkeypatch, search._CrossEncoderCache, "_CrossEncoder")


def test_shared_model_inference_is_serialized_across_workspaces(tmp_path):
    import numpy as np

    active = threading.Lock()
    gate = threading.Barrier(8)

    class Model:
        def encode(self, texts, **kwargs):
            assert active.acquire(blocking=False), "Concurrent use of shared native model"
            try:
                time.sleep(0.01)
                return np.ones((len(texts), 384), dtype=np.float32)
            finally:
                active.release()

    model = Model()
    indexes = [search.HybridSearch(tmp_path / str(i)) for i in range(8)]
    for index in indexes:
        index._model = model

    def embed(i):
        gate.wait(timeout=5)
        return indexes[i].embed("query") if i % 2 else indexes[i].embed_batch(["a", "b"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(embed, range(8)))
    assert [result.shape for result in results] == [(2, 384), (384,)] * 4
