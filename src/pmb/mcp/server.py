"""
PMB MCP Server.

Tools:
- recall(query, top_k=5)            - search
- remember(query, response, ...)    - add a Q/A pair
- record_fact(fact, ...)            - add a factual statement
- pin(ulid)                         - pin
- forget(ulid)                      - archive
- stats()                           - workspace stats
- list_recent(limit=20, event_type) - most recent events

The Engine is initialised lazily once - workspace detection runs at server
start and the same engine is reused thereafter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# UTF-8 on Windows. Guarded against stdout substitutions (Textual's
# _PrintCapture, pytest capture, etc.) that don't expose `.encoding`.
try:
    enc = getattr(sys.stdout, "encoding", None)
    if enc and enc.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastmcp import FastMCP

from pmb.core.engine import Engine
from pmb.core.workspace import detect_workspace

PMB_SYSTEM_INSTRUCTIONS = """\
PMB is OFF for general questions (theory, syntax, "what is X"). Don't call
it on coding/debugging questions answerable from training.

But when you DO engage - READ BEFORE YOU WRITE. The core failure mode:
agents record lessons, decisions, facts - then on the next task ignore all
of it and start from scratch. PMB exists to break that pattern. If you
write a lesson and never read it, the lesson is wasted.

══════════ READ FIRST - call BEFORE acting ══════════

▶ project_overview(name) - at the START of any work on a known project.
  User says "working on LoadGuard / fix bug in LeanBoard / write code for
  PMB". ONE call returns: lessons (RULES to follow), decisions, open
  goals, recent activity, related entities. Replaces 5+ recall() calls.
  7ms, graph-backed. Try this BEFORE guessing the project structure.

▶ recall(query) - for any question about user/past/project. The response
  now includes a `lessons` field - READ THE LESSONS FIRST and follow
  them. Then use `results` as background.

▶ session_brief - after long sessions when your own context compacted.
  Re-orient on what THIS session decided/built. Don't re-ask the user.

▶ recent_activity / what_just_happened / list_goals - for the obvious
  triggers ("what did I recently", "what did we just do", "what are my goals").

If a lesson surfaces in any of the above - it overrides your default
behaviour. "We use pnpm, never npm" → use pnpm. No discussion.

══════════ WRITE - only on triggers ══════════

▶ record_batch(items=[…]) - exactly ONE call per turn, all items together.

  Types: fact | fact_tree | goal | activity | milestone | lesson.

  Triggers:
    1. "remember / it's important" → importance=0.95, pin=true
    2. User shares a fact ("I'm working on X", "ate pizza yesterday")
       → importance=0.7
    3. You completed substantive work / made a decision
       → {"type":"activity","kind":"completed"/"decision", ...}
    4. User corrects you, OR you discover a reusable project rule
       → {"type":"lesson","content":"This repo uses pnpm, never npm",
          "project":"Acme"}  # omit project only for genuinely global rules
       Lessons are PROCEDURAL ("how to work here"), high-importance.
       They will surface in every future recall - record them so the
       NEXT session of yourself reads them and gets smarter.

══════════ DON'T record ══════════

Memory is for what's NOT trivially re-derivable. Skip:
  - secrets / tokens / API keys (they're redacted anyway - don't rely on it)
  - transient tool output, stack traces, file/dir listings as "facts"
  - restating repo content (code structure, file contents, git history) -
    the agent can just read the repo
  - future intent as a fact → use a goal instead (see trigger table)
Junk is cheap but not free: it dilutes recall. When unsure whether something
is durable signal vs. transient noise, lean toward NOT recording it.

══════════ RULES ══════════

- ONE record_batch per turn (never multiple)
- Use pin:true field, NEVER call pin() separately
- NEVER recall after writing to verify
- ABSOLUTE dates ("On May 25, 2026"), not "today"
- Don't narrate tool calls - no "I'll save that / found in memory /
  according to records / in memory / I recorded"
