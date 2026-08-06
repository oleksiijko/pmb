"""B1/B2/B3: the persistent memory daemon.

- B2: the internal /internal/health + /internal/hook/prepare-context routes
  answer against the SAME warm engine (tested in-process via TestClient, no
  heavy model load needed — lesson surfacing is lexical).
- B1/B3: the registry knows a daemon (find_live_daemon) and the hook client
  (_try_daemon_prepare) talks to it, honoring the version handshake and
  degrading to None when absent / mismatched.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import pmb

# ── B2: internal routes answer against the warm engine (in-process) ─────────

def _build_daemon_app(cwd, seed_lesson: str | None = None):
    from starlette.testclient import TestClient

    from pmb.mcp.daemon import _register_internal_routes
    from pmb.mcp.server import build_server

    server = build_server(cwd=cwd, prewarm=False)  # no 20s model load in tests
    engine = server._pmb_engine
    if seed_lesson:
        engine.record_batch([{"type": "lesson", "content": seed_lesson}])
        engine.wait_for_writes(timeout=30)
    _register_internal_routes(server, engine)
    app = server.http_app(path="/mcp")
    return TestClient(app), engine


def test_internal_health_reports_version_and_workspace(tmp_pmb_home, tmp_workspace_dir):
    client, engine = _build_daemon_app(tmp_workspace_dir)
    with client:
        r = client.get("/internal/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["version"] == pmb.__version__
        assert body["workspace"] == engine.workspace.id


def test_internal_prepare_context_surfaces_a_lesson(tmp_pmb_home, tmp_workspace_dir):
    client, _ = _build_daemon_app(
        tmp_workspace_dir,
        seed_lesson="Always use pnpm in this repo, never npm.")
    with client:
        r = client.post("/internal/hook/prepare-context",
                        json={"message": "should I run npm or pnpm to install deps here?"})
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "daemon"
        assert body["version"] == pmb.__version__
        # lexical lesson surfacing needs no embedding model → deterministic
        assert "pnpm" in body["context"].lower()


def test_internal_prepare_context_empty_message(tmp_pmb_home, tmp_workspace_dir):
    client, _ = _build_daemon_app(tmp_workspace_dir)
    with client:
        r = client.post("/internal/hook/prepare-context", json={"message": ""})
        assert r.status_code == 200
        assert r.json()["context"] == ""


def test_internal_recall_returns_serialized_pack(tmp_pmb_home, tmp_workspace_dir):
    """The /internal/recall route lets `pmb recall` reuse the warm engine: it
    returns a serialized pack (results + the bm25/vector signal breakdown the
    CLI renders)."""
    client, engine = _build_daemon_app(tmp_workspace_dir)
    engine.record_batch([
        {"type": "fact", "content": "zz canonical deploy region is eu-central-7"},
    ])
    engine.wait_for_writes(timeout=30)
    with client:
        r = client.post("/internal/recall",
                        json={"query": "canonical deploy region", "top_k": 5})
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "daemon"
        assert body["version"] == pmb.__version__
        hit = [x for x in body.get("results", []) if "eu-central-7" in x["content"]]
        assert hit, "seeded fact should be recalled by the daemon"
        assert "signals" in hit[0] and "bm25" in hit[0]["signals"]


# ── B1: registry tracks a daemon; find_live_daemon matches kind+home ────────

def test_find_live_daemon_matches_kind(tmp_pmb_home):
    from pmb.mcp.registry import find_live_daemon, register_server, unregister_server
    # a plain mcp server must NOT be returned as a daemon
    register_server(transport="streamable-http", kind="mcp",
                    host="127.0.0.1", port=9111)
    assert find_live_daemon() is None
    # a daemon for THIS pmb_home is found
    entry = register_server(transport="streamable-http", kind="daemon",
                            host="127.0.0.1", port=9112)
    got = find_live_daemon()
    assert got is not None and got["port"] == 9112
    unregister_server(entry["pid"])


# ── B3: the hook client talks to a stub daemon + honors the version gate ────

class _StubHandler(BaseHTTPRequestHandler):
    version_to_send = pmb.__version__

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(n)
        body = json.dumps({
            "context": "STUB CONTEXT FROM DAEMON",
            "version": self.version_to_send,
            "source": "daemon",
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def stub_daemon(tmp_pmb_home):
    """Run a stub HTTP daemon + register it so find_live_daemon() resolves it."""
    from pmb.mcp.daemon import write_daemon_token
    from pmb.mcp.registry import register_server, unregister_server

    write_daemon_token()
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    entry = register_server(transport="streamable-http", kind="daemon",
                            host="127.0.0.1", port=port)
    try:
        yield srv
    finally:
        unregister_server(entry["pid"])
        srv.shutdown()


def test_hook_client_uses_daemon(stub_daemon, tmp_pmb_home):
    from pmb.cli.commands.ambient import _try_daemon_prepare
    _StubHandler.version_to_send = pmb.__version__
    out = _try_daemon_prepare("where do I live?", 4000, timeout=2.0)
    assert out == "STUB CONTEXT FROM DAEMON"


def test_hook_client_rejects_version_mismatch(stub_daemon, tmp_pmb_home):
    from pmb.cli.commands.ambient import _try_daemon_prepare
    _StubHandler.version_to_send = "0.0.0-other"
    out = _try_daemon_prepare("where do I live?", 4000, timeout=2.0)
    assert out is None  # mismatched version → treat as absent, go cold
    _StubHandler.version_to_send = pmb.__version__


def test_hook_client_none_when_no_daemon(tmp_pmb_home):
    from pmb.cli.commands.ambient import _try_daemon_prepare
    # no daemon registered → immediate None (cold path)
    assert _try_daemon_prepare("anything", 4000, timeout=0.3) is None


# ── S9: the daemon drains buffered perf rows when it stops serving ──────────

def test_lifespan_shutdown_drains_perf(monkeypatch):
    """The ASGI lifespan shutdown is the daemon's only usable drain hook.

    uvicorn captures SIGTERM/SIGINT, shuts down, restores the previous
    handlers and re-raises the signal from inside `Server.capture_signals`, so
    the process dies without `uvicorn.run` ever returning — a `finally` around
    it and `atexit` are both dead code on the path that actually stops a
    daemon. Lifespan shutdown does run, before that re-raise.
    """
    import asyncio
    from unittest.mock import MagicMock

    from pmb.mcp.daemon import _drain_on_lifespan_shutdown

    engine = MagicMock()
    flushed: list[bool] = []
    monkeypatch.setattr("pmb.mcp.perf.flush_perf",
                        lambda *a, **kw: flushed.append(True))

    async def inner_app(scope, receive, send):
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    wrapped = _drain_on_lifespan_shutdown(inner_app, engine)
    events = ["lifespan.startup", "lifespan.shutdown"]
    sent: list[dict] = []

    async def receive():
        return {"type": events.pop(0)}

    async def send(message):
        # the drain must have happened by the time shutdown is reported done
        if message["type"] == "lifespan.shutdown.complete":
            assert flushed, "shutdown reported complete before the perf flush"
        sent.append(message)

    asyncio.run(wrapped({"type": "lifespan"}, receive, send))

    assert [m["type"] for m in sent] == ["lifespan.startup.complete",
                                         "lifespan.shutdown.complete"]
    # exactly once: the wrapper also drains from a `finally`, which must see
    # the message path has already run
    assert engine.wait_for_writes.call_count == 1
    assert len(flushed) == 1


def test_lifespan_shutdown_drains_when_the_app_raises(monkeypatch):
    """A drain hung only off the shutdown message is not enough.

    If the wrapped app raises during its shutdown, no
    `lifespan.shutdown.complete` (or `.failed`) is ever sent — uvicorn catches
    the exception in `LifespanOn.main` and carries on to re-raise the signal.
    Without a `finally` the daemon would silently record nothing, which is the
    defect this wrapper exists to fix.
    """
    import asyncio
    from unittest.mock import MagicMock

    from pmb.mcp.daemon import _drain_on_lifespan_shutdown

    engine = MagicMock()
    flushed: list[bool] = []
    monkeypatch.setattr("pmb.mcp.perf.flush_perf",
                        lambda *a, **kw: flushed.append(True))

    async def inner_app(scope, receive, send):
        assert (await receive())["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        raise RuntimeError("shutdown blew up")

    wrapped = _drain_on_lifespan_shutdown(inner_app, engine)
    events = ["lifespan.startup", "lifespan.shutdown"]

    async def receive():
        return {"type": events.pop(0)}

    async def send(message):
        pass

    with pytest.raises(RuntimeError, match="shutdown blew up"):
        asyncio.run(wrapped({"type": "lifespan"}, receive, send))

    assert len(flushed) == 1
    assert engine.wait_for_writes.call_count == 1


def test_lifespan_wrapper_passes_other_scopes_through():
    """Only the lifespan scope is intercepted; requests are untouched."""
    import asyncio
    from unittest.mock import MagicMock

    from pmb.mcp.daemon import _drain_on_lifespan_shutdown

    seen: list[str] = []

    async def inner_app(scope, receive, send):
        seen.append(scope["type"])

    wrapped = _drain_on_lifespan_shutdown(inner_app, MagicMock())
    asyncio.run(wrapped({"type": "http"}, None, None))
    assert seen == ["http"]


class _StubApp:
    """Minimal ASGI app: records what it is asked to do, drains nothing."""

    def __init__(self):
        self.middleware: list = []
        self.scopes: list[str] = []

    def add_middleware(self, mw):
        self.middleware.append(mw)

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope["type"])


class _StubServer:
    def __init__(self, app, engine):
        self._app = app
        self._pmb_engine = engine
        self.name = "stub-ws"

    def http_app(self, path=None):
        return self._app

    def custom_route(self, *_a, **_kw):
        return lambda fn: fn


def _serve_and_capture(monkeypatch, tmp_pmb_home):
    """Run `run_daemon` with everything heavy stubbed; return what it served."""
    from unittest.mock import MagicMock

    from pmb.mcp.daemon import run_daemon

    engine = MagicMock()
    engine.config.get.side_effect = lambda key, *a: {
        "daemon.idle_exit_min": 0,     # no idle watcher thread in a test
        "daemon.maintenance": False,
    }.get(key, 0)
    engine.recover_outbox.return_value = 0
    app = _StubApp()

    monkeypatch.setattr("pmb.mcp.server.build_server",
                        lambda *a, **kw: _StubServer(app, engine))
    monkeypatch.setattr("pmb.mcp.registry.find_live_daemon", lambda: None)
    monkeypatch.setattr("pmb.mcp.registry.register_server",
                        lambda **kw: {"pid": 4242})
    monkeypatch.setattr("pmb.mcp.registry.unregister_server", lambda pid: None)

    served: dict = {}
    monkeypatch.setattr("uvicorn.run",
                        lambda app, **kw: served.update(app=app, kwargs=kw))

    run_daemon(port=0)
    return served, app, engine


def _drive_lifespan(app):
    """Put an ASGI app through one startup/shutdown lifespan cycle."""
    import asyncio

    events = ["lifespan.startup", "lifespan.shutdown"]

    async def receive():
        return {"type": events.pop(0)}

    async def send(_message):
        pass

    asyncio.run(app({"type": "lifespan"}, receive, send))


def test_run_daemon_serves_an_app_that_drains_on_shutdown(monkeypatch,
                                                          tmp_pmb_home):
    """The wrapper only helps if `run_daemon` actually hands it to uvicorn.

    Testing `_drain_on_lifespan_shutdown` in isolation proves the wrapper
    drains, not that the daemon serves a wrapped app - passing the raw app to
    `uvicorn.run` would leave every other test in this section green while the
    daemon silently recorded nothing again.
    """
    flushed: list[bool] = []
    monkeypatch.setattr("pmb.mcp.perf.flush_perf",
                        lambda *a, **kw: flushed.append(True))

    served, raw_app, _engine = _serve_and_capture(monkeypatch, tmp_pmb_home)

    assert served, "run_daemon never reached uvicorn.run"
    # `run_daemon`'s own `finally` fallback has already drained once by now -
    # only a drain caused by the lifespan cycle itself proves the wrapping.
    baseline = len(flushed)
    _drive_lifespan(served["app"])
    assert len(flushed) > baseline, (
        "the app handed to uvicorn does not drain on lifespan shutdown")
    assert raw_app.scopes == ["lifespan"], "the real app was never called"


def test_run_daemon_requires_lifespan_support(monkeypatch, tmp_pmb_home):
    """uvicorn's default `lifespan="auto"` can silently disable the drain.

    In auto mode, an app that rejects the lifespan protocol during startup
    makes uvicorn log a warning and serve on without a shutdown phase - the
    wrapper is then never asked to drain, and the re-raised SIGTERM still
    kills the process from inside `uvicorn.run`, so `run_daemon`'s `finally`
    does not run either. Nothing would record telemetry and nothing would say
    so. This daemon depends on lifespan, so it asks for it explicitly and
    fails loudly instead.
    """
    monkeypatch.setattr("pmb.mcp.perf.flush_perf", lambda *a, **kw: None)

    served, _raw_app, _engine = _serve_and_capture(monkeypatch, tmp_pmb_home)

    assert served["kwargs"].get("lifespan") == "on"


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Root cause: spawns a real uvicorn daemon, binds a port and waits on model
# prewarm - the waits are wall-clock, so a loaded runner can time it out.
@pytest.mark.quarantined
@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
def test_sigterm_on_a_real_daemon_persists_buffered_rows(tmp_path):
    """The end of the story the other tests only tell in pieces.

    Everything above stubs `flush_perf`, so nothing proves a buffered row
    reaches SQLite on the path that actually stops a daemon. This drives a
    real daemon over streamable-http with fewer than `_PERF_FLUSH_EVERY`
    calls - the case where only a shutdown drain can save them - and counts
    rows in the workspace database after SIGTERM.
    """
    import asyncio
    import os as _os  # noqa: F401 - shadow-free alias for the subprocess env
    import signal
    import sqlite3
    import subprocess
    import sys
    import time
    import urllib.request

    pytest.importorskip("uvicorn")
    Client = pytest.importorskip("fastmcp").Client

    calls = 6  # < _PERF_FLUSH_EVERY (25)
    home = tmp_path / "pmb_home"
    home.mkdir()
    port = _free_port()
    env = dict(_os.environ, PMB_HOME=str(home), PMB_WORKSPACE="e2ews")

    proc = subprocess.Popen(
        [sys.executable, "-m", "pmb.cli", "daemon", "run",
         "--port", str(port), "--idle-exit-min", "0"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    try:
        token = None
        deadline = time.time() + 180  # cold start includes the model prewarm
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"daemon exited early: {proc.stderr.read()[-2000:]}")
            token_file = home / "daemon.token"
            if token_file.exists():
                token = token_file.read_text().strip()
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/internal/health",
                        headers={"Authorization": f"Bearer {token}"})
                    urllib.request.urlopen(req, timeout=2).read()
                    break
                except Exception:
                    pass
            time.sleep(1)
        else:
            pytest.fail("daemon never became healthy")

        async def _hit():
            async with Client(f"http://127.0.0.1:{port}/mcp",
                              auth=token) as client:
                for _ in range(calls):
                    await client.call_tool("recall", {"query": "e2e ping"})

        asyncio.run(_hit())

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    db = home / "workspaces" / "e2ews" / "events.sqlite"
    assert db.exists(), "daemon never created the workspace database"
    with sqlite3.connect(str(db)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "mcp_calls" in tables, (
            "no perf table at all - every buffered row died with the daemon")
        assert conn.execute("SELECT count(*) FROM mcp_calls").fetchone()[0] \
            == calls


def test_drain_before_exit_never_raises(monkeypatch):
    """Shutdown must not be blocked by a broken engine or a perf failure."""
    from unittest.mock import MagicMock

    from pmb.mcp.daemon import _drain_before_exit

    def _boom(*a, **kw):
        raise RuntimeError("perf is gone")

    # never the real flush: it would write whatever `_PERF_BUF` holds to a
    # live database outside tmp_path
    monkeypatch.setattr("pmb.mcp.perf.flush_perf", _boom)
    engine = MagicMock()
    engine.wait_for_writes.side_effect = RuntimeError("engine is gone")
    _drain_before_exit(engine)  # must not propagate
