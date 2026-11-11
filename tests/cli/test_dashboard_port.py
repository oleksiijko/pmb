from __future__ import annotations

import errno
import socket
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pmb.cli.main import app


@pytest.fixture
def bound_ports(monkeypatch) -> list[int]:
    ports: list[int] = []
    engine = SimpleNamespace(workspace=SimpleNamespace(name="Test", id="test"))
    monkeypatch.setattr("pmb.cli.commands.capture.Engine", lambda: engine)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever",
                        lambda server: ports.append(server.server_port))
    return ports


def test_dashboard_skips_occupied_default_port(bound_ports, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        busy = listener.getsockname()[1]
        monkeypatch.setattr("pmb.cli.commands.capture._DASHBOARD_DEFAULT_PORT", busy)
        result = CliRunner().invoke(app, ["dashboard"])

    assert result.exit_code == 0, result.output
    port, = bound_ports
    assert port > 0 and port != busy
    assert f"Port {busy} is unavailable" in result.output
    assert f"PMB dashboard at http://127.0.0.1:{port}" in result.output


def test_dashboard_recovers_when_server_bind_fails(bound_ports, monkeypatch):
    bind = ThreadingHTTPServer.server_bind

    def reject_fixed_port(server):
        if server.server_address[1] != 0:
            raise OSError(errno.EADDRINUSE, "port became busy")
        bind(server)

    monkeypatch.setattr(ThreadingHTTPServer, "server_bind", reject_fixed_port)
    result = CliRunner().invoke(app, ["dashboard"])

    assert result.exit_code == 0, result.output
    port, = bound_ports
    assert port > 0
    assert f"PMB dashboard at http://127.0.0.1:{port}" in result.output


def test_dashboard_reports_failed_explicit_port(bound_ports, monkeypatch):
    def reject_bind(server):
        raise PermissionError(errno.EACCES, "forbidden")

    monkeypatch.setattr(ThreadingHTTPServer, "server_bind", reject_bind)
    result = CliRunner().invoke(app, ["dashboard", "--port", "18888"])

    assert result.exit_code == 2, result.output
    assert "Dashboard could not bind" in result.output
    assert "http://127.0.0.1:18888" in result.output
    assert bound_ports == []


def test_dashboard_open_uses_the_bound_port(bound_ports, monkeypatch):
    opened: list[str] = []
    ready = threading.Event()

    def open_url(url: str) -> bool:
        opened.append(url)
        ready.set()
        return True

    monkeypatch.setattr(webbrowser, "open", open_url)
    result = CliRunner().invoke(app, ["dashboard", "--port", "0", "--open"])

    assert result.exit_code == 0, result.output
    port, = bound_ports
    assert port > 0
    assert ready.wait(timeout=5)
    assert opened == [f"http://127.0.0.1:{port}"]