- Read-tool results are your knowledge, weave naturally into the answer
- Save-content rules apply to MEMORY only. Answer length is not
  restricted - answer with whatever depth the question deserves.

PMB is local-only.
"""


# Kept for reference - the old verbose instructions. Not used.
PMB_SYSTEM_INSTRUCTIONS_OLD = """\
PMB (Personal Memory Brain) is the user's persistent long-term memory.
Treat it as the AUTHORITATIVE source for anything personal or project-specific.

═══════════════════════════════════════════════════════════════════════════
PART 0 - FIRST PRINCIPLE: SAVE GENEROUSLY, BATCH AGGRESSIVELY
═══════════════════════════════════════════════════════════════════════════

The rules below say BATCH writes and be QUIET. They do NOT say save LESS.

  ✅ user states 6 facts → ONE record_batch with 6 items
  ❌ user states 6 facts → "I'll save the important ones" → 2 items
  ❌ user states 6 facts → 6 separate record_fact calls

Junk is cheap. Gaps hurt the user. When in doubt - SAVE IT.

═══════════════════════════════════════════════════════════════════════════
PART 0.5 - EXPLICIT MEMORY TRIGGERS (pin to permanent)
═══════════════════════════════════════════════════════════════════════════

If the user uses any of these phrases - the fact is HIGH PRIORITY. After
saving, immediately call `pin(ulid)` so it never decays:

  • "remember" / "remember this" / "don't forget" / "save this"
  • "remember this" / "remember that" / "don't forget" / "save this"
  • "this is important" / "this is important" / "important:" / "important to me"
  • "for the record" / "for the future" / "write this down"

Pattern: record_fact (or fact_tree) with importance=0.95, then pin.

  User: "Remember - my birthday is March 14"
  → record_fact("User's birthday is March 14", importance=0.95)
  → pin(ulid)

═══════════════════════════════════════════════════════════════════════════
PART 1 - SPEED & STYLE RULES
═══════════════════════════════════════════════════════════════════════════

Each MCP call costs ~3-5 seconds of YOUR thinking time. User notices.

1. **BATCH ALL WRITES.** Call `record_batch` ONCE with all items per turn.
   Never make 5 separate record_* calls - 30s instead of 5s. See PART 2.

2. **ONE RECALL PER QUESTION.** Call `recall` ONCE with a well-chosen query.
   Don't loop. If top results aren't relevant, ONE rephrased follow-up is
   allowed; never more than 2.

3. **USE FACTS AS YOUR OWN KNOWLEDGE.** After recall, weave facts into the
   answer naturally. NEVER prefix with:
       ❌ "memory records that..."
       ❌ "according to the records..."
       ❌ "I found in memory that..."
       ❌ "Looking at my records..."
   Just answer as if you simply know:
       ✅ "Meeting with Max tomorrow at the café in Podil, discussing the Rust startup."
       ✅ "Postgres 17 on port 5433."

4. **NO PROCESS NARRATION.** Don't tell the user you're saving things,
   what tools you're calling, or how many records you stored. Just save
   silently and answer. The dashboard shows what was stored.

═══════════════════════════════════════════════════════════════════════════
PART 1 - WHEN TO `recall` (READ memory)
═══════════════════════════════════════════════════════════════════════════

ALWAYS call `recall` BEFORE answering, when the user asks about:
  • Past events:    "when did I...", "what did I do...", "when was the last time..."
  • Personal data:  "what's my...", "what's my...", "where do I live"
  • Decisions:      "why did we choose X?", "why did we choose..."
  • People:         "who is X?", "who is...", "what did X say?"
  • Project state:  "what port?", "what's the config?", "where's X?"
  • Health/life:    "when was I sick?", "what was my appointment?"

For "what did we just do?" / "what were we just discussing?" - call
`what_just_happened(5)` or `recent_activity(minutes=60)` instead. Those
are instant (no vector search).

