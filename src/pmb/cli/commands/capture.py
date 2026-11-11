"""`pmb` capture root commands - extracted from cli/main.py (no behavior change).

cli/main.py imports this module so these @app.command registrations run."""

from __future__ import annotations

import time
from pathlib import Path

import typer
from rich.markup import escape as esc
from rich.panel import Panel
from rich.table import Table

from pmb.cli._common import (  # noqa: F401
    _agent_toggles_from_config,
    _apply_ttl,
    _humanize_time,
    _open_config,
    _parse_duration,
    app,
    console,
    loading,
)
from pmb.core.engine import Engine
from pmb.core.workspace import detect_workspace

_DASHBOARD_DEFAULT_PORT = 8765


def _print_dashboard_bind_error(host: str, port: int, exc: OSError) -> None:
    console.print(
        f"[red]Dashboard could not bind[/] [cyan]http://{host}:{port}[/]\n"
        f"[dim]{type(exc).__name__}: {exc}[/]\n\n"
        "That port may already be used by the PMB daemon / MCP server, or it "
        "may be blocked by the OS.\n"
        "Try: [cyan]pmb dashboard --port 18888[/]\n"
        "Check: [cyan]pmb daemon status[/]"
    )


@app.command()
def dashboard(
    port: int | None = typer.Option(
        None, "--port", "-p",
        help="Port to bind to (default 8765; picks a free port if the default is busy).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (default localhost)"),
    export: str = typer.Option(
        None, "--export", "-e",
        help="Don't serve - write a self-contained HTML file (open offline / share)."),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the dashboard (or export) in your browser."),
):
    """Launch the local web dashboard - an interactive map of your memory.

    Serves http://127.0.0.1:8765 with the memory graph, a git-style timeline of
    what was captured and when, workspace stats, recall debugger, and a
    per-event inspector. Bound to localhost; pure stdlib, no external deps.

    `--export memory.html` writes a single self-contained file instead of
    serving - useful to open offline or share.
    """
    eng = Engine()
    if export:
        from pmb.dashboard.viz import build_memory_html
        html = build_memory_html(eng)
        p = Path(export)
        p.write_text(html, encoding="utf-8")
        try:
            s = eng.graph_stats()
            console.print(
                f"[green]done[/] wrote [bold]{p}[/] - "
                f"{s['n_entities']} entities, {s['n_edges']} connections")
        except Exception:
            console.print(f"[green]done[/] wrote [bold]{p}[/]")
        if open_browser:
            import webbrowser
            webbrowser.open(p.resolve().as_uri())
        return
    try:
        from pmb.dashboard.server import run_dashboard
    except Exception as e:
        console.print(f"[red]Failed to import dashboard:[/] {e}")
        return
    bind_port = port if port is not None else _DASHBOARD_DEFAULT_PORT
    try:
        run_dashboard(
            eng, host=host, port=bind_port, fallback_port=port is None,
            open_browser=open_browser,
        )
    except OSError as e:
        _print_dashboard_bind_error(host, bind_port, e)
        raise typer.Exit(2)


@app.command()
def init(
    name: str | None = typer.Option(None, "--name", help="Custom workspace name"),
):
    """Initialize a workspace in the current directory."""
    ws = detect_workspace()
    if name:
        ws.name = name
    ws.ensure_dirs()
    ws.save_meta()

    # Also drop a .pmb/workspace.yaml into the project (optional)
    local_config = Path.cwd() / ".pmb"
    local_config.mkdir(exist_ok=True)
    config_file = local_config / "workspace.yaml"
    if not config_file.exists():
        config_file.write_text(
            f"id: {ws.id}\nname: {ws.name}\n", encoding="utf-8"
        )

    console.print(Panel.fit(
        f"[bold green]Workspace initialized[/]\n\n"
        f"  ID:     [cyan]{ws.id}[/]\n"
        f"  Name:   [cyan]{ws.name}[/]\n"
        f"  Root:   {ws.root}\n"
        f"  Source: {ws.source}\n"
        f"  Storage: {ws.storage_dir}\n",
        title="PMB",
    ))


