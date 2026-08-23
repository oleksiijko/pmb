"""Persistent memory daemon (B-phase).

The whole reason this exists: the UserPromptSubmit hook spawns a FRESH
`pmb prepare-context` process per user message, whose new Engine is cold, so
semantic recall is skipped (`RECALL_COLD_SKIP`). `pmb warmup` only warms its
own (short-lived) process. The daemon holds ONE warm Engine + embedding model
+ LanceDB for the whole session and answers prepare-context over a tiny local
HTTP API, so the hook gets real semantic recall in <150ms.

It is the SAME streamable-http MCP server (one warm process, bearer auth,
registry tracking) with three extra internal routes mounted via fastmcp's
`custom_route`. Hooks become thin HTTP clients (see cli/commands/ambient.py)
that fall back to the existing cold path the instant the daemon is absent.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from pathlib import Path

import pmb


def _pmb_home() -> Path:
    return Path(os.environ.get("PMB_HOME") or (Path.home() / ".pmb"))


def token_path() -> Path:
    return _pmb_home() / "daemon.token"


def write_daemon_token(rotate: bool = False) -> str:
    """Return the local daemon token, persisting it across restarts (S6).

    A REUSED token is what lets `pmb connect --daemon` bake a stable
    `Authorization: Bearer <token>` into an MCP client's config: if the token
    rotated on every daemon start, that static header would go stale the first
    time the daemon idle-exits and restarts. Pass ``rotate=True`` to force a
    fresh secret (e.g. on suspected compromise). chmod 600 on POSIX so other
    local users can't read it."""
    p = token_path()
    if not rotate:
        existing = read_daemon_token()
        if existing:
            return existing
    tok = secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok, encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    return tok


def read_daemon_token() -> str | None:
    try:
        return token_path().read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


# Shared mutable so the request middleware and the idle watcher agree on when
# the last request arrived.
_LAST_REQUEST = {"ts": time.time()}

# M1: last maintenance-tick summary, surfaced in /internal/health and
# `pmb daemon status`. None until the first tick runs.
_LAST_MAINTENANCE: dict = {"summary": None}

# R11: per-session set of lesson ulids already fired by the PreToolUse guard,
# so a rule interrupts at most once per session (not on every tool call).
_PRETOOL_SEEN: dict[str, set] = {}

def pretool_lessons(engine, excerpt: str, seen: set) -> list:
    """R11 core: lessons that should FIRE for a tool-call excerpt - a STRONG
    match (>= 2 distinctive overlapping tokens incl >= 1 identifier-grade one),
    not yet fired this session, max 2. The guard interrupts the agent, so the
    bar is deliberately higher than ordinary lesson surfacing. Pure function for
    testability; `seen` is mutated with the fired ulids."""
    if not excerpt or not excerpt.strip():
        return []
    try:
        import re as _re

        from pmb.core.text_match import (
            distinctive_tokens,
            is_strong,
            shell_command_names,
        )
    except Exception:
        return []
    # Command name(s) the agent is about to run, extracted STRUCTURALLY (no
    # hardcoded command list). Lets a rule that NAMES a command ('never use git')
    # fire even though 'git' is too short to be a distinctive token.
    cmds = shell_command_names(excerpt)
    q = distinctive_tokens(excerpt)
    try:
        cands = engine.find_lessons(excerpt, limit=6) or []
    except Exception:
        cands = []
    # ALSO pull rules that mention a command we're about to run - distinctive
    # token matching drops short names like 'git', so a bare 'never use git' rule
    # would never even be a candidate. Cheap recent-lesson scan for a raw word hit.
    if cmds:
        have = {L.get("ulid") for L in cands}
        try:
            for L in (engine.find_lessons("", limit=200) or []):
                words = set(_re.findall(r"[a-z0-9_.\-/]+",
                                        (L.get("content") or "").lower()))
                if (cmds & words) and L.get("ulid") not in have:
                    cands.append(L)
                    have.add(L.get("ulid"))
        except Exception:
            pass
    # PRIORITY: a rule that NAMES the command we're about to run beats a fuzzy
    # keyword match for the tiny slot budget. Without this, fuzzy candidates
    # (e.g. lessons that merely share the word "dashboard") take both slots and
    # crowd out the command-bound rule the agent actually needs right here.
    def _words(L) -> set:
        return set(_re.findall(r"[a-z0-9_.\-/]+", (L.get("content") or "").lower()))
    primary = [L for L in cands if cmds & _words(L)]
    secondary = [L for L in cands if not (cmds & _words(L))]
    fired = []
    for L in (*primary, *secondary):
        u = L.get("ulid")
        if not u or u in seen:
            continue
        content = L.get("content") or ""
        ov = q & distinctive_tokens(content)
        cmd_hit = bool(cmds & _words(L))  # the rule names the command we're running
        # Fire on: a rule that NAMES this command, OR two distinctive overlapping
        # tokens, OR one identifier-grade one (record_batch, qwen2.5 - is_strong).
        if cmd_hit or len(ov) >= 2 or any(is_strong(t) for t in ov):
            seen.add(u)
            fired.append(L)
        if len(fired) >= 2:
            break
    return fired