Trust results with score > 0.2 - that's the user's recorded reality, more
authoritative than your inferences from code / docker / env / web.

═══════════════════════════════════════════════════════════════════════════
PART 2 - WHEN TO WRITE memory  ← ALWAYS USE `record_batch`
═══════════════════════════════════════════════════════════════════════════

When user mentions multiple things worth saving, extract ALL of them in ONE
pass and call `record_batch` ONCE. Each item has a `type` discriminator:

  fact      → atomic single-statement fact
  fact_tree → main event + N linked subfacts (advice, time, details)
  goal      → a goal/intention with status (pending/in_progress/done) + due_at
  activity  → working-memory log entry (lighter, 3-day decay)
  milestone → checkpoint in a named state-chain (e.g. "layers: 6 → 7 → 11")

WHAT to extract from a typical user turn:

  PERSONAL EVENTS, HEALTH:                  → fact or fact_tree (importance 0.7-0.9)
  GOALS / PLANS / INTENTIONS with deadline: → goal (status="in_progress", due_at=…)
  ACTIONS the user just did:                → activity (kind="completed" / "edit")
  DECISIONS the user made:                  → fact (importance 0.7-0.8)
  RELATIONSHIPS:                             → fact ("User's wife is Anna")
  PROGRESS on a tracked metric:             → milestone (chain_name="…")

Example - user says one long paragraph:

  "Today fixed JWT bug (3h). Want v1.0 by June. Tomorrow meeting Max
   (ex-Grammarly) at Podol café - Rust startup. Peanut allergy worsened,
   doctor said carry EpiPen. Dropped LanceDB for SQLite-only. Finished
   async chapter in Rust book, 4 chapters left."

→ ONE call:

  record_batch(items=[
    {"type":"activity","content":"Fixed JWT 24h validation bug, 3h",
     "kind":"edit"},
    {"type":"goal","title":"Ship PMB v1.0 by end of June 2026",
     "status":"in_progress","due_at":<epoch for 2026-06-30>},
    {"type":"fact_tree",
     "main":"Meeting Max on May 25 2026 at café on Podol",
     "subfacts":["Max - ex-colleague from Grammarly",
                 "Topic: discussing Rust startup idea"],
     "importance":0.7},
    {"type":"fact_tree",
     "main":"User's peanut allergy worsened May 24 2026",
     "subfacts":["Doctor advised: carry EpiPen always",
                 "Check EpiPen expiry every 6 months"],
     "importance":0.9},
    {"type":"fact","content":"PMB dropped LanceDB, SQLite-only "
     "(LanceDB pulled ~200MB deps)","importance":0.8},
    {"type":"milestone","chain_name":"rust_book_progress",
     "title":"Finished async chapter, 4 chapters left",
     "state":{"chapters_left":4,"last_finished":"async"}},
  ])

WRITING RULES:
  • Use ABSOLUTE dates ("On May 24, 2026", never "today") - derived from
    the session date in the instructions header.
  • One atomic fact per item.
  • Use the user's primary language for the body when possible - multilingual
    embedding bridges RU↔EN automatically.
  • importance: 0.9 health/medical, 0.7 events/plans, 0.5 opinions.
  • Call `record_batch` DURING the turn, before you answer - not "at the end".

═══════════════════════════════════════════════════════════════════════════
PART 3 - AI-AGENT MODE (when YOU are doing the work)
═══════════════════════════════════════════════════════════════════════════

When YOU act on the user's behalf (writing code, refactoring, debugging,
deploying), YOUR actions also deserve memory. Don't only save what the user
said - save what YOU did.

  • Design decision you made    → activity(kind="decision", content="why X over Y")
  • Files you edited            → activity(kind="edit")
  • Tools you ran (tests, build, lint) → activity(kind="tool_call")
  • Step you finished           → activity(kind="completed") + maybe milestone
  • Multi-step plan you started → goal(status="in_progress")
  • Bug + fix + lesson learned  → fact_tree(main=bug, subfacts=[cause, fix, lesson])
  • High-level user instruction → fact(importance=0.9) + pin
  • Tracked metric changed (test count, build size, layer count) → milestone