@app.command()
def warmup():
    """P1-1: Eagerly load model + BM25 + LanceDB so the next recall is fast.

    Without this the first query pays ~1-2s cold-start cost. Useful when
    integrating PMB into a latency-sensitive flow (voice assistant) - call
    `pmb warmup` at startup and the actual user-facing query is hot.
    """
    eng = Engine()
    with loading("waking the memory engine - model + BM25 + LanceDB…"):
        result = eng.warmup(with_first_query=True)
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Stage")
    table.add_column("ms")
    table.add_row("Model load",       f"{result['model_load_ms']:.1f}")
    table.add_row("BM25 load",        f"{result['bm25_load_ms']:.1f}")
    table.add_row("LanceDB open",     f"{result['lance_open_ms']:.1f}")
    table.add_row("First probe query", f"{result['first_query_ms']:.1f}")
    table.add_row("[bold]Total[/]",   f"[bold]{result['total_ms']:.1f}[/]")
    console.print(table)
    # warmup only warms THIS process. For hooks, the persistent daemon
    # (`pmb daemon start`, shipped in 0.6.0) is what keeps auto-recall warm.
    console.print(
        "[green]Engine warm for THIS process.[/] CLI recalls in this shell are "
        "now fast.\n[dim]For warm hook-based auto-recall, run "
        "[/][cyan]pmb daemon start[/][dim] - one persistent process serves the "
        "hooks so they get real semantic recall, not the per-process cold "
        "skip.[/]"
    )
    # D6: a slow cold load on a memory/CPU-constrained box → suggest fastembed
    # (ONNX), a lower-RAM, faster-cold-start backend for the SAME multilingual
    # model family. Don't auto-switch (mixing embedders needs a reindex).
    try:
        backend = eng.config.get("embedding.backend") or "sentence-transformers"
        if result["model_load_ms"] > 10000 and backend == "sentence-transformers":
            console.print(
                "[yellow]Model cold-load was slow (>10s).[/] Consider the "
                "lower-RAM, faster-starting [cyan]fastembed[/] backend:\n"
                "  [dim]pmb config set embedding.backend fastembed  &&  pmb reindex[/]\n"
                "  [dim](reindex is required - never mix embedders in one index.)[/]"
            )
    except Exception:
        pass


@app.command()
def stats():
    """Statistics for the current workspace."""
    eng = Engine()
    s = eng.stats()
    ws = s["workspace"]
    ev = s["events"]

    console.print(Panel.fit(
        f"[bold]Workspace[/]: [cyan]{ws['name']}[/] ({ws['id']})\n"
        f"  root: {ws['root']}\n"
        f"  source: {ws['source']}\n"
        f"  created: {ws['created_at']}\n",
        title="PMB Stats",
    ))

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Total events", str(ev["total"]))
    table.add_row("Active", f"[green]{ev['active']}[/]")
    table.add_row("Archived", f"[dim]{ev['archived']}[/]")
    table.add_row("Search index size", str(s["search_index_size"]))
    table.add_row("Oldest", _humanize_time(ev["oldest_timestamp"]))
    table.add_row("Newest", _humanize_time(ev["newest_timestamp"]))
    console.print(table)

    if ev["by_type"]:
        type_table = Table(show_header=True, header_style="bold magenta", title="Events by type")
        type_table.add_column("Type")
        type_table.add_column("Count")
        for t, n in sorted(ev["by_type"].items(), key=lambda x: -x[1]):
            type_table.add_row(t, str(n))
        console.print(type_table)


@app.command(name="list")
def list_cmd(
    limit: int = typer.Option(20, "-n", "--limit"),
    event_type: str | None = typer.Option(None, "--type"),
):
    """Recent events in the current workspace."""
    eng = Engine()
    events = eng.events.list_active(eng.workspace.id, limit=limit, event_type=event_type)

    if not events:
        console.print("[yellow]No events yet. Use `pmb remember` or recall to start filling memory.[/]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Time", style="dim")
    table.add_column("Type", style="cyan")
    table.add_column("Importance", justify="right")
    table.add_column("Access", justify="right")
    table.add_column("Content")
    for e in events:
        content = e.content[:80] + "..." if len(e.content) > 80 else e.content
        table.add_row(
            _humanize_time(e.timestamp),
            e.event_type,
            f"{e.importance:.2f}",
            str(e.access_count),
            content,
        )
    console.print(table)