def pretool_negatives(engine, excerpt: str, seen: set) -> list:
    """Action-time REPEAT guard: of the "do NOT repeat this" corpus (failures +
    auto-captured correction drafts), the ones that STRONGLY match what the
    agent is about to do - i.e. "you're walking into something you were
    corrected/burned on before". Same strong bar as pretool_lessons (a named
    command, OR >=2 distinctive overlapping tokens, OR one identifier-grade
    one), once per (session, item). This is the close on 'guard fired != agent
    obeyed': it fires at the MOMENT of the action, not on the message."""
    if not excerpt or not excerpt.strip():
        return []
    try:
        import re as _re

        from pmb.core.text_match import (
            distinctive_tokens,
            is_strong,
            shell_command_names,
        )
    except Exception:
        return []
    cmds = shell_command_names(excerpt)
    q = distinctive_tokens(excerpt)
    try:
        cands = engine.find_negative_memories(excerpt, limit=6) or []
    except Exception:
        return []
    fired = []
    for L in cands:
        u = L.get("ulid")
        if not u or u in seen:
            continue
        text = L.get("match_text") or L.get("content") or ""
        ov = q & distinctive_tokens(text)
        words = set(_re.findall(r"[a-z0-9_.\-/]+", text.lower()))
        cmd_hit = bool(cmds & words)
        if cmd_hit or len(ov) >= 2 or any(is_strong(t) for t in ov):
            seen.add(u)
            fired.append(L)
        if len(fired) >= 2:
            break
    return fired


def _workspace_matches(engine, requested) -> bool:
    """S4: the daemon serves exactly ONE workspace (its build cwd). A client
    that names a DIFFERENT workspace must be refused so we never inject
    workspace A's memory into workspace B's hooks. A client that names nothing
    (no PMB_WORKSPACE) is served best-effort, the unchanged single-workspace
    contract."""
    if not requested:
        return True
    req = str(requested).strip().lower()
    ws = engine.workspace
    cands = {str(getattr(ws, "id", "")).lower(), str(getattr(ws, "name", "")).lower()}
    return req in cands


