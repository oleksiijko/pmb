"""Codex 120s-timeout regression (June 11 2026).

Root cause: a `record_batch` in a COLD stdio MCP server acked fast (outbox),
but the background embed-queue worker then EAGERLY triggered the full torch
model load (`_ = self.search.model`). Under memory pressure (a second ~400MB
model next to the warm daemon → paging) the load's long GIL bursts starved the
server's asyncio loop, so Codex's NEXT `tools/call` was never read before its
120s deadline → "timed out awaiting tools/call after 120s".

The fix: the worker is PASSIVE (waits for `is_ready()`, never loads), writes
stay durable without the model (SQLite row + embed_queue_pending), warmup()
kicks the drain once the model is legitimately ready, and the Codex entry gets
`tool_timeout_sec=300` headroom for genuinely slow cold first calls.
"""
from __future__ import annotations

import sqlite3
import time

from pmb.core.engine import Engine
from pmb.core.search import HybridSearch


def _items(n: int = 3) -> list[dict]:
    return [{"type": "fact", "content": f"cold-path fact number {i} marker-{i}"}
            for i in range(n)]


def test_cold_record_batch_never_touches_the_model(tmp_pmb_home, tmp_workspace_dir,
                                                   monkeypatch):
    touched: dict = {}

    # Other engines may still be draining writes from earlier tests. Scope the
    # trap to this engine's search class so their legitimate loads cannot count
    # as a violation of the cold-write contract.
    unrelated = HybridSearch(tmp_workspace_dir / "unrelated-vectors")
    unrelated._model = object()

    class ColdSearch(HybridSearch):
        @property
        def model(self):
            touched["model"] = True
            raise AssertionError("model load triggered on the cold write path")

        def attach_cached_model(self) -> bool:
            return False

    monkeypatch.setattr("pmb.core.engine.base.HybridSearch", ColdSearch)
    assert unrelated.model is unrelated._model
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home,
                 config_overrides={"recall.cache_size": 0})

    t0 = time.perf_counter()
    out = eng.record_batch_async(items=_items())
    ack_ms = (time.perf_counter() - t0) * 1000.0
    assert out.get("ok") is True
    assert ack_ms < 5000, f"ack took {ack_ms:.0f}ms — must be near-instant"

    eng.wait_for_writes(timeout=60)
    time.sleep(1.0)   # give the (passive) worker a beat — it must stay idle
    assert "model" not in touched, "cold write path loaded the embedding model"

    with sqlite3.connect(str(eng.workspace.db_path)) as c:
        n_rows = c.execute(
            "SELECT COUNT(*) FROM events WHERE workspace_id=? AND event_type='fact'",
            (eng.workspace.id,)).fetchone()[0]
        n_queued = c.execute(
            "SELECT COUNT(*) FROM embed_queue_pending").fetchone()[0]
    assert n_rows >= 3, "facts must land in SQLite without the model"
    assert n_queued >= 3, "embeds must be deferred to the durable queue"


def test_eager_autoload_opt_in_restores_old_behavior(tmp_pmb_home, tmp_workspace_dir,
                                                     monkeypatch):
    touched: dict = {}

    class EagerSearch(HybridSearch):
        @property
        def model(self):
            touched["model"] = True
            raise RuntimeError("stop before the real (slow) load")

    monkeypatch.setattr("pmb.core.engine.base.HybridSearch", EagerSearch)
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home,
                 config_overrides={"recall.cache_size": 0,
                                   "embed.queue_autoload": True})
    eng._enqueue_embed("u-eager", "some text")   # spawns the worker
    deadline = time.time() + 5.0
    while "model" not in touched and time.time() < deadline:
        time.sleep(0.05)
    assert touched.get("model"), "autoload=true must keep the eager load"


def test_warmup_kick_drains_leftover_queue(tmp_pmb_home, tmp_workspace_dir,
                                           monkeypatch):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home,
                 config_overrides={"recall.cache_size": 0})
    called: dict = {}
    monkeypatch.setattr(eng, "_drain_embed_queue",
                        lambda: called.setdefault("drained", True))
    eng._embed_queue = [("u1", "t1")]
    eng._embed_worker_started = False
    eng._kick_embed_drain()
    deadline = time.time() + 3.0
    while "drained" not in called and time.time() < deadline:
        time.sleep(0.05)
    assert called.get("drained"), "warmup kick must spawn the drain worker"


def test_kick_is_noop_on_empty_queue(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home,
                 config_overrides={"recall.cache_size": 0})
    eng._kick_embed_drain()   # must not raise, must not flip the started flag
    assert eng._embed_worker_started is False


def test_codex_entry_gets_tool_timeout_headroom():
    from pmb.cli.connect import merge_codex_entry
    cfg, _ = merge_codex_entry({}, "pmb", {"command": "pmb-mcp", "env": {}})
    e = cfg["mcp_servers"]["pmb"]
    assert e["startup_timeout_sec"] == 120
    assert e["tool_timeout_sec"] == 300