@app.command()
def remember(
    query: str = typer.Argument(...),
    response: str = typer.Argument(...),
    importance: float = typer.Option(0.5, "--importance", "-i"),
):
    """Manually add a Q/A pair to memory."""
    with loading("saving to memory (loading embedding model on first run)…"):
        eng = Engine()
        ulid = eng.remember(query=query, response=response, importance=importance)
    console.print(f"[green]Stored[/] ULID: [cyan]{ulid}[/]")


@app.command()
def fact(
    text: str = typer.Argument(..., help="The factual statement to record"),
    importance: float = typer.Option(0.7, "--importance", "-i"),
    ttl: str | None = typer.Option(
        None, "--ttl",
        help="Auto-expire after a duration (30d, 12h, 2w, 3mo). Archived later "
             "by `pmb prune-expired`; never affects recall.",
    ),
):
    """Record a standalone fact (project decision, preference, setting)."""
    eng = Engine()
    ulid = eng.record_fact(fact=text, importance=importance)
    if ttl:
        _apply_ttl(eng, ulid, ttl)
    console.print(f"[green]Fact stored[/] ULID: [cyan]{ulid}[/]")


@app.command()
def note(
    text: str = typer.Argument(..., help="The note to remember, in quotes"),
    importance: float = typer.Option(0.6, "--importance", "-i"),
    pin: bool = typer.Option(False, "--pin", help="Pin it (max importance, never auto-archived)"),
    ttl: str | None = typer.Option(
        None, "--ttl",
        help="Auto-expire after a duration (30d, 12h, 2w, 3mo). Archived later "
             "by `pmb prune-expired`; never affects recall.",
    ),
):
    """Instant capture - jot a memory straight from the terminal, no agent.

    The lowest-friction way to feed PMB:
      pmb note "decided to use Postgres for JSONB"
      pmb note "Anna's birthday is March 3" --pin
      pmb note "spike: try Redis cache" --ttl 14d
    """
    eng = Engine()
    ulid = eng.record_fact(
        fact=text,
        importance=0.95 if pin else importance,
        metadata={"source": "cli-note"},
    )
    if pin:
        try:
            eng.pin(ulid)
        except Exception:
            pass
    if ttl:
        _apply_ttl(eng, ulid, ttl)
    console.print(f"[green]Noted[/] [cyan]{ulid}[/]" + (" [yellow](pinned)[/]" if pin else ""))


@app.command()
def learn(
    lesson: str = typer.Argument(..., help="A reusable lesson, in quotes"),
    importance: float = typer.Option(0.85, "--importance", "-i"),
    failed: bool = typer.Option(
        False, "--failed",
        help="Record a FAILURE (negative memory): 'tried X, it didn't work'. "
             "Surfaces with a warning so you/the agent don't repeat it.",
    ),
    ttl: str | None = typer.Option(
        None, "--ttl",
        help="Auto-expire after a duration (e.g. 90d). Rarely needed for "
             "lessons; useful for time-bound rules.",
    ),
):
    """Teach PMB a durable LESSON - a correction or technique to apply going
    forward, not just a one-off fact.

    Where `note` records what happened, `learn` records how to work better:
      pmb learn "this repo uses pnpm, never npm"
      pmb learn "always run `make fmt` before committing"
      pmb learn --failed "tried bumping numpy to 2.x - broke lancedb, stay on 1.x"

    Lessons/failures are stored at high importance (0.85) and tagged so
    `pmb lessons`, `pmb audit`, and recall treat them specially. Unlike a
    keyword-only store, they are retrieved by PMB's hybrid + predicate-aware
    ranker - the right one surfaces, not a lookalike.
    """
    eng = Engine()
    kind = "failure" if failed else "lesson"
    ulid = eng.record_fact(
        fact=lesson,
        importance=importance,
        metadata={"source": "lesson", "kind": kind},
    )
    if ttl:
        _apply_ttl(eng, ulid, ttl)
    word = "Recorded failure" if failed else "Learned"
    console.print(f"[green]{word}[/] [cyan]{ulid}[/] [dim]({kind}, importance {importance})[/]")
    console.print("[dim]Tip: `pmb lessons` to review · `pmb consolidate` to distill "
                  "lessons from recent sessions via LLM.[/]")