def _register_internal_routes(mcp, engine) -> None:
    """Mount /internal/health + /internal/hook/* + /internal/shutdown on the
    fastmcp ASGI app, backed by the SAME warm engine. Call before http_app()."""
    import anyio
    from starlette.responses import JSONResponse

    @mcp.custom_route("/internal/health", methods=["GET"])
    async def _health(request):  # noqa: ANN001
        return JSONResponse({
            "ok": True,
            "version": pmb.__version__,
            "warm": bool(getattr(engine, "is_warm", lambda: False)()),
            "workspace": engine.workspace.id,
            "workspace_name": getattr(engine.workspace, "name", None),
            "pmb_home": str(_pmb_home()),
            "last_maintenance": _LAST_MAINTENANCE["summary"],  # M1
        })

    @mcp.custom_route("/internal/hook/prepare-context", methods=["POST"])
    async def _prepare(request):  # noqa: ANN001
        _LAST_REQUEST["ts"] = time.time()
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not _workspace_matches(engine, (body or {}).get("workspace")):
            return JSONResponse({"error": "workspace_mismatch",
                                 "version": pmb.__version__}, status_code=409)
        msg = str((body or {}).get("message") or "").strip()
        max_chars = int((body or {}).get("max_chars") or 4000)

        def _work() -> str:
            from pmb.hooks import compute_prepare_context_text
            try:
                return compute_prepare_context_text(engine, msg, max_chars) or ""
            except Exception as e:
                try:
                    from pmb.core.errlog import log_error
                    log_error(engine.workspace.db_path, "daemon_hook", e, "prepare")
                except Exception:
                    pass
                return ""

        text = await anyio.to_thread.run_sync(_work)
        return JSONResponse({
            "context": text,
            "version": pmb.__version__,
            "warm": bool(getattr(engine, "is_warm", lambda: False)()),
            "source": "daemon",
        })

    @mcp.custom_route("/internal/hook/session-restore", methods=["POST"])
    async def _restore(request):  # noqa: ANN001
        _LAST_REQUEST["ts"] = time.time()
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not _workspace_matches(engine, (body or {}).get("workspace")):
            return JSONResponse({"error": "workspace_mismatch",
                                 "version": pmb.__version__}, status_code=409)
        max_chars = int((body or {}).get("max_chars") or 3000)

        def _work() -> str:
            from pmb.hooks import build_session_restore
            try:
                return build_session_restore(
                    engine, minutes=None, include_project=True,
                    max_chars=max_chars) or ""
            except Exception as e:
                try:
                    from pmb.core.errlog import log_error
                    log_error(engine.workspace.db_path, "daemon_hook", e, "restore")
                except Exception:
                    pass
                return ""

        text = await anyio.to_thread.run_sync(_work)
        return JSONResponse({
            "context": text,
            "version": pmb.__version__,
            "source": "daemon",
        })

    @mcp.custom_route("/internal/recall", methods=["POST"])
    async def _recall(request):  # noqa: ANN001
        """Full hybrid recall over the warm engine, for the `pmb recall` CLI so
        a human gets the SAME BM25+vector results an agent does instead of the
        cold BM25-only fallback. Best-effort: the CLI falls back to a local cold
        engine when this is unavailable."""
        _LAST_REQUEST["ts"] = time.time()
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not _workspace_matches(engine, (body or {}).get("workspace")):
            return JSONResponse({"error": "workspace_mismatch",
                                 "version": pmb.__version__}, status_code=409)
        query = str((body or {}).get("query") or "").strip()
        top_k = int((body or {}).get("top_k") or 5)
        if not query:
            return JSONResponse({"results": [], "version": pmb.__version__})

        def _work() -> dict:
            try:
                pack = engine.recall(query=query, top_k=top_k)
                return {
                    "results": [r.to_dict() for r in pack.results],
                    "workspace_name": pack.workspace_name,
                    "n_total_in_workspace": pack.n_total_in_workspace,
                    "elapsed_ms": pack.elapsed_ms,
                }
            except Exception as e:
                try:
                    from pmb.core.errlog import log_error
                    log_error(engine.workspace.db_path, "daemon_hook", e, "recall")
                except Exception:
                    pass
                return {"results": []}

        out = await anyio.to_thread.run_sync(_work)
        out["version"] = pmb.__version__
        out["warm"] = bool(getattr(engine, "is_warm", lambda: False)())
        out["source"] = "daemon"
        return JSONResponse(out)

    @mcp.custom_route("/internal/hook/pretool", methods=["POST"])
    async def _pretool(request):  # noqa: ANN001
        """R11: PreToolUse lesson guard. Fires a matching lesson at TOOL-CALL
        time ("use pnpm, never npm" when the agent is about to run `npm
        install`), even if the agent never called memory. Daemon-served only;
        advisory (never blocks); once per (session, lesson)."""
        _LAST_REQUEST["ts"] = time.time()
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not _workspace_matches(engine, (body or {}).get("workspace")):
            return JSONResponse({"error": "workspace_mismatch",
                                 "version": pmb.__version__}, status_code=409)
        excerpt = str((body or {}).get("excerpt") or "")[:500]
        session = str((body or {}).get("session_id") or "")

        def _work() -> str:
            if not excerpt.strip():
                return ""
            try:
                if not engine.config.get("hooks.pretool_guard"):
                    return ""
            except Exception:
                pass
            try:
                seen = _PRETOOL_SEEN.setdefault(session, set())
                # REPEAT guard first (loud): things the user already corrected /
                # that already failed. This is the "stop before you repeat it"
                # signal - it gets the top slot and the strongest wording.
                negs = pretool_negatives(engine, excerpt, seen)
                lessons = pretool_lessons(engine, excerpt, seen)
                if not negs and not lessons:
                    return ""
                try:
                    if negs:
                        engine._log_lesson_surfaces(
                            negs, query="pretool", source="pretool_guard.repeat")
                    if lessons:
                        engine._log_lesson_surfaces(
                            lessons, query="pretool", source="pretool_guard")
                except Exception:
                    pass
                lines: list[str] = []
                if negs:
                    lines.append(
                        "[pmb] STOP - you were corrected on this before. "
                        "Do NOT repeat it:")
                    for L in negs:
                        txt = (L.get("match_text") or L.get("content") or "")[:240]
                        lines.append(f"  - {txt}")
                if lessons:
                    lines.append("[pmb] Relevant rule(s) before this action:")
                    lines += [f"  ! {(L.get('content') or '')[:240]}" for L in lessons]
                return "\n".join(lines)
            except Exception:
                return ""

        text = await anyio.to_thread.run_sync(_work)
        return JSONResponse({"context": text, "version": pmb.__version__,
                             "source": "daemon"})

    @mcp.custom_route("/internal/shutdown", methods=["POST"])
    async def _shutdown(request):  # noqa: ANN001
        """S3: authenticated shutdown so a client that detects a VERSION
        mismatch can retire the stale daemon and autostart the new build,
        instead of every hook falling cold for up to idle_exit_min."""
        await anyio.to_thread.run_sync(_drain_before_exit, engine)

        def _bye():
            time.sleep(0.2)
            os._exit(0)

        threading.Thread(target=_bye, daemon=True).start()
        return JSONResponse({"ok": True, "version": pmb.__version__})


