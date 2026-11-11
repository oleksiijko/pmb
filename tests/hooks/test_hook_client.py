"""S2: the stdlib-only `pmb-hook` fast lane.

Covers: import budget (no heavy deps), payload extraction, the track-action
inline hot-path (no daemon, no engine), and a daemon-served prepare-context
round-trip against a stub HTTP server.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_SRC = str(next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file()) / "src")
sys.path.insert(0, _SRC)


# ── import budget ───────────────────────────────────────────────────

def test_hookclient_import_is_light():
    code = (
        "import sys; import pmb.hookclient.__main__ as m; "
        "heavy=[x for x in ('numpy','typer','fastmcp','mcp','pmb.core.engine',"
        "'sentence_transformers','lancedb','torch') if x in sys.modules]; "
        "print(','.join(heavy))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", f"pmb-hook pulled heavy modules: {r.stdout!r}"


# ── payload extraction + flag parsing ───────────────────────────────

def test_extract_prompt_from_json_and_raw():
    from pmb.hookclient.__main__ import _extract_prompt
    assert _extract_prompt('{"prompt": "fix the auth bug"}') == "fix the auth bug"
    assert _extract_prompt('{"hook_event_name":"x","message":"hi there"}') == "hi there"
    assert _extract_prompt("plain text message") == "plain text message"
    assert _extract_prompt("") == ""


def test_flag_helpers():
    from pmb.hookclient.__main__ import _has, _opt
    args = ["--max-chars", "1234", "--quiet"]
    assert _opt(args, "--max-chars") == "1234"
    assert _opt(args, "--window", "30") == "30"
    assert _has(args, "--quiet") is True
    assert _has(args, "--verbose") is False


# ── track-action inline hot path (no daemon, no engine) ─────────────

def test_track_action_inline_insert(tmp_path, monkeypatch):
    monkeypatch.setenv("PMB_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("PMB_WORKSPACE", "hooktest")
    # a fresh workspace so detect_workspace + ambient_log have a db to write
    from pmb.core.engine import Engine
    ws = tmp_path / "proj"
    ws.mkdir()
    eng = Engine(cwd=ws, pmb_home=tmp_path / "h")
    db_path = eng.workspace.db_path

    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": "/proj/auth.py"},
        "tool_response": {"success": True},
        "session_id": "s1",
    })

    class _Stdin:
        def __init__(self, data): self.buffer = self
        def read(self): return payload.encode("utf-8")

    monkeypatch.setattr(sys, "stdin", _Stdin(payload))
    monkeypatch.chdir(ws)

    from pmb.hookclient.__main__ import main
    rc = main(["track-action", "--quiet"])
    assert rc == 0

    with sqlite3.connect(str(db_path)) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM agent_actions WHERE tool='Edit'").fetchone()[0]
    assert n == 1


# ── S4: daemon workspace-binding guard (pure function, no server) ───

def test_workspace_matches_guard():
    from pmb.mcp.daemon import _workspace_matches

    class _WS:
        id = "abc123"
        name = "myproj"

    class _Eng:
        workspace = _WS()

    eng = _Eng()
    # no client hint → best-effort serve (unchanged single-workspace contract)
    assert _workspace_matches(eng, None) is True
    assert _workspace_matches(eng, "") is True
    # matching id or name → serve
    assert _workspace_matches(eng, "abc123") is True
    assert _workspace_matches(eng, "MyProj") is True   # case-insensitive
    # a DIFFERENT workspace → refuse (this is the wrong-context-injection guard)
    assert _workspace_matches(eng, "otherproj") is False


# ── S10: honest trace injection ─────────────────────────────────────

def test_inject_trace_total_stamps_total_and_source():
    from pmb.hookclient.__main__ import _inject_trace_total
    baked = "== PMB auto-context ==  [intents=PAST_QUERY latency=42ms]\nbody"
    out = _inject_trace_total(baked, "daemon")
    assert "latency=42ms" in out          # inner compute preserved
    assert "source=daemon]" in out        # serving source stamped
    import re
    m = re.search(r"total=(\d+)ms", out)
    assert m, "total wall-time must be stamped"
    assert int(m.group(1)) >= 0
    # the inner latency value is untouched, only the closing bracket is expanded
    assert "latency=42ms total=" in out


def test_inject_trace_total_noop_without_trace():
    from pmb.hookclient.__main__ import _inject_trace_total
    # tracing off → no latency= marker → leave the text exactly as-is
    plain = "== PMB auto-context ==\nbody only, no trace header"
    assert _inject_trace_total(plain, "cold") == plain
    assert _inject_trace_total("", "daemon") == ""


# ── cold-path stdout decode is pinned to UTF-8 (Cyrillic mojibake guard) ──

def test_cold_cli_never_propagates_child_exit_code(monkeypatch):
    """Regression: the cold path returned the child CLI's exit code. Claude Code
    reads exit 2 from a UserPromptSubmit hook as "block the prompt", and stderr
    is captured, so any CLI usage error silently blocked the user's turn."""
    import subprocess as _sp

    import pmb.hookclient.__main__ as hc

    class _R:
        returncode = 2
        stdout = ""

    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: _R())
    assert hc._exec_full_cli(["prepare-context", "--quiet"]) == 0


