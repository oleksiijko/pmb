"""Tests for LLM-based consolidation. Uses MockLLM — no API calls."""
from __future__ import annotations

from pmb.core.engine import Engine
from pmb.health.consolidate import (
    _parse_llm_json,
    cluster_events,
)


class MockLLM:
    """Deterministic stand-in for the Anthropic client."""

    def __init__(self, *, consolidate: bool = True, summary: str = "Generalized rule",
                 confidence: float = 0.9, reasoning: str = "they all say the same thing",
                 raise_error: bool = False):
        self._consolidate = consolidate
        self._summary = summary
        self._confidence = confidence
        self._reasoning = reasoning
        self._raise = raise_error
        self.calls: list[list[str]] = []

    def consolidate(self, events_text: list[str]) -> dict:
        self.calls.append(list(events_text))
        if self._raise:
            raise RuntimeError("simulated LLM failure")
        return {
            "consolidate": self._consolidate,
            "summary": self._summary,
            "confidence": self._confidence,
            "reasoning": self._reasoning,
        }


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


def test_parse_clean_json():
    out = _parse_llm_json('{"consolidate": true, "summary": "x", "confidence": 0.9, "reasoning": "y"}')
    assert out["consolidate"] is True
    assert out["summary"] == "x"
    assert out["confidence"] == 0.9


def test_parse_fenced_json():
    out = _parse_llm_json('```json\n{"consolidate": false, "summary": "", "confidence": 0.1, "reasoning": "no"}\n```')
    assert out["consolidate"] is False
    assert out["confidence"] == 0.1


def test_parse_with_prose():
    out = _parse_llm_json('Sure! Here is my answer:\n{"consolidate": true, "summary": "rule", "confidence": 0.7, "reasoning": "obvious"}')
    assert out["summary"] == "rule"


def test_parse_garbage_falls_back():
    out = _parse_llm_json("this is not json at all")
    assert out["consolidate"] is False
    assert out["confidence"] == 0.0


# ----------------------------------------------------------------------
# Clustering
# ----------------------------------------------------------------------