@app.command()
def distill(
    session: str | None = typer.Option(
        None, "--session",
        help="Session id to distill (default: most recent session's events).",
    ),
    backend: str = typer.Option("auto", "--backend",
                                help="auto | claude | anthropic | openai | ollama"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview, don't store"),
):
    """Auto-distill durable LESSONS & FAILURES from a session via an LLM.

    Reads what the agent did/decided/was-corrected-on in a session and extracts
    reusable rules ("use pnpm, never npm") and failures ("numpy 2.x broke
    lancedb") - so the agent works better next time. Zero-command version:
    `pmb config set lessons.auto_distill_on_session_end true` runs this
    automatically on `pmb session end`.

    Needs an LLM backend (claude CLI / Anthropic key / OpenAI key / Ollama). Off the recall
    path - cannot affect recall quality or speed.
    """
    eng = Engine()
    with loading("distilling lessons from the session (LLM)…"):
        res = eng.distill_lessons(session_id=session, backend=backend, dry_run=dry_run)
    if res.get("skipped") == "no_llm":
        console.print("[yellow]No LLM backend available.[/] Install Claude CLI / "
                      "Ollama, or set ANTHROPIC_API_KEY or OPENAI_API_KEY.")
        if res.get("detail"):
            console.print(f"[dim]{res['detail']}[/]")
        return
    if res.get("skipped") == "no_events":
        console.print("[yellow]No session events to distill.[/]")
        return
    cands = res.get("candidates") or []
    if not cands:
        console.print("[dim]No durable lessons found in this session.[/]")
        return
    for c in cands:
        mark = "[red]⚠[/]" if c["type"] == "failure" else "[magenta]★[/]"
        console.print(f"  {mark} {c['content']}")
    if dry_run:
        console.print(f"[dim](dry-run) {len(cands)} candidate(s), nothing stored.[/]")
    else:
        console.print(f"[green]Distilled[/] {res.get('n_recorded', 0)} new "
                      f"lesson(s)/failure(s) into memory.")


@app.command()
def lessons(
    limit: int = typer.Option(50, "-n", "--limit"),
):
    """List the durable lessons PMB has learned (procedural memory).

    These are the corrections and techniques that make the agent work better
    over time - the 'don't repeat this mistake' layer.
    """
    eng = Engine()
    events = eng.events.list_active(eng.workspace.id, limit=2000)
    lessons = [
        e for e in events
        if (e.metadata or {}).get("kind") == "lesson"
        or (e.metadata or {}).get("source") == "lesson"
    ]
    if not lessons:
        console.print(
            "[yellow]No lessons yet.[/] Teach one: "
            "[cyan]pmb learn \"this repo uses pnpm, never npm\"[/]\n"
            "[dim]Or distill from recent work: `pmb consolidate`.[/]"
        )
        return
    lessons.sort(key=lambda e: (-e.importance, -e.timestamp))
    t = Table(show_header=True, header_style="bold magenta",
              title=f"Lessons & failures ({len(lessons)})")
    t.add_column("", width=2); t.add_column("Lesson / failure")
    t.add_column("When", style="dim")
    for e in lessons[:limit]:
        is_fail = (e.metadata or {}).get("kind") == "failure"
        mark = "[red]⚠[/]" if is_fail else "[magenta]★[/]"
        content = e.content[:90] + ("…" if len(e.content) > 90 else "")
        t.add_row(mark, content, _humanize_time(e.timestamp))
    console.print(t)


@app.command()
def audit(
    limit: int = typer.Option(2000, "-n", "--limit", help="Max events to scan"),
):
    """'What does PMB know about me?' - a grouped, read-only view of memory.

    Shows everything stored, grouped by type and by source, so you can see
    at a glance what PMB has accumulated and where it came from. Local,
    read-only, no model load.
    """
    from collections import Counter

    from pmb.memory_quality import confidence_from, is_stale
    from pmb.provenance import describe_source, source_key
    eng = Engine()
    events = eng.events.list_active(eng.workspace.id, limit=limit)

    if not events:
        console.print("[yellow]Memory is empty. Use `pmb note` or connect an agent.[/]")
        return

    console.print(Panel.fit(
        f"[bold]{len(events)}[/] active memories in workspace "
        f"[cyan]{eng.workspace.name}[/]",
        title="PMB audit - what I know about you",
    ))

    by_type = Counter(e.event_type for e in events)
    by_source = Counter(source_key(e.metadata) for e in events)
    pinned = [e for e in events if e.importance >= 0.9]

    t1 = Table(show_header=True, header_style="bold magenta", title="By type")
    t1.add_column("Type", style="cyan"); t1.add_column("Count", justify="right")
    for t, n in by_type.most_common():
        t1.add_row(t, str(n))
    console.print(t1)

    t2 = Table(show_header=True, header_style="bold magenta", title="By source (where it came from)")
    t2.add_column("Source", style="cyan"); t2.add_column("Count", justify="right")
    for s, n in by_source.most_common():
        t2.add_row(s, str(n))
    console.print(t2)

    # Highest-importance / pinned facts - the things PMB treats as core about you
    if pinned:
        t3 = Table(show_header=True, header_style="bold magenta",
                   title=f"Core / pinned facts (importance ≥ 0.9) - top {min(15, len(pinned))}")
        t3.add_column("Content"); t3.add_column("From", style="dim")
        for e in sorted(pinned, key=lambda x: -x.importance)[:15]:
            content = e.content[:70] + ("…" if len(e.content) > 70 else "")
            t3.add_row(content, describe_source(e.metadata))
        console.print(t3)

    # Memory health - hygiene signals (read-only, no ranking change)
    now = time.time()
    n_stale = sum(1 for e in events
                  if is_stale(e.timestamp, now, access_count=e.access_count))
    n_lessons = sum(1 for e in events if (e.metadata or {}).get("kind") == "lesson")
    n_failures = sum(1 for e in events if (e.metadata or {}).get("kind") == "failure")
    n_lowconf = sum(1 for e in events if confidence_from(e.metadata) < 0.6)
    try:
        n_conflicts = len(eng.detect_conflicts())
    except Exception:
        n_conflicts = 0
    th = Table(show_header=True, header_style="bold magenta", title="Memory health")
    th.add_column("Signal"); th.add_column("Count", justify="right")
    th.add_row("Lessons (procedural)", str(n_lessons))
    th.add_row("Failures (don't-repeat)", str(n_failures))
    th.add_row("Possibly stale (>180d, rarely used)",
               f"[yellow]{n_stale}[/]" if n_stale else "0")
    th.add_row("Low-confidence (agent-inferred)", str(n_lowconf))
    th.add_row("Conflicts detected", f"[yellow]{n_conflicts}[/]" if n_conflicts else "0")
    console.print(th)

    console.print(
        "[dim]Tip: `pmb recall \"<query>\"` to search · `pmb lessons` for lessons · "
        "`pmb forget <ulid>` to archive · `pmb health conflicts` to resolve.[/]"
    )


@app.command()
def watch(
    path: str = typer.Argument(..., help="File or directory to watch (e.g. ~/journal.md)"),
    interval: float = typer.Option(5.0, "--interval", help="Poll interval seconds"),
    once: bool = typer.Option(False, "--once", help="Single pass then exit (cron-friendly)"),
    importance: float = typer.Option(0.5, "--importance", "-i"),
):
    """Auto-capture: ingest new paragraphs from a notes file/folder into memory.

    Turns existing note-taking into memory with zero extra effort:
      pmb watch ~/journal.md          # poll forever (Ctrl+C to stop)
      pmb watch ~/notes/ --once       # single pass (run from cron/Task Scheduler)

    Content-hash dedup: editing old text won't re-ingest; only new paragraphs
    are added.
    """
    from pmb.ingest.watch import load_state, save_state, scan_new_chunks
    eng = Engine()
    target = Path(path).expanduser()
    if not target.exists():
        console.print(f"[red]Path not found:[/] {target}")
        raise typer.Exit(2)

    state_path = eng.workspace.storage_dir / "watch_state.json"

    def _pass() -> int:
        seen = load_state(state_path)
        new_items, updated = scan_new_chunks(target, seen)
        if new_items:
            eng.record_batch_bulk([
                {"type": "fact", "content": it["content"], "importance": importance,
                 "metadata": {"source": "watch", "file": it["file"]}}
                for it in new_items
            ])
            save_state(state_path, updated)
        return len(new_items)

    if once:
        n = _pass()
        console.print(f"[green]Ingested[/] {n} new paragraph(s) from {target}")
        if n:
            eng.regraph()
        return

    console.print(f"[cyan]Watching[/] {target} (every {interval:.0f}s). Ctrl+C to stop.")
    import time as _time
    try:
        while True:
            n = _pass()
            if n:
                console.print(f"  [green]+{n}[/] new paragraph(s) ingested")
            _time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped watching. Run `pmb regraph` to refresh the entity graph.[/]")


def _try_daemon_recall(query: str, top_k: int) -> dict | None:
    """Best-effort: ask the warm daemon for hybrid (BM25 + vector) recall so the
    CLI matches what an agent gets, without a cold model load. Returns the JSON
    pack, or None when there is no live, version-matched daemon for THIS
    workspace (the caller then falls back to a local cold engine)."""
    try:
        import json
        import os
        import urllib.request

        import pmb
        from pmb.core.workspace import detect_workspace
        from pmb.mcp.daemon import read_daemon_token
        from pmb.mcp.registry import find_live_daemon

        info = find_live_daemon()
        if not info or not info.get("port"):
            return None
        try:
            ws_id = detect_workspace().id
        except Exception:
            ws_id = os.environ.get("PMB_WORKSPACE")
        host = info.get("host") or "127.0.0.1"
        port = int(info["port"])
        tok = read_daemon_token() or ""
        body = json.dumps(
            {"query": query, "top_k": top_k, "workspace": ws_id}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/internal/recall", data=body,
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as r:
            out = json.loads(r.read().decode("utf-8"))
        # Reject a version-mismatched daemon (stale build) or a workspace refusal.
        if out.get("version") != pmb.__version__ or "results" not in out:
            return None
        return out
    except Exception:
        return None


def _pack_from_daemon(query: str, out: dict):
    """Rebuild a RecallPack from the daemon JSON so the normal render path works
    unchanged."""
    from pmb.core.engine.types import RecallPack, RecallResult
    results = []
    for d in out.get("results", []):
        s = d.get("signals", {}) or {}
        results.append(RecallResult(
            ulid=d.get("ulid", ""), event_type=d.get("event_type", ""),
            content=d.get("content", ""), metadata=d.get("metadata") or {},
            timestamp=float(d.get("timestamp") or 0.0),
            score=float(d.get("score") or 0.0),
            bm25_score=float(s.get("bm25") or 0.0),
            vec_score=float(s.get("vector") or 0.0),
            importance=float(s.get("importance") or 0.0),
            recency_score=float(s.get("recency") or 0.0),
            raw_vec=float(s.get("raw_cosine") or 0.0),
        ))
    return RecallPack(
        query=query, workspace_name=out.get("workspace_name", ""),
        workspace_id="", results=results,
        n_total_in_workspace=int(out.get("n_total_in_workspace") or 0),
        elapsed_ms=float(out.get("elapsed_ms") or 0.0),
    )


@app.command()
def recall(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(5, "-k", "--top"),
    rerank: bool = typer.Option(False, "--rerank",
                                help="Cross-encoder reranker over top-25 (adds ~80MB model + ~100ms)"),
):
    """Search memory for relevant events."""
    pack = None
    source = "cold (bm25-only)"
    # A warm daemon gives full hybrid recall without a 25s cold model load.
    # Skip it for --rerank (the cross-encoder lives only on the local path).
    if not rerank:
        out = _try_daemon_recall(query, top_k)
        if out is not None:
            pack = _pack_from_daemon(query, out)
            source = "daemon (hybrid)"
    if pack is None:
        with loading("searching memory (loading embedding model on first run)…"):
            eng = Engine(
                rerank_model="cross-encoder/ms-marco-MiniLM-L-6-v2" if rerank else None,
            )
            pack = eng.recall(query=query, top_k=top_k, rerank=rerank)

    console.print(f"\n[bold]Query:[/] {query}")
    console.print(f"[dim]Workspace: {pack.workspace_name} | "
                  f"Search took {pack.elapsed_ms:.1f}ms | "
                  f"Total in workspace: {pack.n_total_in_workspace} | "
                  f"source: {source}[/]\n")

    if not pack.results:
        console.print("[yellow]No matches.[/]")
        return

    from pmb.memory_quality import confidence_from, confidence_label, freshness_label
    from pmb.provenance import describe_source
    now = time.time()
    for i, r in enumerate(pack.results, 1):
        ts = _humanize_time(r.timestamp)
        sigs = (f"score={r.score:.2f} bm25={r.bm25_score:.2f} "
                f"vec={r.vec_score:.2f} imp={r.importance:.2f}")
        kind = (r.metadata or {}).get("kind", "")
        marker = "[red]⚠ FAILURE[/] " if kind == "failure" else (
                 "[magenta]★ LESSON[/] " if kind == "lesson" else "")
        title = f"#{i}  {marker}[{r.event_type}]  {ts}  ({sigs})"
        content = r.content[:500] + "..." if len(r.content) > 500 else r.content
        # Trust signals (display only - no effect on ranking):
        src = describe_source(r.metadata)
        conf = confidence_from(r.metadata)
        stale = freshness_label(r.timestamp, now,
                                access_count=getattr(r, "access_count", 0) or 0)
        sub = f"{r.ulid}  ·  from: {src}  ·  confidence: {confidence_label(conf)}"
        if stale:
            sub += f"  ·  [yellow]⚠ {stale}[/]"
        console.print(Panel(
            content,
            title=title,
            subtitle=f"[dim]{sub}[/]",
            title_align="left",
        ))


@app.command()
def why(
    query: str = typer.Argument(...),
    top_k: int = typer.Option(3, "-k", "--top", help="Explain the top-K results"),
):
    """Explain WHY recall ranked results the way it did - full PAMVR trace.

    Most memory tools are a black box: you get results, no idea why. `pmb why`
    shows, per result, exactly which of the 14 predicate-aware reranking rules
    fired and the multiplier each contributed. Great for debugging a miss or
    just understanding the engine.

      pmb why "where do I live now"
    """
    from pmb.reasoning.pamvr import explain_pamvr
    eng = Engine()
    with loading("searching memory (loading embedding model on first run)…"):
        pack = eng.recall(query=query, top_k=top_k)

    console.print(f"\n[bold]Why these results for:[/] {query}")
    console.print(f"[dim]Workspace {pack.workspace_name} · {pack.elapsed_ms:.1f}ms · "
                  f"{pack.n_total_in_workspace} events[/]\n")

    if not pack.results:
        console.print("[yellow]No matches - nothing to explain.[/]")
        return

    bridges = getattr(eng, "_vocab_bridges", None)
    for i, r in enumerate(pack.results, 1):
        ev = eng.events.get_by_ulid(r.ulid)
        content = r.content[:160] + ("…" if len(r.content) > 160 else "")
        console.print(f"[bold cyan]#{i}[/]  {content}")
        console.print(
            f"   [dim]final score {r.score:.3f}  "
            f"(bm25 {r.bm25_score:.2f} · vec {r.vec_score:.2f} · imp {r.importance:.2f})[/]"
        )
        if ev is None:
            console.print("   [dim](event detail unavailable)[/]\n")
            continue
        exp = explain_pamvr(query, ev, base_score=1.0, vocab_bridges=bridges)
        if not exp["rules_fired"]:
            console.print("   [dim]no PAMVR rules fired (pure BM25+vector match)[/]\n")
            continue
        for step in exp["rules_fired"]:
            mult = step["mult"]
            arrow = "[green]▲[/]" if mult > 1.0 else "[red]▼[/]"
            console.print(f"     {arrow} {step['rule']:<40} ×{mult:.2f}")
        net = exp["net_multiplier"]
        net_color = "green" if net >= 1.0 else "red"
        console.print(f"   [bold]net PAMVR multiplier: [{net_color}]×{net:.2f}[/][/]\n")


@app.command()
def overview(
    topic: str = typer.Argument(..., help="Topic to summarize, in quotes"),
    max_events: int | None = typer.Option(
        None, "-n", "--max-events",
        help="How many memories to synthesize (default: config overview.max_events)."),
):
    """'What do I know about <topic>?' - a structured overview from memory.

    Aggregates the relevant memories into key facts & decisions, lessons,
    failures, open goals, a timeline and related topics. No LLM, fully local -
    great for getting up to speed on a project/feature before you start.

      pmb overview "authentication"
    """
    eng = Engine()
    if max_events is None:
        try:
            max_events = int(eng.config.get("overview.max_events"))
        except Exception:
            max_events = 40
    with loading("building overview (loading embedding model on first run)…"):
        ov = eng.topic_overview(topic, max_events=max_events)
    if ov.get("empty"):
        console.print(f"[yellow]No memories about[/] '{esc(topic)}'.")
        return
    span = ov.get("span") or {}
    console.print(Panel.fit(
        f"[bold]{ov['n_memories']}[/] memories about [cyan]{esc(topic)}[/]"
        + (f"  ·  {span.get('from')} -> {span.get('to')}" if span else ""),
        title="PMB overview",
    ))

    def _sect(title, items, marker=""):
        if not items:
            return
        console.print(f"\n[bold magenta]{title}[/] ({len(items)})")
        for it in items:
            d = f"[dim]{it.get('date')}[/] " if it.get("date") else ""
            console.print(f"  {marker}{d}{esc(it['content'])}")

    _sect("Facts & decisions", ov["facts"])
    _sect("Lessons", ov["lessons"], "[magenta]★[/] ")
    _sect("Failures", ov["failures"], "[red]⚠[/] ")
    _sect("Goals", ov["goals"])
    if ov.get("related_topics"):
        console.print("\n[bold magenta]Related topics:[/] "
                      + ", ".join(esc(t) for t in ov["related_topics"]))


@app.command()
def pin(ulid: str = typer.Argument(...)):
    """Pin an event - high importance, never auto-archived."""
    eng = Engine()
    eng.pin(ulid)
    console.print(f"[green]Pinned[/] {ulid}")


@app.command()
def forget(ulid: str = typer.Argument(...)):
    """Archive an event (reversible). Restore with `pmb restore <ulid>`.

    `pmb delete` is the fuller front-door (multiple ulids, a preview, and
    `--hard` to purge permanently); `forget` is the quick single-event archive.
    """
    eng = Engine()
    eng.forget(ulid)
    console.print(f"[yellow]Archived[/] {ulid}  "
                  f"[dim]restore with [cyan]pmb restore {ulid}[/].[/]")


@app.command()
def delete(
    ulids: list[str] = typer.Argument(..., help="Event ULID(s) to delete."),
    hard: bool = typer.Option(
        False, "--hard",
        help="Purge PERMANENTLY (vector + row + graph links). Default archives, "
             "which is reversible via `pmb restore <ulid>`."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Delete one or more memories - the clear front-door for removing things.

    By default this ARCHIVES (reversible; restore with `pmb restore`). Use
    `--hard` to purge permanently (cannot be undone). You always see a preview
    and a confirmation first (unless `--yes`).

      pmb delete 018f...                 # archive (reversible)
      pmb delete 018f... 019a... --hard  # purge several, permanently
    """
    eng = Engine()
    found, missing = [], []
    for u in ulids:
        ev = eng.events.get_by_ulid(u)
        (found if ev is not None else missing).append((u, ev) if ev is not None else u)
    if missing:
        console.print(f"[yellow]Not found:[/] {', '.join(missing)}")
    if not found:
        console.print("[red]Nothing to delete.[/]")
        raise typer.Exit(1)

    verb = "permanently DELETE" if hard else "archive"
    colour = "red" if hard else "yellow"
    console.print(f"[{colour}]{len(found)}[/] memory(ies) to {verb}:")
    for u, ev in found:
        body = esc((ev.content or "")[:80]) + ("…" if len(ev.content or "") > 80 else "")
        console.print(f"  [dim]{u[:12]}[/] [{ev.event_type}] {body}")

    if not yes:
        prompt = ("Permanently delete - this CANNOT be undone?" if hard
                  else "Archive these (reversible)?")
        if not typer.confirm(prompt, default=not hard):
            console.print("[yellow]Cancelled.[/] Nothing changed.")
            raise typer.Exit(0)

    n_ok = 0
    for u, _ev in found:
        if eng.delete_event(u, hard=hard).get("ok"):
            n_ok += 1
    if hard:
        console.print(f"[red]✗ Deleted[/] {n_ok} memory(ies) permanently.")
    else:
        console.print(f"[yellow]Archived[/] {n_ok} memory(ies).  "
                      f"[dim]Restore with [cyan]pmb restore <ulid>[/].[/]")


@app.command()
def restore(ulid: str = typer.Argument(..., help="Event ULID to bring back.")):
    """Restore an archived memory - undo a soft `pmb delete` / `pmb forget`.

    (Only archived, reversible deletions can be restored. A `--hard` delete is
    permanent and cannot be brought back.)
    """
    eng = Engine()
    eng.unforget(ulid)
    console.print(f"[green]Restored[/] {ulid}")