def test_cold_prepare_context_accepts_prompt_starting_with_dash(monkeypatch):
    """Regression: a prompt starting with "-" (a Markdown bullet) reached the
    cold CLI as an unknown option → exit 2 → prompt blocked. The argv the hook
    builds must parse with the real CLI, the prompt landing in MESSAGE."""
    from typer.main import get_command

    import pmb.hookclient.__main__ as hc
    from pmb.cli._common import app

    seen: dict = {}
    monkeypatch.setattr(hc, "_daemon_request", lambda *a, **k: None)
    monkeypatch.setattr(hc, "_exec_full_cli", lambda cli, **k: seen.setdefault("cli", cli) and 0)
    monkeypatch.setattr(hc, "_read_stdin_utf8", lambda: json.dumps({"prompt": "- first bullet"}))
    hc.cmd_prepare_context(["--quiet"])

    name, *args = seen["cli"]
    group = get_command(app)
    cmd = group.get_command(group.make_context("pmb", [name]), name)
    ctx = cmd.make_context(name, args)  # raises click.UsageError on a bad parse
    assert ctx.params["message"] == "- first bullet"


def test_cold_cli_decodes_child_stdout_as_utf8(monkeypatch):
    """Regression: the cold fallback ran `subprocess.run(..., text=True)` with no
    encoding, so the child `pmb` CLI's UTF-8 stdout was decoded with the Windows
    locale (cp1251) and DOUBLE-encoded - the agent saw the auto-context's 'Карта
    проекта' arrive as 'РљР°СЂС‚Р° РїСЂРѕРµРєС‚Р°'. The cold path must pin the
    decode (and the child's output codec) to UTF-8, matching the daemon path."""
    import subprocess as _sp

    import pmb.hookclient.__main__ as hc

    seen: dict = {}

    class _R:
        returncode = 0
        stdout = "контекст"

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen.update(kw)
        return _R()

    monkeypatch.setattr(_sp, "run", _fake_run)

    rc = hc._exec_full_cli(["session-restore", "--quiet"])
    assert rc == 0
    assert seen.get("encoding") == "utf-8", "child stdout must be decoded as UTF-8"
    assert seen.get("errors") == "replace"
    # the child is also told to emit UTF-8 (belt-and-suspenders across hosts)
    assert seen.get("env", {}).get("PYTHONIOENCODING") == "utf-8"
    # SAC-safe: the cold fallback runs the SIGNED interpreter, not pmb.exe
    assert seen["cmd"][:3] == [sys.executable, "-m", "pmb.cli"]


# ── daemon-served prepare-context against a stub server ─────────────