def _drain_before_exit(engine) -> None:
    """Land everything this process is still holding in memory.

    Queued writes go through the engine outbox, and `record_call` keeps perf
    rows buffered until `_PERF_FLUSH_EVERY` of them accumulate - a daemon that
    served fewer calls than that writes no telemetry at all unless it drains
    on the way out. Never raises: shutdown must not be blocked by either.
    """
    try:
        engine.wait_for_writes(timeout=5.0)
    except Exception:
        pass
    try:
        from pmb.mcp.perf import flush_perf  # S9: don't drop buffered perf
        flush_perf()
    except Exception:
        pass


def _drain_on_lifespan_shutdown(app, engine):
    """Wrap an ASGI app so `_drain_before_exit` runs on lifespan shutdown.

    The daemon cannot drain after `uvicorn.run()` returns, because on a signal
    it never returns: uvicorn captures SIGTERM/SIGINT itself, shuts down
    gracefully, restores the previous handlers and then re-raises the signal
    at the end of `Server.capture_signals()`. The process dies of that signal
    from inside `uvicorn.run`, so a `finally` around it - and `atexit`, which
    Python does not run on SIGTERM either - are both dead code on the path
    that actually stops a daemon.

    The ASGI lifespan shutdown does run first, on the event loop, before that
    re-raise. Wrapping at the protocol level rather than adding a Starlette
    `on_shutdown` handler keeps this working whether or not the app fastmcp
    hands us was built with its own lifespan context.

    Normally the drain runs from the shutdown message, so it lands before the
    server is told shutdown is complete. An app that raises on the way down
    never sends that message - uvicorn catches it in `LifespanOn.main` and
    goes on to re-raise the signal - so the `finally` covers that path too,
    once.
    """
    import anyio

    async def _wrapped(scope, receive, send):
        if scope.get("type") != "lifespan":
            await app(scope, receive, send)
            return

        drained = False

        async def _drain_once():
            nonlocal drained
            if not drained:
                drained = True
                await anyio.to_thread.run_sync(_drain_before_exit, engine)

        async def _send(message):
            if message.get("type") in ("lifespan.shutdown.complete",
                                       "lifespan.shutdown.failed"):
                await _drain_once()
            await send(message)

        try:
            await app(scope, receive, _send)
        finally:
            await _drain_once()

    return _wrapped


