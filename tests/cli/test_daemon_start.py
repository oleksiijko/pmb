"""`pmb daemon start` hardening: a busy port must NOT silently kill the daemon.

  * `_free_port` skips an occupied port and returns the next bindable one
    (so a streamable-http MCP server already on 8765 no longer collides);
  * `_daemon_log_path` resolves under the PMB home, so the spawned daemon's
    stdout/stderr land in a diagnosable log instead of DEVNULL.
"""
from __future__ import annotations

import socket

from typer.testing import CliRunner

from pmb.cli.commands.daemon import _daemon_log_path, _free_port
from pmb.cli.main import app


def test_free_port_skips_a_busy_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))           # OS picks a free port
    busy = s.getsockname()[1]
    s.listen(1)
    try:
        got = _free_port("127.0.0.1", busy)
        assert got != busy, "should have skipped the occupied port"
        assert busy < got < busy + 20
    finally:
        s.close()


def test_free_port_returns_a_truly_free_port_unchanged():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()                          # release it so it's bindable
    assert _free_port("127.0.0.1", free) == free


def test_daemon_log_path_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMB_HOME", str(tmp_path))
    monkeypatch.delenv("PMB_WORKSPACE", raising=False)
    p = _daemon_log_path()
    assert p.name == "daemon.log"
    assert str(tmp_path) in str(p)


def test_restart_keeps_current_port_and_warns_on_fallback(tmp_pmb_home, monkeypatch):
    live_port = 18765
    monkeypatch.setattr("pmb.mcp.registry.find_live_daemon",
                        lambda: {"port": live_port, "host": "127.0.0.1"})

    def stop():
        monkeypatch.setattr("pmb.mcp.registry.find_live_daemon", lambda: None)

    def fallback(host, port):
        assert port == live_port
        return port + 1

    monkeypatch.setattr("pmb.cli.commands.daemon.stop", stop)
    monkeypatch.setattr("pmb.cli.commands.daemon._free_port", fallback)
    monkeypatch.setattr("pmb.cli.commands.daemon.subprocess.Popen", lambda *a, **kw: None)
    monkeypatch.setattr("pmb.cli.commands.daemon._probe",
                        lambda *a: {"workspace": "test", "version": "test"})
    result = CliRunner().invoke(app, ["daemon", "restart"])
    assert result.exit_code == 0, result.output
    assert "Port 18765 is busy. Starting on 18766." in result.output
    assert "HTTP MCP clients" in result.output