Example. You fixed an auth bug:
  record_batch(items=[
    {"type": "activity", "kind": "edit",
     "content": "Extracted JWT validator into a separate module"},
    {"type": "fact_tree",
     "main": "JWT validation bug fixed on May 24 2026",
     "subfacts": ["Root cause: tokens older than 24h skipped exp check",
                  "Fix: added explicit exp comparison with 60s leeway",
                  "Lesson: always test with already-expired tokens"],
     "importance": 0.8},
    {"type": "activity", "kind": "completed",
     "content": "All 211 auth tests passing after refactor"},
  ])

Future "when did we fix auth?" / "why did you choose X?" instantly answered
without re-reading code.

═══════════════════════════════════════════════════════════════════════════
PART 4 - OTHER TOOLS (use when relevant)
═══════════════════════════════════════════════════════════════════════════

  recall_smart       - for important queries (escalates on low confidence)
  what_just_happened - instant: last N events of current session
  recent_activity    - instant: last X minutes of activity log
  list_goals         - open goals (status="in_progress")
  chain_history      - full evolution of a tracked metric
  pin                - pin a memory (use after "remember"/"save this" triggers)
  dedupe_sweep       - one-shot dedup of duplicate facts
  workspace_info     - confirm which memory you're using

The single-item record_fact / record_goal / record_milestone tools still
exist for one-off cases, but for any multi-fact message PREFER record_batch.

═══════════════════════════════════════════════════════════════════════════
ARCHITECTURE NOTE - why writes stay fast
═══════════════════════════════════════════════════════════════════════════