def _idle_watcher(idle_exit_min: float, engine) -> None:
    """Exit the process after `idle_exit_min` minutes with no request, so a
    forgotten daemon doesn't hold ~400MB forever. 0 = never exit."""
    if not idle_exit_min or idle_exit_min <= 0:
        return
    limit_s = float(idle_exit_min) * 60.0
    while True:
        time.sleep(min(limit_s / 2.0, 60.0))
        if (time.time() - _LAST_REQUEST["ts"]) >= limit_s:
            _drain_before_exit(engine)
            sys.stderr.write("[pmb-daemon] idle timeout reached - exiting.\n")
            os._exit(0)


def _maintenance_watcher(engine) -> None:
    """M1: run the self-maintenance tick once per `maintenance_interval_h` of
    uptime, only while idle. Daemon thread; never raises into the server."""
    from pmb.maintenance.tick import run_maintenance_tick, should_run_maintenance
    try:
        if not bool(engine.config.get("daemon.maintenance")):
            return
    except Exception:
        return
    last_tick = time.time()   # don't fire immediately on a fresh start
    while True:
        try:
            interval_s = float(engine.config.get("daemon.maintenance_interval_h")) * 3600.0
            idle_min_s = float(engine.config.get("daemon.maintenance_idle_min")) * 60.0
        except Exception:
            interval_s, idle_min_s = 24 * 3600.0, 300.0
        # wake periodically; the predicate gates the actual run
        time.sleep(min(interval_s / 4.0, 300.0))
        now = time.time()
        if not should_run_maintenance(now, last_tick, interval_s,
                                      _LAST_REQUEST["ts"], idle_min_s):
            continue
        try:
            archive = bool(engine.config.get("daemon.maintenance_archive"))
        except Exception:
            archive = True
        try:
            days_since = max(0.0, (now - last_tick) / 86400.0)
            summary = run_maintenance_tick(engine, archive=archive,
                                           decay_days=days_since, now=now)
            _LAST_MAINTENANCE["summary"] = summary
            st = summary.get("steps", {})
            sys.stderr.write(
                f"[pmb-daemon] maintenance: decayed "
                f"{st.get('decay', {}).get('decayed', 0)}, archived "
                f"{st.get('archive_cold', {}).get('archived', 0)}, conflicts "
                f"{st.get('conflicts', {}).get('found', 0)}, declutter-candidates "
                f"{st.get('declutter_dryrun', {}).get('would_archive', 0)}\n"
            )
        except Exception as e:
            sys.stderr.write(f"[pmb-daemon] maintenance tick failed: {e}\n")
        last_tick = now