def test_cluster_empty_workspace_returns_nothing(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    assert cluster_events(eng) == []


def test_cluster_groups_similar_events(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    # Four very similar fact statements + one unrelated
    eng.record_fact("user prefers no comments in code")
    eng.record_fact("don't add docstrings, user thinks they're noise")
    eng.record_fact("strip comments before commit per user preference")
    eng.record_fact("comments are not welcome in this repo")
    eng.record_fact("unrelated: deploy with kubectl apply -f")

    clusters = cluster_events(eng, similarity_threshold=0.3, min_cluster_size=3)
    assert len(clusters) >= 1
    # The first cluster should be the "no comments" cluster, size >= 3
    top = max(clusters, key=lambda c: len(c))
    assert len(top) >= 3


def test_cluster_respects_min_size(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    # Only 2 similar — below min_cluster_size=3
    eng.record_fact("user uses postgres")
    eng.record_fact("postgres on port 5432")
    assert cluster_events(eng, min_cluster_size=3) == []


def test_cluster_respects_threshold(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    eng.record_fact("user prefers no comments")
    eng.record_fact("deploy via terraform")
    eng.record_fact("postgres on port 5432")
    # Very high threshold — none of these are highly similar
    assert cluster_events(eng, similarity_threshold=0.99, min_cluster_size=2) == []


# ----------------------------------------------------------------------
# Run consolidation
# ----------------------------------------------------------------------


def test_consolidation_stores_fact_and_archives_sources(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    ulids = [
        eng.record_fact("user prefers no comments in code"),
        eng.record_fact("don't add docstrings, user dislikes them"),
        eng.record_fact("strip comments before commit"),
    ]
    llm = MockLLM(consolidate=True, summary="User prefers code without comments.", confidence=0.9)

    result = eng.consolidate(llm=llm, similarity_threshold=0.3, min_cluster_size=3)

    assert result["n_clusters_found"] >= 1
    assert result["n_consolidated"] >= 1
    # The new fact exists
    rs = result["results"]
    stored = [r for r in rs if r["consolidated"]]
    assert stored
    new_ulid = stored[0]["new_ulid"]
    new_ev = eng.events.get_by_ulid(new_ulid)
    assert new_ev is not None
    assert "comments" in new_ev.content.lower()
    assert new_ev.importance == 0.85
    assert new_ev.metadata["consolidated_from"]
    # Sources archived
    archived = stored[0]["archived_source_ulids"]
    assert len(archived) >= 3
    for u in archived:
        ev = eng.events.get_by_ulid(u)
        assert ev.archived_at is not None


def test_consolidation_dry_run_does_not_write(tmp_pmb_home, tmp_workspace_dir, monkeypatch):
    import numpy as np

    from pmb.core.events import Event

    # This test checks write isolation, not the embedder's floating-point
    # similarity around a threshold. Semantic clustering is tested separately.
    with Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home) as eng:
        monkeypatch.setattr(eng.search, "embed", lambda text: np.ones(384, dtype=np.float32))
        for content in ("user prefers no comments", "avoid docstrings", "strip comments"):
            event = eng.events.append(Event(
                workspace_id=eng.workspace.id, event_type="fact", content=content,
            ))
            eng.search.add(event.ulid, event.to_text())
        llm = MockLLM(summary="Keep source files concise.", confidence=0.9)
        before = eng.events.list_active(eng.workspace.id)
        result = eng.consolidate(llm=llm, similarity_threshold=0.3,
                                 min_cluster_size=3, dry_run=True)
        assert eng.events.list_active(eng.workspace.id) == before
        assert eng.events.count(eng.workspace.id) == 3
        assert result["n_consolidated"] == 1
        assert result["n_archived"] == 0
        assert result["new_facts_created"] == 0
        assert len(llm.calls) == 1


def test_consolidation_skips_low_confidence(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    eng.record_fact("user prefers no comments in code")
    eng.record_fact("don't add docstrings")
    eng.record_fact("strip comments before commit")
    llm = MockLLM(consolidate=True, summary="Maybe?", confidence=0.3)

    result = eng.consolidate(llm=llm, similarity_threshold=0.3, min_cluster_size=3)
    assert result["n_consolidated"] == 0
    # Sources NOT archived when not consolidated
    assert result["n_archived"] == 0


def test_consolidation_skips_when_llm_says_no(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    eng.record_fact("user prefers no comments")
    eng.record_fact("don't add docstrings")
    eng.record_fact("strip comments")
    llm = MockLLM(consolidate=False, summary="", confidence=0.95)

    result = eng.consolidate(llm=llm, similarity_threshold=0.3, min_cluster_size=3)
    assert result["n_consolidated"] == 0
    assert result["n_archived"] == 0


def test_consolidation_handles_llm_error(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    eng.record_fact("a")
    eng.record_fact("a")
    eng.record_fact("a")
    llm = MockLLM(raise_error=True)
    result = eng.consolidate(llm=llm, similarity_threshold=0.4, min_cluster_size=3)
    # Result records the failure but doesn't crash
    assert result["n_consolidated"] == 0
    if result["results"]:
        assert "llm error" in result["results"][0]["reasoning"]


def test_ollama_ping_failure_returns_false(monkeypatch):
    """Ping should not raise on connection failure — used in auto-detection."""
    from pmb.health.consolidate import OllamaClient
    # Point at a port nothing's on
    assert OllamaClient.ping(base_url="http://127.0.0.1:1", timeout=0.5) is False


def test_ollama_consolidate_via_mocked_urlopen(monkeypatch):
    """Drive OllamaClient.consolidate with a mocked HTTP layer — no network."""
    import json as _json
    import urllib.request

    from pmb.health.consolidate import OllamaClient

    expected = {
        "response": _json.dumps({
            "consolidate": True,
            "summary": "User prefers no comments.",
            "confidence": 0.9,
            "reasoning": "they all say the same",
        })
    }

    class _Resp:
        status = 200
        def read(self_inner):
            return _json.dumps(expected).encode("utf-8")
        def __enter__(self_inner): return self_inner
        def __exit__(self_inner, *a): return False

    def _fake_urlopen(req, timeout=None):
        # Sanity: we hit /api/generate with a POST
        assert req.full_url.endswith("/api/generate")
        assert req.method == "POST"
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = OllamaClient(model="llama3.1:8b")
    out = client.consolidate(["one", "two", "three"])
    assert out["consolidate"] is True
    assert "comments" in out["summary"].lower()
    assert out["confidence"] == 0.9


def test_openai_consolidate_via_mocked_urlopen(monkeypatch):
    """Drive OpenAIClient.consolidate with mocked HTTP - no network."""
    import json as _json
    import urllib.request

    from pmb.health.consolidate import OpenAIClient

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("PMB_OPENAI_MODEL", raising=False)
    captured = {}

    class _Resp:
        def read(self_inner):
            return _json.dumps({
                "choices": [{"message": {"content": _json.dumps({
                    "consolidate": True,
                    "summary": "User prefers no comments.",
                    "confidence": 0.9,
                    "reasoning": "they all say the same",
                })}}]
            }).encode("utf-8")
        def __enter__(self_inner): return self_inner
        def __exit__(self_inner, *a): return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = _json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.get_header("Authorization")
        assert req.method == "POST"
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = OpenAIClient().consolidate(["one", "two", "three"])

    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in captured["body"]["messages"]] == ["system", "user"]
    assert out["consolidate"] is True
    assert out["confidence"] == 0.9


def test_ollama_unreachable_raises_runtimeerror(monkeypatch):
    import urllib.error
    import urllib.request

    from pmb.health.consolidate import OllamaClient

    def _fail(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _fail)
    client = OllamaClient()
    try:
        client.consolidate(["a"])
    except RuntimeError as e:
        assert "Ollama unreachable" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_resolve_auto_falls_back_to_ollama(monkeypatch):
    """No claude on PATH, no API key, ollama up → ollama."""
    from pmb.health import consolidate as C
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(C.ClaudeCLIClient, "available", staticmethod(lambda *a, **kw: False))
    monkeypatch.setattr(C.OllamaClient, "ping", staticmethod(lambda *a, **kw: True))
    client = C.resolve_llm_client(backend="auto")
    assert isinstance(client, C.OllamaClient)


def test_resolve_auto_falls_through_to_openai(monkeypatch):
    """No claude/no Anthropic but OpenAI key set → openai."""
    from pmb.health import consolidate as C
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(C.ClaudeCLIClient, "available", staticmethod(lambda *a, **kw: False))
    monkeypatch.setattr(C.OllamaClient, "ping", staticmethod(lambda *a, **kw: False))
    client = C.resolve_llm_client(backend="auto")
    assert isinstance(client, C.OpenAIClient)


def test_resolve_auto_raises_when_nothing_available(monkeypatch):
    from pmb.health import consolidate as C
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(C.ClaudeCLIClient, "available", staticmethod(lambda *a, **kw: False))
    monkeypatch.setattr(C.OllamaClient, "ping", staticmethod(lambda *a, **kw: False))
    try:
        C.resolve_llm_client(backend="auto")
    except RuntimeError as e:
        msg = str(e)
        assert "ANTHROPIC_API_KEY" in msg
        assert "OPENAI_API_KEY" in msg
        assert "ollama" in msg.lower()
        assert "claude" in msg.lower()
    else:
        raise AssertionError("expected RuntimeError")


def test_resolve_explicit_ollama_when_down(monkeypatch):
    from pmb.health import consolidate as C
    monkeypatch.setattr(C.OllamaClient, "ping", staticmethod(lambda *a, **kw: False))
    try:
        C.resolve_llm_client(backend="ollama")
    except RuntimeError as e:
        assert "not reachable" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_resolve_explicit_openai_without_key(monkeypatch):
    from pmb.health import consolidate as C
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        C.resolve_llm_client(backend="openai")
    except RuntimeError as e:
        assert "OPENAI_API_KEY" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_claude_cli_available_check(monkeypatch):
    import shutil

    from pmb.health.consolidate import ClaudeCLIClient
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/path/claude" if cmd == "claude" else None)
    assert ClaudeCLIClient.available() is True
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    assert ClaudeCLIClient.available() is False


def test_claude_cli_consolidate_via_mocked_subprocess(monkeypatch):
    """Mock subprocess so we never spawn a real claude process."""
    import subprocess as sp

    from pmb.health.consolidate import ClaudeCLIClient

    expected = '{"consolidate": true, "summary": "User prefers no comments.", "confidence": 0.9, "reasoning": "all three say so"}'

    class _R:
        returncode = 0
        stdout = expected
        stderr = ""

    captured = {}
    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs.get("timeout")
        return _R()

    monkeypatch.setattr(sp, "run", _fake_run)
    client = ClaudeCLIClient(command="claude", model="haiku", timeout=60)
    out = client.consolidate(["msg1", "msg2", "msg3"])
    assert out["consolidate"] is True
    assert "comments" in out["summary"].lower()
    # Sanity: argv shape
    assert captured["argv"][0] == "claude"
    assert "-p" in captured["argv"]
    assert "--model" in captured["argv"]
    assert "haiku" in captured["argv"]
    assert "--no-session-persistence" in captured["argv"]


def test_claude_cli_propagates_exit_code(monkeypatch):
    import subprocess as sp

    from pmb.health.consolidate import ClaudeCLIClient
    class _R:
        returncode = 1
        stdout = ""
        stderr = "Some error happened on stderr"
    monkeypatch.setattr(sp, "run", lambda *a, **kw: _R())
    client = ClaudeCLIClient()
    try:
        client.consolidate(["x"])
    except RuntimeError as e:
        assert "exited 1" in str(e)
        assert "Some error" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_claude_cli_handles_missing_binary(monkeypatch):
    import subprocess as sp

    from pmb.health.consolidate import ClaudeCLIClient
    def _fail(*a, **kw):
        raise FileNotFoundError("claude not found")
    monkeypatch.setattr(sp, "run", _fail)
    try:
        ClaudeCLIClient().consolidate(["x"])
    except RuntimeError as e:
        assert "PATH" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_claude_cli_handles_timeout(monkeypatch):
    import subprocess as sp

    from pmb.health.consolidate import ClaudeCLIClient
    def _timeout(*a, **kw):
        raise sp.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(sp, "run", _timeout)
    try:
        ClaudeCLIClient(timeout=1).consolidate(["x"])
    except RuntimeError as e:
        assert "timed out" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_resolve_auto_prefers_claude_cli_over_anthropic(monkeypatch):
    """When `claude` is on PATH, auto picks it even if API key is set."""
    from pmb.health import consolidate as C
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(C.ClaudeCLIClient, "available", staticmethod(lambda *a, **kw: True))
    client = C.resolve_llm_client(backend="auto")
    assert isinstance(client, C.ClaudeCLIClient)


def test_resolve_auto_falls_through_claude_to_anthropic(monkeypatch):
    """No claude in PATH but API key set → anthropic."""
    from pmb.health import consolidate as C
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(C.ClaudeCLIClient, "available", staticmethod(lambda *a, **kw: False))
    monkeypatch.setattr(C.AnthropicHaikuClient, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(C.OllamaClient, "ping", staticmethod(lambda *a, **kw: False))
    client = C.resolve_llm_client(backend="auto")
    assert isinstance(client, C.AnthropicHaikuClient)


def test_resolve_explicit_claude_when_missing(monkeypatch):
    from pmb.health import consolidate as C
    monkeypatch.setattr(C.ClaudeCLIClient, "available", staticmethod(lambda *a, **kw: False))
    try:
        C.resolve_llm_client(backend="claude")
    except RuntimeError as e:
        assert "not in PATH" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_resolve_unknown_backend_raises():
    from pmb.health import consolidate as C
    try:
        C.resolve_llm_client(backend="gpt5")
    except ValueError as e:
        assert "unknown backend" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_consolidation_preserves_pinned_sources(tmp_pmb_home, tmp_workspace_dir):
    eng = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home)
    pinned = eng.record_fact("user prefers no comments")
    eng.pin(pinned)
    eng.record_fact("don't add docstrings")
    eng.record_fact("strip comments")
    llm = MockLLM(consolidate=True, summary="No comments preferred.", confidence=0.9)

    eng.consolidate(llm=llm, similarity_threshold=0.3, min_cluster_size=3)
    # Pinned event should remain active
    p = eng.events.get_by_ulid(pinned)
    assert p.archived_at is None
    assert p.importance >= 0.99


def test_malformed_llm_fields_cannot_authorize_archiving():
    import json

    for patch in ({"consolidate": "false"}, {"confidence": "high"}, {"confidence": True},
                  {"confidence": float("nan")}, {"confidence": 2}, {"summary": None},
                  {"summary": "   "}):
        payload = {"consolidate": True, "summary": "A rule", "confidence": 0.9, **patch}
        assert _parse_llm_json(json.dumps(payload))["consolidate"] is False
    for raw in ("[]", "null", "1"):
        assert _parse_llm_json(raw)["consolidate"] is False


def test_consolidation_waits_for_last_inflight_embedding(isolated_engine, monkeypatch):
    import threading
    import time

    from pmb.core.embed_queue import PersistentEmbedQueue
    from pmb.health.consolidate import _drain_pending_embeds

    engine = isolated_engine
    queue = PersistentEmbedQueue(engine.workspace.db_path)
    queue.enqueue("last-item", "memory")
    engine._durable_embed_queue = queue
    engine._embed_worker_started = True
    monkeypatch.setattr(engine.search, "_model", object())
    entered = threading.Event()
    def finish():
        entered.set()
        time.sleep(0.05)
        queue.drain_once(lambda ulid, text: None)
    thread = threading.Thread(target=finish)
    thread.start()
    assert entered.wait(timeout=1)
    try:
        assert _drain_pending_embeds(engine, timeout_s=2) == 0
        assert queue.pending_count() == 0
    finally:
        thread.join(timeout=2)


def test_llm_failure_keeps_sources_and_returns_nonzero_cli(isolated_engine, monkeypatch):
    from typer.testing import CliRunner

    from pmb.cli.commands import maintenance
    from pmb.cli.main import app
    from pmb.core.events import Event
    from pmb.health.consolidate import Cluster

    engine = isolated_engine
    events = [engine.events.append(Event(
        workspace_id=engine.workspace.id, event_type="fact", content=f"Source {i}",
    )) for i in range(3)]
    cluster = Cluster(events[0].ulid, [event.ulid for event in events], 0.9)
    monkeypatch.setattr("pmb.health.consolidate.cluster_events", lambda *args, **kwargs: [cluster])
    monkeypatch.setattr("pmb.health.consolidate.resolve_llm_client",
                        lambda **kwargs: MockLLM(raise_error=True))
    engine.config.set_workspace("consolidate.suggest_keyed", False)
    monkeypatch.setattr(maintenance, "Engine", lambda: engine)
    result = CliRunner().invoke(app, ["consolidate"])
    assert result.exit_code == 1
    assert "1 cluster(s) failed" in result.stdout
    assert len(engine.events.list_active(engine.workspace.id)) == 3
    assert not (engine.workspace.storage_dir / "consolidation_state.yaml").exists()
