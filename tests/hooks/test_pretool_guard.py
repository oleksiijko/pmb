"""R11: PreToolUse lesson guard.

Fires a matching rule at TOOL-CALL time ("use pnpm, never npm" when the agent
is about to run `npm install`), even if the agent never called memory. STRONG
match required (>= 2 distinctive overlapping tokens incl >= 1 identifier-grade),
fires at most once per session, advisory only. Daemon-served — the thin client
just relays it.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pmb.core.engine import Engine
from pmb.mcp.daemon import pretool_lessons


@pytest.fixture
def eng(tmp_pmb_home, tmp_workspace_dir):
    e = Engine(cwd=tmp_workspace_dir, pmb_home=tmp_pmb_home,
               config_overrides={"recall.cache_size": 0})
    # an identifier-grade rule (record_batch is is_strong) + a two-word rule
    e.record_fact("always use record_batch, never many record_fact calls",
                  metadata={"kind": "lesson", "source": "lesson"})
    e.record_fact("the lockfile is pnpm-lock; use pnpm install, never npm",
                  metadata={"kind": "lesson", "source": "lesson"})
    return e


def test_fires_on_identifier_match(eng):
    # one identifier-grade overlap (record_fact) is enough
    fired = pretool_lessons(eng, "Edit the importer to call record_fact in a loop", set())
    assert fired, "an identifier-grade overlap must fire the guard"
    assert "record_batch" in fired[0]["content"]


def test_fires_on_two_distinctive_words(eng):
    # two distinctive overlapping words (pnpm + npm) fire even without an _ token
    fired = pretool_lessons(eng, "Bash: pnpm install then npm dedupe", set())
    assert any("pnpm" in (L["content"] or "") for L in fired)


def test_fires_at_most_once_per_session(eng):
    seen: set = set()
    assert pretool_lessons(eng, "run record_fact in the importer", seen)
    again = pretool_lessons(eng, "another record_fact call here", seen)
    assert not again, "the same lesson must not re-fire in the same session"


def test_no_fire_on_unrelated(eng):
    assert not pretool_lessons(eng, "Read the project README documentation", set())
    assert not pretool_lessons(eng, "Bash ls -la /tmp", set())


# ── thin client relays the daemon's guard text ──────────────────────────────

class _Stub(BaseHTTPRequestHandler):
    version_str = "0.0.0"

    def log_message(self, *a):
        pass

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        self.rfile.read(ln)
        body = json.dumps({"context": "[pmb] Relevant rule(s):\n  ! use pnpm",
                           "version": type(self).version_str,
                           "source": "daemon"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_thin_client_pretool_relays(tmp_path, monkeypatch, capsys):
    import pmb
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    _Stub.version_str = pmb.__version__
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import pmb.hookclient.__main__ as hc
        monkeypatch.setattr("pmb.mcp.registry.find_live_daemon",
                            lambda: {"host": "127.0.0.1", "port": port}, raising=False)
        monkeypatch.setattr("pmb.mcp.daemon.read_daemon_token", lambda: "t",
                            raising=False)

        class _In:
            def __init__(self, d):
                self._d = d.encode()
                self.buffer = self
            def read(self):
                return self._d

        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "npm install lodash"},
                              "session_id": "s1"})
        monkeypatch.setattr(sys, "stdin", _In(payload))
        rc = hc.main(["pretool", "--quiet"])
        assert rc == 0
        out = capsys.readouterr().out
        # PreToolUse stdout must be valid JSON with the lesson in
        # additionalContext - a bare-text hook made Cursor block the tool call.
        payload = json.loads(out)
        assert "use pnpm" in payload["additionalContext"]
        assert "decision" not in payload, \
            "advisory guard must never drive the host's permission flow"
    finally:
        srv.shutdown()