def _daemon_bearer_middleware(token: str):
    """Bearer middleware that lets /internal/health and CORS preflights through
    (they leak nothing) but requires the token everywhere else."""
    import hmac

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected = f"Bearer {token}"

    class _Mw(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            _LAST_REQUEST["ts"] = time.time()
            if request.method == "OPTIONS" or request.url.path in (
                "/internal/health", "/healthz", "/",
            ):
                return await call_next(request)
            got = request.headers.get("authorization", "")
            if not got or not hmac.compare_digest(got, expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    return _Mw


def run_daemon(
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    idle_exit_min: float | None = None,
) -> int:
    """Foreground daemon runner. Returns a process exit code.

    Refuses to start a second daemon for this PMB_HOME (points at the live one).
    Builds the warm MCP server, mounts internal routes, writes a fresh token,
    recovers the write outbox, registers in the server registry, and serves.
    """
    from pmb.mcp.registry import (
        find_live_daemon,
        register_server,
        unregister_server,
    )
    from pmb.mcp.server import build_server

    existing = find_live_daemon()
    if existing:
        sys.stderr.write(
            f"[pmb-daemon] already running (pid {existing.get('pid')}, "
            f"port {existing.get('port')}). Not starting a second.\n"
        )
        return 0

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "[pmb-daemon] uvicorn is required. Install: pip install 'uvicorn[standard]'\n"
        )
        return 2

    server = build_server(prewarm=True)
    engine = getattr(server, "_pmb_engine", None)
    if engine is None:
        sys.stderr.write("[pmb-daemon] could not access engine from server.\n")
        return 2

    _register_internal_routes(server, engine)

    # Build the ASGI app from fastmcp, then attach bearer auth.
    app = None
    builder = getattr(server, "http_app", None) or getattr(
        server, "streamable_http_app", None)
    if builder is None:
        sys.stderr.write("[pmb-daemon] fastmcp exposes no http_app builder.\n")
        return 2
    try:
        app = builder(path=path)
    except TypeError:
        app = builder()

    token = write_daemon_token()
    try:
        app.add_middleware(_daemon_bearer_middleware(token))
    except Exception as e:
        sys.stderr.write(f"[pmb-daemon] middleware install failed: {e}\n")

    # Replay any writes left pending by a previous (crashed) process.
    try:
        n = engine.recover_outbox()
        if n:
            sys.stderr.write(f"[pmb-daemon] recovering {n} pending write(s).\n")
    except Exception:
        pass

    if idle_exit_min is None:
        try:
            idle_exit_min = float(engine.config.get("daemon.idle_exit_min"))
        except Exception:
            idle_exit_min = 120.0

    entry = None
    try:
        entry = register_server(
            transport="streamable-http", kind="daemon",
            host=host, port=port, path=path,
            workspace=getattr(server, "name", None),
        )
        import atexit
        atexit.register(unregister_server, entry["pid"])
    except Exception:
        pass

    threading.Thread(target=_idle_watcher, args=(idle_exit_min, engine),
                     daemon=True, name="pmb-daemon-idle").start()
    # M1: self-maintenance tick (no-op unless daemon.maintenance is on).
    threading.Thread(target=_maintenance_watcher, args=(engine,),
                     daemon=True, name="pmb-daemon-maintenance").start()

    sys.stderr.write(
        f"[pmb-daemon] warm memory daemon on http://{host}:{port}{path}\n"
        f"  workspace: {engine.workspace.id}  ·  idle-exit: "
        f"{'never' if not idle_exit_min else f'{idle_exit_min:g}min'}\n"
    )
    try:
        # lifespan="on", not the default "auto": in auto mode an app that
        # rejects the protocol is served without a shutdown phase, which would
        # silently take the drain below with it - and the SIGTERM re-raise
        # means the `finally` here would not cover for it. This daemon depends
        # on lifespan, so a missing one must fail loudly at startup.
        uvicorn.run(_drain_on_lifespan_shutdown(app, engine),
                    host=host, port=port, log_level="warning",
                    lifespan="on")
    finally:
        # Fallback only. SIGTERM never reaches this: uvicorn re-raises it and
        # the default disposition kills the process from inside uvicorn.run().
        # SIGINT does - the restored handler turns the re-raise into a
        # KeyboardInterrupt that leaves uvicorn.run() - as does a graceful
        # return. The lifespan wrapper has normally drained already; a second
        # drain is a no-op once the outbox is empty.
        _drain_before_exit(engine)
        if entry is not None:
            try:
                unregister_server(entry["pid"])
            except Exception:
                pass
    return 0
