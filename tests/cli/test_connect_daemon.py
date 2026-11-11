"""S6: `pmb connect --daemon` points JSON hosts at the ONE shared warm daemon
over streamable-HTTP (type: http) instead of a stdio Engine+model per client.

Two safety-critical properties:
  * The daemon token is PERSISTENT, so the baked `Authorization` header stays
    valid across daemon restarts (idle-exit + hook autostart).
  * codex can't take an HTTP entry, so under daemon mode it wires the lightweight
    `pmb mcp proxy` stdio<->daemon bridge (still ONE shared warm process); hosts
    that can't share the daemon at all (extensions / remote) fall back to stdio
    and SAY SO, instead of silently writing a broken entry.

HOME + PMB_HOME are redirected to tmp so nothing touches the real config /
CLAUDE.md / daemon token.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(home))   # Windows Path.home()
    monkeypatch.setenv("HOME", str(home))          # POSIX Path.home()
    monkeypatch.setenv("PMB_HOME", str(tmp_path / "pmb_home"))
    return tmp_path


def test_daemon_token_is_persistent(isolated):
    from pmb.mcp.daemon import read_daemon_token, write_daemon_token
    t1 = write_daemon_token()
    t2 = write_daemon_token()
    assert t1 and t1 == t2 == read_daemon_token(), "token must persist across calls"
    t3 = write_daemon_token(rotate=True)
    assert t3 != t1, "rotate=True must mint a fresh token"
    assert write_daemon_token() == t3, "and then persist the new one"


def test_make_daemon_entry_shape(isolated):
    from pmb.cli.connect import make_daemon_entry
    e = make_daemon_entry(port=8765)
    assert e["type"] == "http"
    assert e["url"] == "http://127.0.0.1:8765/mcp"
    assert e["headers"]["Authorization"].startswith("Bearer ")
    # custom host/port/path flow through
    e2 = make_daemon_entry(host="127.0.0.1", port=18888, path="/mcp")
    assert e2["url"] == "http://127.0.0.1:18888/mcp"


def test_connect_claude_daemon_writes_http_entry(isolated):
    from pmb.cli.connect import connect as do_connect
    r = do_connect("claude-code", cwd=isolated, scope="project",
                   install_hooks=False, use_daemon=True)
    assert r["daemon_http"] is True
    assert r["entry"]["type"] == "http"
    assert r["entry"]["url"].endswith("/mcp")
    assert "Authorization" in r["entry"]["headers"]


def test_reconnect_uses_live_daemon_port(isolated, monkeypatch):
    from pmb.cli.connect import connect as do_connect

    monkeypatch.setattr("pmb.mcp.registry.find_live_daemon",
                        lambda _home: {"port": 18765})
    result = do_connect("claude-code", cwd=isolated, scope="project",
                        install_hooks=False, use_daemon=True)
    assert result["entry"]["url"] == "http://127.0.0.1:18765/mcp"


def test_connect_claude_stdio_default_unchanged(isolated):
    from pmb.cli.connect import connect as do_connect
    r = do_connect("claude-code", cwd=isolated, scope="project",
                   install_hooks=False, use_daemon=False)
    assert r["daemon_http"] is False
    # stdio entry: command-shaped, no http url
    assert "type" not in r["entry"] or r["entry"].get("type") != "http"
    assert "command" in r["entry"]


def test_connect_codex_daemon_uses_stdio_proxy(isolated):
    # codex can't take an HTTP entry, but under daemon mode it now wires the
    # lightweight `pmb mcp proxy` stdio<->daemon bridge (shares the ONE warm
    # process) instead of falling back to its own heavy Engine+model.
    from pmb.cli.connect import connect as do_connect
    r = do_connect("codex", cwd=isolated, scope="project",
                   install_hooks=False, use_daemon=True)
    assert r["daemon_http"] is False
    assert r["daemon_proxy"] is True
    assert r["daemon_http_unavailable"] is False     # it IS sharing the daemon
    e = r["entry"]
    assert "command" in e                             # stdio-shaped, not http
    assert e.get("args", [])[-2:] == ["mcp", "proxy"]  # the bridge, not pmb-mcp


def test_connect_codex_stdio_keeps_heavy_server(isolated):
    # --stdio (use_daemon=False) keeps the old per-session in-process server.
    from pmb.cli.connect import connect as do_connect
    r = do_connect("codex", cwd=isolated, scope="project",
                   install_hooks=False, use_daemon=False)
    assert r["daemon_proxy"] is False
    assert r["entry"].get("args", [])[-2:] != ["mcp", "proxy"]


# ── S6-default: the flip + its safety pins ──────────────────────────────────

def _cfg(isolated):
    import os
    from pathlib import Path

    from pmb.config import Config
    return Config(pmb_home=Path(os.environ["PMB_HOME"]))


def test_prep_daemon_http_gates_and_pins(isolated):
    # autostart on (default) → viable, and it PINS idle_exit_min=0 + the profile
    # so the shared daemon stays reachable for the MCP connection.
    from pmb.cli.connect import _prep_daemon_http
    assert _prep_daemon_http(None, "lean") is True
    cfg = _cfg(isolated)
    assert cfg.get("daemon.idle_exit_min") == 0
    assert cfg.get("daemon.tool_profile") == "lean"


def test_prep_daemon_http_falls_back_when_autostart_off(isolated):
    from pmb.cli.connect import _prep_daemon_http
    _cfg(isolated).set_global("daemon.autostart", False)
    # autostart off → not viable (caller writes a stdio entry instead)
    assert _prep_daemon_http(None, "lean") is False


def test_connect_claude_default_flip_pins_idle_exit(isolated):
    # With connect.default_daemon on (the flipped default), a JSON host gets the
    # HTTP entry AND the daemon is pinned to never idle-exit.
    from pmb.cli.connect import connect as do_connect
    r = do_connect("claude-code", cwd=isolated, scope="project",
                   install_hooks=False, use_daemon=True)
    assert r["daemon_http"] is True
    assert _cfg(isolated).get("daemon.idle_exit_min") == 0


def test_connect_daemon_falls_back_to_stdio_when_autostart_off(isolated):
    from pmb.cli.connect import connect as do_connect
    _cfg(isolated).set_global("daemon.autostart", False)
    r = do_connect("claude-code", cwd=isolated, scope="project",
                   install_hooks=False, use_daemon=True)
    # autostart off → HTTP not viable → stdio entry, not a broken http one
    assert r["daemon_http"] is False
    assert "command" in r["entry"]


def test_connect_default_daemon_config_default_is_on():
    # the flip: connect.default_daemon defaults True
    from pmb.config import SCHEMA
    assert SCHEMA["connect.default_daemon"].default is True


def test_ensure_daemon_started_is_noop_under_pytest(monkeypatch):
    # PYTEST_CURRENT_TEST is set during a test → the spawn guard returns early.
    import subprocess

    from pmb.cli.commands.manage import _ensure_daemon_started
    called = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: called.setdefault("spawned", True))
    _ensure_daemon_started(None, None)
    assert "spawned" not in called


def test_ensure_daemon_started_skips_when_already_live(monkeypatch):
    import subprocess

    from pmb.cli.commands.manage import _ensure_daemon_started
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)   # bypass the pytest guard
    monkeypatch.setattr("pmb.mcp.registry.find_live_daemon",
                        lambda: {"pid": 1, "port": 8765}, raising=False)
    called = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: called.setdefault("spawned", True))
    _ensure_daemon_started(None, None)
    assert "spawned" not in called, "must not spawn a second daemon"
