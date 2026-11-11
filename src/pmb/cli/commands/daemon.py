"""`pmb daemon` - manage the persistent memory daemon (B-phase).

The daemon holds ONE warm Engine + embedding model + LanceDB so hook-based
auto-recall gets real semantic recall instead of the per-process cold skip.
`start` spawns it detached; `run` is the foreground worker `start` launches.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import typer

from pmb.cli._common import console

daemon_app = typer.Typer(help="Manage the persistent memory daemon (warm hooks).")


def _health_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/internal/health"


def _probe(host: str, port: int, timeout: float = 0.5) -> dict | None:
    try:
        with urllib.request.urlopen(_health_url(host, port), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _free_port(host: str, port: int, tries: int = 20) -> int:
    """First bindable port at or above `port` (so the daemon never silently dies
    on a busy 8765 - e.g. when a streamable-http MCP server already holds it)."""
    for p in range(port, port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                if os.name != "nt":
                    # Match uvicorn's bind policy when restarting after an HTTP connection.
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, p))
                return p
            except OSError:
                continue
    return port  # give up; let the daemon surface the bind error to its log


def _daemon_log_path():
    """`<pmb_home>/daemon.log` - where the spawned daemon's stdout/stderr go, so
    a crash (bad port, model failure) is diagnosable instead of swallowed."""
    from pathlib import Path
    try:
        from pmb.core.workspace import detect_workspace
        home = detect_workspace().pmb_home
    except Exception:
        home = Path(os.environ.get("PMB_HOME") or (Path.home() / ".pmb"))
    return Path(home) / "daemon.log"


@daemon_app.command()
def start(
    port: int = typer.Option(8765, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
):
    """Start the memory daemon in the background (idempotent)."""
    from pmb.mcp.registry import find_live_daemon
    live = find_live_daemon()
    if live:
        console.print(
            f"[green]Daemon already running[/] (pid {live.get('pid')}, "
            f"port {live.get('port')}).")
        return

    # Auto-pick a free port so a busy 8765 (e.g. a streamable-http MCP server
    # already on it) doesn't make the daemon die silently on bind.
    requested = port
    port = _free_port(host, port)
    if port != requested:
        console.print(
            f"[yellow]Port {requested} is busy. Starting on {port}.[/] "
            "HTTP MCP clients configured for the old port need to reconnect. "
            "After the daemon starts, run `pmb connect <agent> --daemon` "
            "with the original scope, or update the port in the client's MCP URL."
        )

    kwargs: dict = {}
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP - background daemon with no
        # flashing console window; survives the launching process exiting.
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True

    # Send the daemon's stdout/stderr to a log (NOT DEVNULL) so a crash - bad
    # port, model failure - is diagnosable instead of swallowed.
    log_path = _daemon_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 (child owns it)
    except Exception:
        out = subprocess.DEVNULL
    # Carry the configured tool profile to the spawned daemon so a manual
    # `pmb daemon start/restart` serves the same profile as the connect /
    # auto-start path. Without this it falls back to the tight `minimal`
    # default, and an agent loses forget / recall_smart / index_project on
    # every restart.
    env = dict(os.environ)
    if not env.get("PMB_TOOL_PROFILE"):
        try:
            from pmb.config import Config
            from pmb.core.workspace import detect_workspace
            ws = detect_workspace()
            prof = str(Config(
                workspace_dir=ws.storage_dir, pmb_home=ws.pmb_home,
            ).get("daemon.tool_profile") or "").strip()
            if prof:
                env["PMB_TOOL_PROFILE"] = prof
        except Exception:
            pass
    try:
        subprocess.Popen(
            [sys.executable, "-m", "pmb.cli", "daemon", "run",
             "--port", str(port), "--host", host],
            stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env, **kwargs,
        )
    finally:
        if out is not subprocess.DEVNULL:
            out.close()

    with console.status("starting memory daemon…"):
        for _ in range(50):  # ~5s for uvicorn to bind (model warms async after)
            time.sleep(0.1)
            data = _probe(host, port)
            if data:
                console.print(
                    f"[green]✓ Daemon up[/] v{data.get('version')} · "
                    f"workspace {data.get('workspace')} · "
                    f"warm={data.get('warm')} · port {port}\n"
                    f"[dim]Hooks will use it on the next message. "
                    f"Embedding model finishes warming in the background.[/]")
                return
    console.print(
        "[yellow]Daemon spawned but health didn't respond yet.[/] "
        f"Check `pmb daemon status` in a moment, or the log:\n[dim]{log_path}[/]")


@daemon_app.command()
def run(
    port: int = typer.Option(8765, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
    idle_exit_min: float | None = typer.Option(
        None, "--idle-exit-min",
        help="Exit after N idle minutes (default from config; 0 = never)."),
):
    """Run the daemon in the FOREGROUND (this is what `start` launches)."""
    from pmb.mcp.daemon import run_daemon
    raise typer.Exit(run_daemon(host=host, port=port, idle_exit_min=idle_exit_min))


@daemon_app.command()
def stop():
    """Stop the running memory daemon."""
    from pmb.mcp.registry import find_live_daemon, unregister_server
    live = find_live_daemon()
    if not live:
        console.print("[yellow]No daemon running.[/]")
        return
    pid = int(live.get("pid"))
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        console.print(f"[red]Failed to stop pid {pid}:[/] {e}")
        return
    try:
        unregister_server(pid)
    except Exception:
        pass
    console.print(f"[green]✓ Stopped daemon[/] (pid {pid}).")


@daemon_app.command()
def status(
    port: int = typer.Option(8765, "--port"),
    host: str = typer.Option("127.0.0.1", "--host"),
):
    """Show whether the memory daemon is running and warm."""
    from pmb.mcp.registry import find_live_daemon
    live = find_live_daemon()
    if not live:
        console.print(
            "[yellow]No daemon running.[/] Start one with "
            "[cyan]pmb daemon start[/] for warm hook recall.")
        return
    data = _probe(live.get("host") or host, int(live.get("port") or port))
    up_s = max(0.0, time.time() - float(live.get("started_at") or time.time()))
    rss = live.get("rss_mb")
    console.print(
        f"[green]Daemon running[/] · pid {live.get('pid')} · "
        f"port {live.get('port')} · uptime {up_s/60:.0f}min"
        + (f" · RSS {rss:.0f}MB" if rss else ""))
    if data:
        console.print(
            f"  v{data.get('version')} · workspace {data.get('workspace')} · "
            f"warm={data.get('warm')}"
            + ("" if data.get("warm") else " [dim](embedding model still loading)[/]"))
    else:
        console.print("  [yellow]health endpoint did not respond[/]")


@daemon_app.command()
def restart(
    port: int | None = typer.Option(None, "--port"),
    host: str | None = typer.Option(None, "--host"),
):
    """Restart on the current address unless a new one is supplied."""
    from pmb.mcp.registry import find_live_daemon

    live = find_live_daemon() or {}
    if port is None:
        port = int(live.get("port") or 8765)
    if host is None:
        host = live.get("host") or "127.0.0.1"
    stop()
    time.sleep(0.5)
    start(port=port, host=host)


@daemon_app.command(name="kill-all")
def kill_all():
    """Kill the daemon AND every registered PMB process, then clear the registry.

    Use when stray / duplicate pmb processes pile up - each warm one holds the
    embedding model in RAM, which on a small box starves the daemon. On Windows
    this is a tree-kill (/T), so child prewarm threads go too.
    """
    import signal

    from pmb.mcp.registry import list_servers, unregister_server
    try:
        entries = list_servers(prune=False, with_rss=False)
    except Exception as e:
        console.print(f"[red]Could not read the registry:[/] {e}")
        entries = []
    pids = sorted({int(e["pid"]) for e in entries if e.get("pid")})
    if not pids:
        console.print("[yellow]No registered PMB processes to kill.[/]")
        return
    killed, gone = [], []
    for pid in pids:
        try:
            if os.name == "nt":
                r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True)
                ok = r.returncode == 0
            else:
                os.kill(pid, signal.SIGTERM)
                ok = True
        except Exception:
            ok = False
        (killed if ok else gone).append(pid)
        try:
            unregister_server(pid)
        except Exception:
            pass
    console.print(f"[green]✓ Killed[/] {len(killed)} PMB process(es): {killed}")
    if gone:
        console.print(f"[dim]Already gone / not killable: {gone}[/]")
    console.print("[dim]Registry cleared. Start fresh with "
                  "[cyan]pmb daemon start[/].[/]")