class _StubHandler(BaseHTTPRequestHandler):
    version_str = "0.0.0-test"  # overwritten per-test

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(ln)
        body = json.dumps({
            "context": "DAEMON-CONTEXT-OK",
            "version": type(self).version_str,
            "source": "daemon",
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_prepare_context_daemon_served(tmp_path, monkeypatch, capsys):
    import pmb

    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    _StubHandler.version_str = pmb.__version__   # version handshake matches
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # point the client's daemon discovery + token at the stub
        import pmb.hookclient.__main__ as hc
        monkeypatch.setattr(hc, "find_live_daemon", None, raising=False)
        monkeypatch.setattr(
            "pmb.mcp.registry.find_live_daemon",
            lambda: {"host": "127.0.0.1", "port": port}, raising=False)
        monkeypatch.setattr(
            "pmb.mcp.daemon.read_daemon_token", lambda: "tok", raising=False)

        class _Stdin:
            buffer = None
            def __init__(self, data):
                self._d = data.encode("utf-8")
                self.buffer = self
            def read(self): return self._d

        monkeypatch.setattr(sys, "stdin",
                            _Stdin(json.dumps({"prompt": "what port is the db"})))
        rc = hc.main(["prepare-context", "--max-chars", "4000", "--quiet"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DAEMON-CONTEXT-OK" in out
    finally:
        srv.shutdown()


def test_prepare_context_version_mismatch_falls_back(tmp_path, monkeypatch):
    # stub returns a DIFFERENT version → client must reject (return None) and
    # fall back to the full CLI. We stub the CLI exec to observe the fallback.
    monkeypatch.setenv("PMB_HOME", str(tmp_path / "h"))  # S3 heal stamp lands here
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    _StubHandler.version_str = "9.9.9-wrong"
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        import pmb.hookclient.__main__ as hc
        monkeypatch.setattr(
            "pmb.mcp.registry.find_live_daemon",
            lambda: {"host": "127.0.0.1", "port": port}, raising=False)
        monkeypatch.setattr(
            "pmb.mcp.daemon.read_daemon_token", lambda: "tok", raising=False)

        called = {}
        monkeypatch.setattr(hc, "_exec_full_cli",
                            lambda cli_args, detached=False, trace_source=None:
                            called.setdefault("cli", cli_args) or 0)

        class _Stdin:
            def __init__(self, data):
                self._d = data.encode("utf-8")
                self.buffer = self
            def read(self): return self._d

        monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps({"prompt": "hello world"})))
        hc.main(["prepare-context", "--quiet"])
        assert called.get("cli"), "version mismatch should fall back to the full CLI"
        assert called["cli"][0] == "prepare-context"
        assert "hello world" in called["cli"]  # message passed as positional arg
    finally:
        srv.shutdown()


@pytest.mark.perf
def test_prepare_context_client_overhead_p95(monkeypatch):
    """S10 latency budget. Drives the thin client 20x against a stub daemon and
    asserts p95 of the CLIENT overhead (discovery + transport + parse + trace
    inject) stays well under budget. This is the path S2/S10 optimize; a real
    warm daemon adds only its ~10-50 ms inner compute on top. Marked `perf` so
    slow CI runners can deselect it (`-m 'not perf'`)."""
    import pmb

    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    _StubHandler.version_str = pmb.__version__
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        import pmb.hookclient.__main__ as hc
        monkeypatch.setattr(
            "pmb.mcp.registry.find_live_daemon",
            lambda: {"host": "127.0.0.1", "port": port}, raising=False)
        monkeypatch.setattr(
            "pmb.mcp.daemon.read_daemon_token", lambda: "tok", raising=False)

        class _Stdin:
            def __init__(self, data):
                self._d = data.encode("utf-8")
                self.buffer = self
            def read(self):
                return self._d

        samples = []
        for _ in range(20):
            monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps({"prompt": "x"})))
            t0 = time.perf_counter()
            rc = hc.main(["prepare-context", "--quiet"])
            samples.append((time.perf_counter() - t0) * 1000.0)
            assert rc == 0
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        assert p95 < 300.0, f"client overhead p95={p95:.0f}ms exceeds 300ms budget"
    finally:
        srv.shutdown()


# ── PMB_INTERNAL_LLM guard: PMB's own LLM sub-calls must not trip PMB hooks ──

def test_internal_llm_flag_no_ops_every_hook(monkeypatch, capsys):
    """PMB spawns `claude -p` as its OWN LLM backend and sets PMB_INTERNAL_LLM=1.
    Every hook subcommand must then no-op: no daemon round-trip, no output, no
    capture - so PMB's summarizer prompts don't trip PMB's own hooks (the
    correction-capture false-positive bug)."""
    import pmb.hookclient.__main__ as hc

    reached = {"daemon": False}
    monkeypatch.setattr(
        "pmb.mcp.registry.find_live_daemon",
        lambda: reached.__setitem__("daemon", True) or {"host": "127.0.0.1", "port": 1},
        raising=False,
    )
    monkeypatch.setenv("PMB_INTERNAL_LLM", "1")

    class _Stdin:
        def __init__(self, data):
            self._d = data.encode("utf-8")
            self.buffer = self
        def read(self):
            return self._d

    # a prompt that WOULD trip correction-capture if the hook actually ran
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(
        {"prompt": "again, again - stop. Capture the INTENT, not the diff."})))

    for sub in ("prepare-context", "session-restore", "track-action",
                "pretool", "autowrite"):
        assert hc.main([sub]) == 0

    assert reached["daemon"] is False, "internal LLM call must not reach the daemon"
    assert capsys.readouterr().out == "", "internal LLM call must emit no hook output"


def test_claude_cli_client_marks_internal_llm():
    """The other half of the guard: the spawn flags itself (PMB_INTERNAL_LLM=1)
    so the hook entrypoint above can recognise an internal call."""
    from pmb.health.consolidate import ClaudeCLIClient
    env = ClaudeCLIClient(command="claude")._subprocess_env()
    assert env.get("PMB_INTERNAL_LLM") == "1"
