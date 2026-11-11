"""The stdio transport must leave fd 0 free on Windows.

While a synchronous read is pending on the stdin pipe, Windows makes other
operations on that pipe wait for the client's next message. An MCP server
runs two kinds of such operations:

* native runtimes that seek their standard descriptors while loading. The
  embedding stack's extension modules stalled this way on first use;
* child processes that inherit stdin, such as the ``git`` calls behind
  workspace detection.

An MCP client sends nothing while it waits for a tool result, so the call
hangs until the client gives up. Copilot CLI cancels after 180 s, and that
cancellation is the message that finally releases the stall.

``main()`` must move the transport off fd 0 before serving. The probe server
below stands in for FastMCP: like the real stdio transport, it keeps a read
pending on ``sys.stdin.buffer`` and then runs both kinds of operation.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from pmb.mcp import server as mcp_server

# A blocked operation cannot finish before the test releases it, so the gap
# between these values separates a regression from a slow runner.
RELEASE_AFTER_S = 8.0
LIMIT_S = 5.0

CHILD = textwrap.dedent(
    """
    import json, os, subprocess, sys, threading, time
    import pmb.mcp.server as server_module

    class ProbeServer:
        name = "stdin-isolation-probe"

        def run(self):
            def pump():
                while sys.stdin.buffer.readline():
                    pass

            threading.Thread(target=pump, daemon=True).start()
            time.sleep(0.5)
            print(json.dumps({"ready": True}), flush=True)
            timings = {}
            start = time.monotonic()
            try:
                os.lseek(0, 0, os.SEEK_CUR)
            except OSError:
                pass
            timings["seek_fd0"] = time.monotonic() - start
            start = time.monotonic()
            subprocess.run([sys.executable, "-c", "pass"], timeout=120)
            timings["child_inheriting_stdin"] = time.monotonic() - start
            print(json.dumps(timings), flush=True)
            os._exit(0)

    server_module.build_server = lambda **kwargs: ProbeServer()
    server_module.main()
    """
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pipe semantics")
def test_stdio_transport_leaves_fd0_free(tmp_pmb_home, tmp_path):
    env = {**os.environ, "PMB_MCP_TRANSPORT": "stdio"}
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=tmp_path, env=env, text=True,
    )
    lines: queue.Queue = queue.Queue()

    def collect():
        for line in child.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=collect, daemon=True).start()

    def next_message(timeout: float):
        line = lines.get(timeout=timeout)
        if line is None:
            pytest.fail(f"probe exited early: {child.stderr.read()[-2000:]}")
        return json.loads(line)

    try:
        assert next_message(timeout=120) == {"ready": True}
        result = None
        wait = RELEASE_AFTER_S
        give_up = time.monotonic() + 120
        while result is None:
            try:
                result = next_message(timeout=wait)
            except queue.Empty:
                # Regression: release the blocked call so the timings get reported.
                assert time.monotonic() < give_up, "probe never finished"
                child.stdin.write("{}\n")
                child.stdin.flush()
                wait = 2.0
    finally:
        child.kill()
        child.wait(timeout=30)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()

    assert result["seek_fd0"] < LIMIT_S, result
    assert result["child_inheriting_stdin"] < LIMIT_S, result


def test_isolation_leaves_an_already_moved_stdin_alone(monkeypatch):
    class MovedStdin:
        def fileno(self):
            return 7

    moved = MovedStdin()
    monkeypatch.setattr(sys, "stdin", moved)
    monkeypatch.setattr(os, "dup", lambda fd: pytest.fail("fd 0 was duplicated again"))
    mcp_server._isolate_stdio_stdin()
    assert sys.stdin is moved