Write-path (record_*) does NO LLM call - just embedding + SQLite insert
(~50ms). Deep semantic ops - atomic fact extraction, reflections, narrative
arcs, LLM-verify dedup - are SLEEP-MODE: run via separate commands
(`pmb reflect`, `pmb consolidate`, `pmb dedupe --run-pending`) when user is
idle. You don't trigger them. This keeps every turn fast.
"""


from pmb.mcp._toolspec import (  # tool-profile gating machinery
    _DEFAULT_TOOLS,
    _LEAN_TOOLS,
    _MINIMAL_TOOLS,
    _TOOL_PROFILE,
)


def build_server(
    cwd: Path | None = None,
    workspace_id: str | None = None,
    name: str = "pmb",
    prewarm: bool = True,
) -> FastMCP:
    """Build the MCP server with a bound engine.

    prewarm=True: do a dummy embed during startup so sentence-transformers
    loads BEFORE the first tool call. Without this the first `recall` can
    take 30+s (model cold load) and Codex/Claude time out → Transport closed.
    """
    workspace = detect_workspace(cwd=cwd, explicit_id=workspace_id)
    engine = Engine(workspace=workspace)

    if prewarm:
        # Async prewarm with readiness state. The MCP transport (initialize
        # handshake) responds INSTANTLY because nothing blocks server boot.
        # Tools that need the heavy model (recall) check engine.is_warm()
        # at call time. If warming, they return BM25-only results with a
        # `warming_up: true` flag instead of timing out the client.
        #
        # This is the production-friendly version: a Codex/Claude client
        # with strict startup timeouts gets a snappy server, and the user
        # gets degraded-but-fast first results that the agent can show
        # while waiting for the full pipeline to be ready.
        import threading

        # Stage 1 - critical path (model + LanceDB import). Runs in BG.
        def _stage1_async():
            try:
                vec = engine.search.embed(
                    "warmup query for prewarm pipeline"
                )
            except Exception:
                return
            try:
                _ = (
                    engine.search._table.search(vec.tolist())
                    .metric("cosine")
                    .limit(3)
                    .to_list()
                )
            except Exception:
                pass
            try:
                engine.search.search(
                    "warmup hybrid search query", top_k=3,
                    importance_map={}, timestamp_map={},
                )
            except Exception:
                pass
            # Stage 2 - batch embed + reranker JIT
            try:
                engine.search.embed_batch([
                    "warmup batch item one",
                    "warmup batch item two",
                ])
            except Exception:
                pass
            try:
                if engine.config.get("recall.rerank"):
                    rr = engine.search.reranker
                    if rr is not None:
                        rr.predict([
                            ("query", "doc one"),
                            ("query", "doc two"),
                        ])
            except Exception:
                pass
            # Mark engine fully warm so tools stop returning the partial flag
            engine._is_warm = True
            engine._warmed_at = __import__("time").time()

        threading.Thread(
            target=_stage1_async, daemon=True, name="pmb-prewarm",
        ).start()

    # Personalize instructions with current date so the LLM knows what "today"
    # resolves to when storing facts with relative dates.
    import datetime as _dt
    today = _dt.datetime.now().strftime("%B %d, %Y")
    instructions = (
        f"Current session date: {today}.\n"
        f"When user says 'today', 'yesterday', 'last week' - resolve to absolute "
        f"dates relative to {today} when storing facts.\n\n"
        + PMB_SYSTEM_INSTRUCTIONS
    )

    mcp = FastMCP(name, instructions=instructions)

    # Improvement JJ: wrap mcp.tool() so every registered function is
    # automatically timed and logged to `mcp_calls` SQLite table. Adds
    # ~1ms per call; dashboard reads aggregates for the Performance tab.
    from pmb.mcp.perf import make_timing_wrapper as _make_timing
    _timing_decorator = _make_timing(
        db_path=engine.workspace.db_path,
        workspace_id=engine.workspace.id,
    )
    _original_tool = mcp.tool
    from pmb.mcp._toolspec import short_description as _short_desc

    def _timed_tool(*t_args, **t_kwargs):
        def _inner(fn):
            # D1: serve a compact description (non-"full" profiles) instead of
            # the long docstring, unless the call already set one explicitly.
            kwargs = dict(t_kwargs)
            if "description" not in kwargs:
                sd = _short_desc(getattr(fn, "__name__", ""))
                if sd:
                    kwargs["description"] = sd
            original = _original_tool(*t_args, **kwargs)
            return original(_timing_decorator(fn))
        return _inner

    mcp.tool = _timed_tool

    from pmb.mcp.tools import register_all
    register_all(mcp, engine)

    # Improvement Z: post-registration tool-profile filter.
    # All 55 tools registered above; now drop the ones not in active profile.
    # This reduces what Codex sees in the tool list - fewer descriptions to
    # parse each turn = faster + sharper LLM responses.
    if _TOOL_PROFILE != "full":
        # Resolve the active profile's tool set. NOTE: this must mirror
        # `_should_register` - a missing 'lean' key here is what silently
        # demoted the lean profile to the full default set.
        allowed = {
            "minimal": _MINIMAL_TOOLS,
            "lean": _LEAN_TOOLS,
        }.get(_TOOL_PROFILE, _DEFAULT_TOOLS)
        try:
            import asyncio
            # list_tools() is async. The stdio server path builds the server
            # before any event loop exists, so asyncio.run() works there and
            # gating applies. If we're already inside a running loop (in-memory
            # client / embedded host), asyncio.run() would raise and leave an
            # un-awaited coroutine - skip cleanly instead.
            try:
                asyncio.get_running_loop()
                in_loop = True
            except RuntimeError:
                in_loop = False
            if not in_loop:
                current = asyncio.run(mcp.list_tools())
                current_names = {t.name for t in current}
                remover = (
                    mcp.local_provider.remove_tool
                    if hasattr(mcp, "local_provider") and hasattr(mcp.local_provider, "remove_tool")
                    else mcp.remove_tool
                )
                for tname in current_names:
                    if tname not in allowed:
                        try:
                            remover(tname)
                        except Exception:
                            pass
        except Exception:
            pass  # filter best-effort; if FastMCP API changes, all tools stay

    # Expose the engine on the server so the daemon can mount internal hook
    # routes against the SAME warm engine (additive; stdio/http ignore it).
    try:
        mcp._pmb_engine = engine
    except Exception:
        pass

    return mcp


def _build_bearer_middleware(token: str):
    """Build a Starlette ASGI middleware that requires
    `Authorization: Bearer <token>` on every request except CORS
    preflights and the health endpoint.

    Returns None when token is empty. The wrapper does a constant-time
    compare so a leaked log line can't side-channel a partial match.
    """
    if not token:
        return None
    import hmac

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected = f"Bearer {token}"

    class _BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # CORS preflight + health probe: pass through.
            if request.method == "OPTIONS" or request.url.path in ("/healthz", "/"):
                return await call_next(request)
            got = request.headers.get("authorization", "")
            if not got or not hmac.compare_digest(got, expected):
                return JSONResponse(
                    {"error": "unauthorized",
                     "hint": "send `Authorization: Bearer <PMB_MCP_BEARER_TOKEN>`"},
                    status_code=401,
                )
            return await call_next(request)

    return _BearerAuthMiddleware


def _isolate_stdio_stdin() -> None:
    """Move the stdio transport off fd 0 (Windows).

    The MCP stdio transport keeps a synchronous read pending on the stdin
    pipe. While it is pending, Windows makes other operations on that pipe
    wait for the client's next message: a seek on fd 0 (native runtimes do
    this while loading extension modules) and the startup of any child
    process that inherits stdin (``git``). The client sends nothing while it
    waits for a tool result, so such a call hung until the client timed out.

    Reading from a private, non-inheritable duplicate and pointing fd 0 at
    the null device (the CRT then also points ``STD_INPUT_HANDLE`` at it)
    leaves the transport as the pipe's only user. Acts only while
    ``sys.stdin`` is still fd 0, so a stdin that was already moved is kept.
    """
    if os.name != "nt":
        return
    try:
        if sys.stdin is None or sys.stdin.fileno() != 0:
            return
        private = os.dup(0)
    except (AttributeError, OSError, ValueError):
        return
    try:
        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(devnull, 0)
        finally:
            os.close(devnull)
    except OSError:
        os.close(private)
        return
    sys.stdin = open(private, encoding="utf-8", errors="replace")


def main():
    """Entry point for `pmb-mcp` script.

    Defaults to stdio (per-developer). Set env-vars to expose HTTP for
    team-shared deployments:

      PMB_MCP_TRANSPORT=streamable-http     # or `stdio` (default)
      PMB_MCP_HOST=0.0.0.0                  # 127.0.0.1 by default
      PMB_MCP_PORT=8765
      PMB_MCP_PATH=/mcp                     # mount path
      PMB_MCP_BEARER_TOKEN=<secret>         # optional shared secret

    On stdio, the agent's host spawns `pmb-mcp` per session. On HTTP, run
    one persistent process (systemd, Docker, etc.) and point every IDE at
    its URL - they share one workspace, one entity graph, one memory.
    """
    import sys
    workspace_id = os.environ.get("PMB_WORKSPACE")
    cwd_env = os.environ.get("PMB_CWD")
    cwd = Path(cwd_env) if cwd_env else None

    transport = (os.environ.get("PMB_MCP_TRANSPORT") or "stdio").strip().lower()
    if transport in ("http", "https"):
        transport = "streamable-http"

    if transport not in ("stdio", "streamable-http"):
        sys.stderr.write(
            f"[pmb-mcp] unknown PMB_MCP_TRANSPORT={transport!r}. "
            f"Use 'stdio' or 'streamable-http'.\n"
        )
        sys.exit(2)

    if transport == "stdio":
        # Before build_server(): its prewarm thread loads native extensions.
        _isolate_stdio_stdin()

    host = os.environ.get("PMB_MCP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PMB_MCP_PORT", "8765"))
    except ValueError:
        sys.stderr.write("[pmb-mcp] PMB_MCP_PORT must be an integer\n")
        sys.exit(2)
    path = os.environ.get("PMB_MCP_PATH", "/mcp")
    token = os.environ.get("PMB_MCP_BEARER_TOKEN", "").strip()

    # Issue #6 - HTTP singleton: if a healthy PMB server already serves this
    # host:port, don't spawn a SECOND heavy process (model + LanceDB). Point
    # clients at the existing one instead.
    if transport == "streamable-http":
        try:
            from pmb.mcp.registry import find_live_http
            existing = find_live_http(host, port)
        except Exception:
            existing = None
        if existing:
            sys.stderr.write(
                f"[pmb-mcp] already running on http://{host}:{port} "
                f"(pid {existing.get('pid')}). Not starting a second - point "
                f"clients at the existing URL, or stop it (`pmb mcp status`).\n"
            )
            return

    server = build_server(cwd=cwd, workspace_id=workspace_id)

    # Register this process so `pmb mcp status` can see it (best-effort).
    try:
        import atexit

        from pmb.mcp.registry import register_server, unregister_server
        _entry = register_server(
            transport=transport,
            host=host if transport == "streamable-http" else None,
            port=port if transport == "streamable-http" else None,
            path=path if transport == "streamable-http" else None,
            workspace=getattr(server, "name", None) or workspace_id,
        )
        atexit.register(unregister_server, _entry["pid"])
    except Exception:
        pass

    # Perf rows are buffered and written in batches of _PERF_FLUSH_EVERY;
    # without a flush on the way out, a server handling fewer calls than that
    # records nothing, and every server loses its final partial batch.
    try:
        from pmb.mcp.perf import install_shutdown_flush
        install_shutdown_flush()
    except Exception:
        pass  # telemetry must never break MCP

    if transport == "stdio":
        server.run()
        return

    auth_status = "bearer-token enabled" if token else "UNAUTHENTICATED (network ACL only)"
    sys.stderr.write(
        f"[pmb-mcp] streamable-http on http://{host}:{port}{path}  ·  {auth_status}\n"
        f"  workspace: {server.name}\n"
    )

    # Build the Starlette ASGI app from the fastmcp server, then attach
    # our middleware before handing off to uvicorn. fastmcp's own
    # server.run(transport=...) calls the same thing internally, but
    # bypasses our chance to inject middleware - so we do it ourselves.
    auth_mw = _build_bearer_middleware(token)
    app = None
    last_err: Exception | None = None
    for builder_name in ("http_app", "streamable_http_app"):
        builder = getattr(server, builder_name, None)
        if builder is None:
            continue
        try:
            app = builder(path=path)
            break
        except TypeError:
            try:
                app = builder()
                break
            except Exception as e:
                last_err = e
        except Exception as e:
            last_err = e

    if app is None:
        sys.stderr.write(
            f"[pmb-mcp] fastmcp version doesn't expose http_app/streamable_http_app "
            f"({last_err}). Falling back to server.run() - bearer auth WILL NOT "
            f"work; set PMB_MCP_BEARER_TOKEN='' or upgrade fastmcp.\n"
        )
        server.run(transport="streamable-http", host=host, port=port, path=path)
        return

    if auth_mw is not None:
        try:
            app.add_middleware(auth_mw)
        except Exception as e:
            sys.stderr.write(
                f"[pmb-mcp] middleware install failed: {e} - server will run UNAUTHENTICATED\n"
            )

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "[pmb-mcp] uvicorn is required for streamable-http. Install with: "
            "pip install 'uvicorn[standard]'\n"
        )
        sys.exit(2)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
