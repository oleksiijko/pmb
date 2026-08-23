# Changelog

All notable changes to PMB are documented here.

## [1.2.3] - 2026-08-16 - Version-consistency fix

### Fixed
- **`pmb.__version__` now matches the release.** The 1.2.3 release bumped `pyproject.toml` but left the `__version__` constant (and the npm package version) at 1.2.2, so `pmb daemon status` / `pmb doctor` reported `v1.2.2` even after upgrading — which read as "the restart didn't work" and sent users chasing a non-existent stale-process bug. `__version__`, the npm package, and the CHANGELOG all agree on 1.2.3 now.

## [Unreleased]

### Fixed

- **`pmb decay` no longer dies with `KeyError: 'decay_factor'` on every run.** Decay became per-tier (`signals/decay.py TIER_DECAY_FACTORS`) and `apply_decay` returns `decayed_by_tier`; it has never returned a single `decay_factor`. The CLI summary line still formatted `result['decay_factor']`, so the command raised on every invocation — *after* the importance updates had been written, so the decay itself succeeded and only the report died. That is the worst shape for a maintenance job: scheduled decay looks broken (or, piped to a log, looks like a hard failure) while the data change silently went through. The summary now reports the per-tier breakdown it actually receives, and tolerates a payload without one.

- **MCP call telemetry is no longer lost when a server shuts down.** `record_call` buffers rows and writes them once `_PERF_FLUSH_EVERY` (25) have accumulated, but nothing flushed the remainder on the way out - so a server that handled fewer than 25 calls recorded *nothing*, and every server dropped its final partial batch. Since most sessions never reach 25 tool calls, `mcp_calls` could stay empty indefinitely, leaving the dashboard Performance tab and `pmb mcp perf` with no data to show. The stdio server now installs an `atexit` hook plus SIGTERM/SIGINT handlers (`install_shutdown_flush`), which matters because an MCP stdio server is a child process its host terminates with a signal, and Python does not run `atexit` handlers on SIGTERM. The signal path restores the previous disposition and re-delivers, so Ctrl-C and `SIGTERM` still behave exactly as before.
- **The warm daemon drains its buffered perf rows when it stops serving.** `run_daemon` hands the process to `uvicorn.run`, and uvicorn captures SIGTERM/SIGINT itself: it shuts down, restores the previous handlers, then re-raises the signal from inside `Server.capture_signals`, so the process dies without `uvicorn.run` ever returning. Neither `install_shutdown_flush`'s signal path, nor `atexit`, nor a `finally` around `uvicorn.run` can run on the path that actually stops a daemon. That left `/internal/shutdown` and the idle watcher as the only drains — and the watcher does nothing at all when `daemon.idle_exit_min` is `0` (never idle-exit), so a daemon serving fewer than `_PERF_FLUSH_EVERY` calls recorded no telemetry. The drain now hangs off the ASGI **lifespan shutdown**, which does run before uvicorn re-raises; all exit paths share one `_drain_before_exit` helper. Verified end to end: 6 real MCP calls through a daemon, then `SIGTERM` — `mcp_calls` did not even exist before, and holds all 6 after.

### Added
- **OpenAI API backend support** across consolidation/reasoning, `pmb-chat`, graph extraction, autowrite summaries, config, doctor, and LLM command help. Uses stdlib HTTP; no OpenAI SDK dependency.

## [1.2.2] - 2026-06-29 - Dashboard port fallback and ambient watcher UX

### Fixed
- **`pmb dashboard` no longer crashes with a raw socket traceback when the default port is unavailable.** If `127.0.0.1:8765` is already held by the warm daemon / MCP server, the dashboard auto-skips to the next free port; explicit `--port` choices now get a short actionable diagnostic instead of a Python stack trace.
- **MCP recall lesson E2E now checks the supported contract.** Procedural rules returned by MCP `recall()` live in the top-level `lessons` field, so the old quarantined assertion against only `results` was removed.

### Added
- **`pmb ambient-watch` can manage several MCP-only project observers in one process.** Run `pmb ambient-watch app api docs --save` once, then restart all saved targets with `pmb ambient-watch --all`.

## [1.2.1] - 2026-06-27 - Security and CI-stability pass

### Security
- **Dashboard static serving is path-traversal-proof.** `/static/<name>` is served by exact-name lookup against `STATIC_DIR`'s own listing, so user input never reaches a filesystem path - closing CodeQL `py/path-injection` and the related response-splitting on the derived `Content-Type`.
- **Encrypted-bundle import is tar-slip-safe.** `import_workspace` rejects symlink / hardlink / device members and never calls an unfiltered `extractall` (older runtimes extract validated members by hand) - CodeQL `py/tarslip`.
- **File-path regex made linear (ReDoS).** The entity-graph file-path pattern no longer backtracks catastrophically on long slashless input.
- **Dashboard log injection closed** - the request route is stripped of CR/LF before logging.
- **Git post-commit hook installed `0o700`** (owner-only) instead of `0o755`.

### Added - developer tooling
- **`scripts/codeql_local.sh`** runs CI's CodeQL `security-extended` suite locally (auto-installs the bundle on first run), so security findings surface before a push.
- **`scripts/test.sh`** runs the whole suite or any subset; **`scripts/install-dev-hooks.sh`** wires a pre-commit hook (ruff + CodeQL).

### Fixed - CI stability
- **Matrix-reddening flaky tests fixed at the source:** the smart-recall confidence check tolerates time-based float jitter; the dashboard and delete-API HTTP harnesses wait for readiness and retry transient timeouts (shared `tests/_http.py`); an embedder-float-variance ranking test is marked `platform_sensitive` (gates on the Linux reference runner).

### Fixed - stability and recall-correctness pass (live-session QA)
- **`forget` / `pin` / `restore` invalidate the recall cache**, so a forgotten memory stops surfacing immediately instead of lingering in the warm daemon's cache until the TTL.
- **PMB's own LLM sub-calls no longer trip PMB's hooks.** `pmb track changes` / `track modules` / consolidate spawn `claude -p`; those sub-calls now set `PMB_INTERNAL_LLM=1` and `pmb-hook` no-ops on it, so the summarizer's own instruction ("Capture the INTENT, not the diff") is no longer captured as a "user correction" draft lesson.
- **`pmb track modules` never stores a refusal as a module purpose.** The prompt states the indexed structure is the only input, and refusal / clarifying-question / "unknown" responses are dropped instead of stored.
- **`pmb history` / `pmb correlate` find commits tracked by `pmb track changes`** (read both the `files` and `files_changed` metadata keys and the `git-change` fact source, not only the legacy git-signal schema).
- **`pmb doctor` checks the configured embedding model** (the default multilingual MiniLM-L12) instead of a hardcoded all-MiniLM-L6-v2, ending a false "model not cached" warning.
- **`pmb recall` (CLI) routes through the warm daemon** for full hybrid (BM25 + vector) results when one serves this workspace, instead of cold BM25-only; falls back to the local cold engine otherwise.
- **`index project` captures module-level constants** (UPPER_CASE assignments) as symbols, not just defs / classes / methods.
- **The entity graph no longer ingests code-index scaffolding** (`file`, `symbols`, `imports`, month names) as nodes.
- **Out-of-band writes are visible without a restart.** A long-running engine (the warm daemon) reloads its index when another process changed it; a cold write (a one-shot CLI with no model loaded) indexes into BM25 synchronously; and the recall cache is invalidated on an external change. A CLI `pmb fact` / `index` is now recallable immediately.
- **Auto-recall surfaces the specific fact for project-named questions** ("what is `<project>`'s X?") instead of only the project overview.
- **Project-scoped recall (`recall(project=X)`) also matches decisions that name the project in their content.** Found in a live agent-to-agent test: one session records a decision (record_batch stores it as an `activity`, dropping the project tag), and a later `recall(project=X)` filtered it out - breaking cross-session memory. The scope filter now falls back to a content mention, so the decision is found.
- **Hooks are emitted UNQUOTED so they parse in PowerShell.** Headless `claude -p` on Windows runs hooks through PowerShell, which ParserErrors on a quoted command (`"pmb-hook" ...`) with no `&` call operator - so SessionStart / Stop / auto-recall silently failed in headless agents and injected memory never reached them. The hook command is now `pmb-hook ...` (unquoted bare name on PATH, or a space-free absolute path), which parses in PowerShell, cmd and bash.
- **The cold auto-context no longer mangles Cyrillic / emoji.** The cold hook path decoded the full `pmb` CLI's UTF-8 stdout with the Windows locale (cp1251), double-encoding non-ASCII - the agent saw "Карта проекта" arrive as "РљР°СЂС‚Р° РїСЂРѕРµРєС‚Р°". The decode (and the child's output codec) are now pinned to UTF-8, matching the daemon path.
- **Captured corrections are scoped to the project they're about.** A correction-capture draft was stored as the raw user message with no project context, so it surfaced contextless and could leak across the other projects sharing a memory store. The draft now records which project the complaint is about - both as structured `project_name` metadata (so `find_lessons(project=...)` keys off it) and inline as a `[project: X]` tag - resolved via the same lenient detector the overview path uses (so dev-ish names like `pmb` are caught). A path-like workspace name is reduced to its basename, so the tag never becomes a full filesystem path.
- **Hooks survive Windows Smart App Control.** Hooks are now invoked through the signed Python interpreter (`<python> -m pmb.hookclient ...`) instead of the pip-generated, UNSIGNED `pmb-hook.exe` launcher. SAC blocks unsigned executables, which silently killed every hook (the user saw "part of this app was blocked" pointing at the hook path, typically right after a reinstall recreated the shims). The interpreter path is the space-free first token, so the command still parses unquoted in PowerShell / cmd / bash.
- **`pmb hooks install` is strip-then-add, so reinstall leaves no stale or duplicate hook lines.** It removes every PMB-marked hook first, then writes the current set - previously an unrecognised marker or an old `pmb-hook` line could survive a reinstall as a duplicate (`capture-exploration` was missing from the marker set and is now included).

### Changed
- **The warm daemon serves the effective `lean` tool profile by default** (forget / recall_smart / index_project / project_structure + the core memory loop), and `pmb daemon start/restart` now reads `daemon.tool_profile` into the spawned daemon, so a manual restart keeps it instead of falling back to the tight 10-tool `minimal` surface.
- **`pmb workspace current`** wording: source `explicit` now reads "explicit workspace id (--workspace flag or caller-provided)".

## [1.2.0] - 2026-06-27 - Dashboard redesign and self-tending memory

A full dashboard redesign, settings moved out of the TUI into the dashboard, and engine work that lets memory tend itself.

### Added
- **Dashboard redesign**: violet PMB design system (Inter, glass surfaces), a live Settings tab that edits config in the browser, global search, a collapsible icon sidebar, a boot loader, a custom confirm dialog, Map polish, an Overview activity chart, an Errors tab, and running maintenance commands from the UI.
- **Memory tends itself**: the daemon maintenance tick now runs the forgetting-curve decay before archive_cold (gated by `daemon.maintenance_decay`), so importance decays on its own instead of needing a manual `pmb decay`.
- **Recall quality**: saturated the graph boost `w/(1+w)` and tuned the `graph_boost` default.
- **New docs page: "Does PMB actually help — and how we measure it honestly"**
  (`concepts/measuring-impact.md`). Lays out the two-method evaluation stance -
  retrieval benchmarks (LoCoMo / multilingual) for recall quality, and Earned
  Memory's three honest layers (associational lift → Wilson-CI verdicts →
  within-lesson followed-vs-ignored causal read) for whether memory changes
  outcomes - with the limitations stated plainly and how to run it on your own
  workspace.

### Changed
- **Silent best-effort swallows in the write path are now observable.** Audited
  the ~340 bare `except Exception: pass` blocks. The hot write/recall paths use
  them heavily, but for BEST-EFFORT secondary work (graph edges, temporal /
  identity enrichment, dedup queueing) layered AFTER the primary event is
  already persisted - non-blocking by design, not data loss. The real gap was
  visibility: a systemic enrichment failure degraded recall silently. The
  recall-affecting swallows in the hot path - `write.py` (graph / temporal /
  identity / dedup enrichment), `embed.py` (queue drain - a systemic failure can
  no longer silently leave events unembedded and unfindable) and `recall.py`
  (scoring boosts + recall-time drain) - now route through the existing
  `errlog.log_error` seam (the idiom `batch.py` already used), so a repeated
  failure surfaces in `pmb doctor` / the status panel instead of vanishing.
  Warmup probes, config reads and intentional fall-throughs are left silent by
  design. The happy path is unchanged - zero cost unless something throws.
- **Default MCP tool surface trimmed 34 → 10 (`minimal` is now the built-in
  default profile).** A freshly connected agent now sees a tight core-10 -
  `prepare`, `recall`, `project_overview`, `find_lessons`,
  `mark_lesson_followed`, `session_brief`, `record_batch`, `record_keyed_fact`,
  `list_goals`, `update_goal` - instead of 30+ tools, cutting choice-confusion
  and per-turn token cost. Nothing is removed: the fuller surface stays one flag
  away (`PMB_TOOL_PROFILE=default` = 34 tools, `full` = ~65), and the public
  `default` surface is byte-for-byte unchanged (still pinned by the API-contract
  test). `pmb connect claude-code` now pins `minimal` (was `lean`).
- **Earned Memory is now statistically honest.** `lesson_impact` /
  `pmb health lessons-impact` previously reported per-lesson `lift` as a bare
  point estimate, so an n=1 fluke could read as a real "useful"/"harmful"
  effect. Each lesson now carries a 95% Wilson confidence interval and a
  conservative `verdict` - `useful`/`harmful` only when the CI clears the
  no-lesson baseline AND n ≥ `min_n` (default 5), otherwise `unverified` /
  `insufficient`. The report adds `signal_sufficiency`, a `trustworthy` flag,
  `n_confident`, and an explicit confounding-by-indication `caveat`; the CLI
  shows the CI, the verdict, and a "not yet trustworthy" banner. The
  measurement-only stance is unchanged - it now states WHEN it can't be trusted
  instead of emitting a misleading number.
- **Earned Memory gains a within-lesson causal read.** Beyond the associational
  `lift` (vs the no-lesson baseline), each lesson now reports a `causal_verdict`
  (`helps` / `hurts` / `inconclusive` / `insufficient`) from a
  followed-vs-ignored comparison of the SAME lesson's turns - holding the
  surfacing trigger fixed, which controls for the confounding the baseline lift
  cannot. It fires only when both arms clear `min_n` and their Wilson CIs
  separate. New per-lesson fields `n_followed`, `n_ignored`,
  `followed_success_rate`, `ignored_success_rate`, `followed_lift`; a top-level
  `n_causal`; and a CLI section that prints the conclusive reads (or says the
  causal read is pending more data). Residual confound noted in `caveat`: the
  agent's choice to follow may itself track task type.

## [1.1.0] - 2026-06-24 - Semantic tracking, memory feedback, and token-saving guards

A semantic layer on top of `pmb index project`. The structural index says which
symbols exist; this says WHY a file exists and WHY it changed. All local, Haiku
by default (or fully offline via Ollama), and idempotent.

### Added
- **`pmb track changes`** - reads new git commits, asks a small LLM (Haiku) to
  summarise each commit's INTENT (the "why", which a raw diff does not carry),
  and stores it as a `git-change` memory record linked to the files it touched.
  Idempotent via a per-repo cursor kept next to the workspace DB (not in the
  event store, so it never pollutes memory). Flags: `--since`, `--max-commits`,
  `--backend` (auto/claude/anthropic/ollama).
- **`pmb track modules`** - a one-line "what this file does" summary per indexed
  file. Run `pmb index project` first; files already summarised are skipped
  (`--force` to redo), `--limit` caps LLM calls per run.
- **`pmb track install`** - a git `post-commit` hook so `track changes` runs
  automatically after each commit (background, non-blocking, never clobbers an
  existing hook).
- **`project_structure(name)` MCP tool** - the agent pulls a project's
  file/module map straight from memory (languages, files grouped by directory
  with their purpose + symbol count, key modules, recent change intents) with no
  filesystem scan. Assembles what `index_project` + `track modules` +
  `track changes` already stored.
- **`recall_exploration` / `record_exploration` MCP tools (incremental cognition)** -
  memoize the conclusion an agent reaches after exploring the codebase, keyed to
  the content hash of each source file. A future session calls `recall_exploration`
  and gets the conclusion back with a freshness check: `fresh` when every source
  is byte-identical (reuse it, skip the re-reading), otherwise `stale_files` lists
  what changed so only those are re-checked. Caches the agent's research OUTPUT
  (an incremental build for reasoning), targeting repeated exploration - the real
  token/turn cost - rather than context size.
- **Auto-capture of exploration memos (`pmb capture-exploration`, Stop hook).**
  Off by default (`memo.autocapture_enabled`); when on, a turn that read >= N
  files and ended with an answer is memoized from the transcript automatically.
  `recall_exploration` also reports `repo_changed_since` (the repo moved beyond
  the listed sources, so verify even if the listed ones are fresh), closing the
  false-fresh gap.
- **Memory Delta Protocol (`memory_delta.enabled`, default off).** A per-session
  ledger of items PMB has already shown the agent. When auto-context re-injects
  the same lesson, the renderer collapses it to a one-line handle reference
  (`! [M07] still active`) instead of restating the full text. New items get a
  fresh `[Mxx]` tag, updated items are flagged, and items that left the active
  set surface as `Expired since last turn: M03`. SessionStart fires a Context
  Rebase that clears the ledger so the next render restates full text - critical
  after `/compact` because the model loses the handle->text mapping. Measured
  -72% block size on repeat injections of 3 typical lessons; default off until
  measured on your own workflow. CLI: `pmb memory ledger`, `pmb memory rehydrate`.
- **Resume note (`pmb resume save / show / install`).** Writes
  `.pmb/resume.md` - a structured, committable markdown snapshot of "where we
  are now": open goals, recent decisions, lessons, recent activity, files
  touched, latest exploration conclusions, and project structure. Sourced from
  PMB's typed memory (not extractive), so the sections have real structure.
  Content below the `PMB-RESUME-MARKER` line in an existing file is preserved
  across regenerations - hand-edited additions survive. `pmb resume install`
  flips `resume.auto_save_enabled` so the Stop hook refreshes the file at every
  turn end; commit it to share session continuity with your team.
- **Earned Memory (`pmb health lessons-impact`, `lesson_impact` MCP tool).** Joins
  each surfaced lesson to the OUTCOME of the turn it was active in (tests pass/fail,
  red->green, build, deploy - no LLM) and reports per-lesson success-rate, lift vs
  the no-lesson baseline, and churn - so PMB can show which rules pull weight vs
  which are dead-weight or harmful. Measurement only; ranking/decay feedback is
  deferred until the outcome signal is dense enough to trust.
- **Capture-on-correction.** When a user message reads as pushback / frustration
  / a repeat complaint (RU/UK/EN profanity, ALL-CAPS, "те же грабли",
  repeat-word + a negated action), PMB records a DRAFT lesson on the FIRST
  occurrence and nudges the agent to refine it - so the rule exists in memory
  when it is needed, not after the seventh complaint. Tagged
  `source=correction-capture`, deduped within a 90-min window. Knobs:
  `auto_recall.correction_capture`, `correction_record_draft`,
  `correction_importance`.
- **Repeat guard (message-time).** When a new message strongly overlaps an
  existing rule, it is promoted to a LOUD banner at the top of the auto-context
  block ("YOU'VE HIT THIS BEFORE") instead of one line in a wall. Knob:
  `auto_recall.repeat_guard`.
- **Repeat guard (action-time).** The PreToolUse guard now also fires the
  "do NOT repeat this" corpus - failures + correction drafts - at the MOMENT
  the agent is about to act ("⛔ STOP - you were corrected on this before"),
  so the warning reaches inside autonomous tool loops that have no user
  message. New `find_negative_memories()`; extends the existing R11 lesson
  guard. Replay on a real transcript: the recurring failure became catchable
  at the 2nd occurrence vs the 8th in reality.

### Changed
- **Lesson/decision decay floor.** Lessons and decisions no longer decay below
  importance 0.6, so a critical-but-rarely-recalled rule cannot silently fade
  out of `find_lessons` ranking. Pinned (>=0.99) and ordinary qa/activity
  events are unaffected. Retire a rule with `archive`/`forget`, not by waiting.
- **Surface-ids reach the agent after `/compact`.** `session_restore` now logs
  lesson surfaces BEFORE rendering and embeds `[id:N]` in the text, plus a
  "pending confirmations" block - so `mark_lesson_followed` works on the
  post-compact path (previously the id never reached the agent).

### Fixed
- **`track changes` on merge and root commits.** File list + diff now use
  `git diff-tree --first-parent --root` / `git show --first-parent`, so merge
  commits report their first-parent changes and the initial commit still lists
  its files (previously empty, which made the LLM guess the intent).
- **Faster offline LLM calls.** The Claude-CLI backend passes
  `--strict-mcp-config`, so headless one-shot calls (track summaries,
  consolidation) no longer load the user's full MCP server set on every call.

Reuses the existing LLM client (Claude CLI / Anthropic / OpenAI / Ollama) and runs the
model with no tools, so feeding it untrusted diffs is safe.

## [0.9.3] - 2026-06-14 - Injection precision, cross-lingual recall, model picker

Sharper auto-context: the hook injects the right memory and stays quiet on small
talk, recall finds cross-language answers, and you pick the embedding model in
`setup` (or swap it later in one command). All additive + config-gated - default
behaviour is safe.

### Injection precision (read path)
- **Absolute-evidence gate on by default.** `auto_recall.evidence_min_cosine`
  0.0 -> 0.045: a GENERIC_FACTUAL question the workspace knows nothing about now
  surfaces nothing instead of the min-max top hit (cross-domain noise 100% -> 0%
  on the measured corpus).
- **Specificity gate.** `auto_recall.specificity_strong_cosine` (0.08): a hit
  must share a distinctive token with the message OR be a strong embedding match
  - removes same-domain-but-unhelpful hits the floor alone can't separate.
- **Conversational gate.** A meta turn ("is it better now?") with a diffuse
  match (small score gap + low confidence + no lexical anchor) surfaces nothing
  (`auto_recall.conversational_gap_max` / `conversational_conf_max`).
- **Query-worthiness classifier (opt-in).** The SAE anchor-margin pattern on a
  new axis - conversational vs knowledge-seeking - via English exemplars + the
  multilingual embedder (`auto_recall.query_worthiness`, default off; measured a
  clean split, kept opt-in since the cheap gates already cover the common cases).
- **Rules vs Context split.** Lessons render as RULES (high recall); recall hits
  render as "Possibly relevant context (background, not rules)" (high precision).
- **Corpus-IDF lesson gate.** `find_lessons` / `find_decisions` require a token
  RARE in this workspace's own corpus, not just globally distinctive
  (`recall.lesson_idf_gate`).

### Cross-lingual recall
- **OOV bm25 damping.** When a query's content tokens are entirely
  out-of-vocabulary for the BM25 corpus (e.g. a foreign-language query over an
  English corpus), the lexical channel is down-weighted so the vector channel
  decides (`recall.crosslingual_bm25_damp`, 0.3). F4 cross-lingual top-1 0.27 ->
  0.55, with no in-language regression (floors raised to lock it in).

### Memory model picker
- **`pmb setup` asks which embedder** - Light / Balanced / Best, with plain
  plus/minus + RAM, and loads the chosen one. Plus an offline-LLM-tier choice
  (auto / claude / ollama / anthropic) - background only, no injection cost.
- **`pmb model`** switches the embedder later in ONE step: download + load +
  re-embed memory (`reindex`) + restart the daemon. `pmb model best`,
  `pmb model BAAI/bge-m3`, or the interactive menu.

### Install flow (one warm daemon for every agent)
- **`pmb setup` shares the warm daemon by default.** Following
  `connect.default_daemon`, setup points the host at the ONE warm daemon and
  starts it, so the first message is instant and N clients cost ~400 MB total,
  not ×N. (`--stdio` / `connect.default_daemon false` keeps the old per-client
  server.)
- **`pmb mcp proxy` - stdio↔daemon bridge for codex.** codex is stdio-only and
  can't take an HTTP entry, so it used to cold-load its own Engine+model every
  session (the codex start-up lag). It now launches this lightweight bridge
  (holds no model) that forwards to the shared daemon, autostarting it if
  needed - so codex shares the one warm process too. `pmb connect codex` wires
  it by default.
- **`pmb setup --all`** wires every detected agent at once (Claude Code + Codex
  + Cursor + …), all sharing the single daemon, with one combined summary.
- **In-place setup menus.** The embedder / offline-brain choices are now arrow
  menus that redraw in place (↑↓ pick · ⏎ choose · digit quick-pick · esc keep),
  with a static table+prompt fallback off a TTY / in CI.
- **`pmb daemon kill-all`** stops the daemon AND every registered PMB process,
  then clears the registry - the escape hatch when stray/duplicate warm
  processes pile up and starve a small box (tree-kill on Windows).
- **`pmb-ai` command alias.** The CLI is `pmb`; installed via pip you also get
  `pmb-ai`, and via npm (`npx pmb-ai`) the command is `pmb-ai`. Same tool.
- **npm does a real install cycle.** The npm package no longer just forwards or
  prints "ok": a postinstall (and the launcher, on first use) installs the
  Python `pmb-ai` package persistently, preferring `uv tool install`, then
  `pipx`, then `pip --user`, with visible progress. So `npx pmb-ai setup`
  bootstraps Python PMB and runs setup end to end. Set `PMB_SKIP_POSTINSTALL=1`
  to opt out (CI / Docker). An existing `pmb` on PATH is used as-is.
- **Clearer "you're all set" card** after setup: confirms what was wired (agent
  · hooks · daemon), points to `pmb model` to change the embedder later, and to
  `pmb daemon kill-all` to reset stray processes.

### Codex integration: honest about what's a hook
- **Codex wires `notify` → `pmb codex-notify` (the real mechanism)** for ambient
  auto-write after each turn; read-first / auto-recall is driven by the AGENTS.md
  rules (Codex has no per-turn / session-start shell hook - only Claude Code
  does). `pmb hooks install codex` / `pmb connect codex` now say exactly this
  instead of implying per-turn injection.
- **Removed the dead `~/.codex/hooks/pmb-session-start.sh`.** Codex never
  executed it; it only made `pmb hooks list` look like codex had a session-start
  hook. Install cleans up any leftover; `pmb hooks list` now reports the notify.

### Deleting memories (dashboard + CLI)
- **One clear delete, two levels.** A new `engine.delete_event(ulid, hard=)`:
  soft archives (reversible), hard purges permanently. The hard path
  (`EventStore.purge`) removes the SQLite row, the vector (`search.remove`), and
  the event's graph links so nothing dangles and recall can never resurface it.
- **`pmb delete <ulid...>`** is the front-door: a preview + confirm, archives by
  default (restore with the new **`pmb restore`**), `--hard` to purge, `--yes`
  to skip. The permanent prompt defaults to "no". `forget` / `forget-topic`
  still work for quick archives.
- **Dashboard delete UI.** The event panel now has Archive (reversible) and a
  red Delete (permanent, with a confirm) alongside Pin, backed by
  `POST /api/delete/<ulid>` (`{"hard": true|false}`). The active view refreshes
  after a delete so the memory disappears immediately.

### Documentation
- A proper docs set that renders on GitHub, all hyphen-style (no long dashes):
  [index](docs/README.md) hub, [getting-started](docs/guide/getting-started.md),
  [deleting-memories](docs/guide/deleting-memories.md),
  [architecture](docs/concepts/architecture.md) (components + read/write data
  flow), and [design-and-tech](docs/concepts/design-and-tech.md) (patterns,
  stack, decisions), linked from the README.
- Decluttered `docs/`: removed the screenshot generators (`*.html`, `_save_png.py`,
  `_capture_server.py`, `_template.css`) and the done planning notes under
  `docs/plans/`, keeping `logo.png` and one dashboard screenshot. Fixed a broken
  README image link (it pointed at a missing `02_dashboard.png`) and updated
  `MANIFEST.in` to match.
- Reorganized the docs into a navigable tree (`guide/`, `concepts/`,
  `reference/`, `contributing/`) and added a Material for MkDocs site published
  to GitHub Pages via `.github/workflows/docs.yml` (Pages Source: GitHub
  Actions). New deep-dive pages: `concepts/how-it-works.md` and
  `reference/configuration.md`, plus `guide/dashboard.md`.

### Guard & fixes
- **Command-aware PreToolUse guard.** A rule that NAMES a command ("never use
  git") now fires before every invocation (`git push` / `git status` / chained),
  via structural command-name extraction - no hardcoded command list.
- **No flashing console windows on Windows** - background spawns use
  CREATE_NO_WINDOW (the daemon still survives the launching process).
- **CI offline-model robustness** - `scripts/prewarm_models.py` retries
  downloads and fails fast on the essential embedder; the HF cache key no longer
  rotates on version bumps.
- **Fixed a latency-trace crash** - the PreToolUse shadow-sample reused the
  perf-timer variable name (`auto_recall.py`).

## [0.9.2] - 2026-06-14 - Command palette + first-run

A from-the-first-second CLI experience. No engine behavior change.

- **CLI - command palette.** Bare `pmb` in a terminal opens a fuzzy + synonym
  launcher over every command: type by intent ("make it fast" -> `warmup`,
  "forget old stuff" -> `declutter`/`decay`), arrows to pick, Enter runs. Instant
  and model-free; falls back to the status dashboard when piped (`cli/palette.py`).
- **CLI - first run.** The first `pmb` plays a brain-forming spark boot animation
  and a one-screen welcome (shown once via `~/.pmb/.welcomed`); `pmb setup` is now
  question-free when an agent is detected (`cli/intro.py`).
- **CLI - grouped `--help`.** ~66 commands organized into 7 ordered panels (Setup
  & agents / Capture / Recall & explore / Maintain / Workspaces & data / Agent
  internals / More) instead of one flat wall.
- **CLI - no more blank screens.** 11 model/LLM commands (recall, why, overview,
  reindex, rehearse, dedupe, reflect, arcs, consolidate, distill, regraph) show a
  spinner during cold model/LLM work instead of an empty field.
- **Daemon - robust start.** `pmb daemon start` auto-picks a free port when 8765
  is busy (e.g. a streamable-http MCP server already holds it) instead of dying
  silently, and the spawned daemon's stdout/stderr go to `~/.pmb/daemon.log` (was
  DEVNULL) so a crash is diagnosable.
- **Fix.** The "first run pulls ~90 MB" hint corrected to ~450 MB (the real size
  of the multilingual embedding model) in `pmb setup` and the README.
- **Brand.** The spark wordmark is consistent across setup, status, palette, help.

## [0.9.1] - 2026-06-13

Repository hardening + open-source polish. No engine behavior change.

- **CI:** prewarm the HF models before pytest so the offline suite survives a
  cache miss (a Dependabot PR can't restore the model cache). Fixes the gha-all run.
- **Tests:** the 146-file suite is reorganized into 12 subsystem folders (lang,
  recall, engine, hooks, mcp, ingest, maintenance, security, cli, integration,
  eval, meta) with frozen baselines under `tests/fixtures/`. 1308 passed / 0
  failed - unchanged behavior; repo-root paths are now depth-independent.
- **Docs:** new `docs/usage.md` (per-agent walkthrough); the shipped v0.9 PLAN is
  archived to `docs/plans/`; fixed the `pmb connect claude` -> `claude-code` id.
- **CLI:** `pmb setup` warms the embedding model inline and points at `pmb doctor`.
- **Style:** em/en-dashes replaced with hyphens across the docs and CLI strings.

## [0.9.0] - 2026-06-12 - Anchor Engine

**Memory now understands any language the embedder does - no language packs
required.** A German/Spanish/French/Italian/Polish query routes to the right
intent and the right fact with NO pack for that language (measured: de/es/fr/pl
top-1 recall 1.00 pack-free; intents 12/12 across 6 languages).

- **BREAKING (G3): `packs/ru.yaml` + `packs/uk.yaml` are DELETED.** RU/UK now
  ride the embedder: recall via the multilingual vector channel (V1 RU/UK top-1
  still 1.00, byte-identical), intents + keyed-attribute extraction via the WARM
  anchor tier, and the COLD lexical path self-heals from the user's own traffic
  via ALD (`$PMB_HOME/lang/auto.yaml`). The loader stays for user packs (drop a
  YAML in `$PMB_HOME/lang/` to override) and the opt-in de/es templates.
  **Honest regression:** on a COLD stdio path (no warm daemon, no distilled
  traffic yet) RU/UK general atomic-fact extraction, first-person / self-intent /
  relation / negation / future-intent lexical matchers no longer fire - those
  had no cheap per-candidate warm replacement. The warm-daemon path (the default
  after S6) is unaffected. The packs-off CI gate is now BLOCKING.

Landed against the track (all additive / default-safe; recall behaviour proven
byte-identical via the V1 eval; new multilingual gate in
`test_memory_eval_multilingual.py`):

- **Pack-free extraction + data accuracy (C1-C3, F1-F4).** C1 universal value-
  span detector (`reasoning/spans.py`); C2 keyed-fact extraction by hypothesis
  margin (`reasoning/extract_anchor.py`, warm-only, gated `extract.anchor_keyed`)
  - "Я живу в Киеве" → city=Киеве with zero Russian data; C3 canonical
  `attr: value` atoms; F1 extraction-confidence metadata scaling the recall
  boost; F2 write-time contradiction via the anti-hypothesis; F3 the X2 channel-
  weights learning loop closed (propose in the tick, surface in `pmb doctor`,
  never auto-applied); F4 the eval grown to a multilingual + paraphrase gate.
- **Self-healing lexical cache + corpus stats (D3, E1, E2).** D3 recency-prunes
  the ALD fire log so unused phrasings age out of auto.yaml; E1 derives stopwords
  from the workspace's own document frequency (IDF made explicit); E2 detects PDF
  ALL-CAPS headings via `str.isupper()` (every script, no char-class list).
- **PAMVR on anchors (B3, B4).** B3 precomputes the first-person flag at write
  time (`metadata.fp`) so the per-candidate loop never re-derives it; B4 drops
  the lexical verb-synonym boost in `lang.mode=anchors` (the vector channel
  covers it).

- **Anchor Engine - language-free intent classification (A1/A2/B1/B2 + D1/D2).**
  A new tier that replaces hand-written per-language packs with English-only
  semantic ANCHORS: every role the packs enumerate (goals/past/recent/lessons
  question, work request, self-query, trivial ack) is a set of English
  positives + hard negatives; a message is classified by MARGIN (one-vs-rest)
  against per-set thresholds CALIBRATED at FPR ≤ 1% (`scripts/calibrate_anchors.py`
  → `src/pmb/lang/anchor_calibration.json`, frozen by a test). The shipped
  multilingual embedder does the cross-lingual transfer, so a German/Spanish/
  French/Italian/Polish query lands on the English anchors with ZERO per-language
  data - measured 12/12 across 6 languages × 6 intents. Runs WARM-ONLY (the cold
  hook never loads the model); `lang.anchors` (on) is the kill-switch. B2 extends
  the same tier to two STATEMENT detectors (guidance-seeking query → lesson
  boost; future-plan → suggest-goal flag) in their own scoring/calibration
  GROUPS, so they add multilingual coverage without competing with - or
  regressing - the intent tier (proven: B1 τ/recall byte-identical after B2).
  **ALD (Anchor→Lexicon Distillation):** the maintenance tick mines n-grams that
  reliably predict an anchor in THIS machine's traffic (precision ≥ 0.95,
  support ≥ 6, across-anchor) and compiles them into `$PMB_HOME/lang/auto.yaml`,
  so the COLD lexical path learns the user's languages from their own messages -
  no model, microseconds, fully local. `lang.anchor_log` (on) gates the fire log.
- **Fixed: Codex `record_batch` "timed out awaiting tools/call after 120s".**
  Root cause: the background embed-queue worker EAGERLY triggered the full
  torch model load inside a cold stdio MCP process; under memory pressure (a
  second ~400MB model next to the warm daemon → paging) the load's GIL bursts
  starved the server's asyncio loop, so the client's NEXT call hit its 120s
  deadline. The worker is now PASSIVE (waits briefly for `is_ready()`, never
  loads - `embed.queue_autoload=true` restores the old behavior), writes stay
  durable model-free (`embed_queue_pending`), `warmup()` kicks the leftover
  drain, and `pmb connect codex` adds `tool_timeout_sec=300` headroom
  (re-run it once to pick that up). tests/test_record_batch_cold.py.
- **Memory hygiene + project identity.** `index_project` no longer writes empty
  structural rows or duplicate root rows; `declutter` archives old empty index
  artifacts; project-index labels no longer become bogus person/concept nodes;
  exact project entities win `project_overview`, whose key facts now hide
  machine-generated file rows.
- **Goal reconciliation.** Completed activities automatically close a single
  high-confidence matching open goal; `pmb goals reconcile [--apply]` repairs
  existing divergence. Project overview now reads the canonical `goal_status`.
- **Thread-safe model caches.** Concurrent background embedding workers load one
  shared transformer/cross-encoder instead of racing duplicate native loads.
- **One base-score function + adaptive weights (X1/X2).** The base recall score
  lives in `pmb.reasoning.scoring.combine_base_score`; a per-workspace
  `recall.channel_weights` JSON vector (default identity = no change) scales the
  hit/importance/recency channels, with a `propose_channel_weights` learning
  hook that is never auto-applied.
- **Calibrated confidence (X3).** `calibrated_confidence` - RecallPack.confidence
  as one named, tested, monotonic, bounded function.
- **SLOs-as-code (X4).** `pmb/health/slo.py` ties each quality/latency/durability
  objective to the test that enforces it.
- **Fault-injection + concurrency (X5), security (X7), API contract (X10),
  backup/restore + migration self-heal (X6), docs-can't-lie (X8)** - new gates.
- **`pmb connect` defaults to the shared HTTP daemon (S6).** JSON hosts
  (claude-code/cursor) point at the one warm daemon by default
  (`connect.default_daemon`); `--stdio` opts out. Pins `daemon.idle_exit_min=0`
  and serves `daemon.tool_profile` so the connection stays warm + lean.
- **Recall writes fully off the read path (S9).** Tier promotions ride the
  touch-flusher batch; spreading activation runs on a background thread in the
  daemon; perf telemetry is buffered and flushed on read.

Still roadmap: X2's online weight-learning loop, X9 distribution polish.

## [0.8.0] - 2026-06-11

PLAN.md v0.8.0 "invisible memory": make memory speed invisible and memory
quality measurable, and finish the language-pack vision so the core carries
ZERO Cyrillic. Phases 0, T, S, R, L, V, M.

### Added
- **Thin `pmb-hook` fast lane (S2).** Stdlib-only console script all five
  lifecycle hooks call: warm-daemon localhost path (~10-50 ms) with a full-CLI
  cold fallback; `track-action` runs a dependency-light inline path.
- **Daemon self-heal + workspace guard (S3/S4)** and **`/internal/hook/
  session-restore`** (warm semantic restore).
- **`pmb connect --daemon` (S6).** Point JSON hosts (claude-code / cursor) at the
  ONE shared warm daemon over streamable-HTTP instead of a stdio Engine + ~400 MB
  model per client - N clients cost ~400 MB total, not ×N. The daemon token is
  now PERSISTENT so the baked auth header survives restarts. Opt-in (`--stdio`
  stays the default); codex / editor extensions keep stdio.
- **Memory-quality CI gate (V1).** `tests/test_memory_eval.py` runs a frozen
  EN/RU/UK corpus + paraphrase queries through the full recall pipeline and
  asserts per-bucket + overall top-1/top-3 FLOORS. Measured: EN/RU/UK in-language
  top-1 = 1.00, cross-lingual EN→RU top-3 = 1.00.
- **Intent + PAMVR regression gates (V3/V4).** Labelled EN/RU/UK intent routing
  set; a PAMVR multiplier "freeze" so no `score *= X` change ships silently.
- **Honest hook trace (S10).** The auto-context header now reports true
  end-to-end `total=…ms source=daemon|cold` (was under-reported ~30×) + a
  perf-marked p95 latency smoke (V2).
- **Daemon self-maintenance (M1).** Once per `daemon.maintenance_interval_h` of
  uptime and only while idle, the daemon archives cold rows (archive-only),
  scans conflicts (report-only) and runs a declutter DRY-RUN - surfaced in
  `/internal/health`. Decisions survive (R7). Config `daemon.maintenance`
  (default on) + `daemon.maintenance_archive`.
- **Property-based tests (T6, hypothesis)** for the hot-path invariants
  (tokenizers / intent classifiers / atomic-fact extraction never raise on
  arbitrary unicode).
- **Activity exact-dedup (0.2)**, **truthful CI (T1-T4)** + a **zero-Cyrillic
  ratchet** test.

### Changed
- **Zero-Cyrillic core (Phase L complete).** All Russian/Ukrainian prose AND all
  functional matching DATA (verb stems, stopword sets, intent/heading/relation
  regexes, fact-extraction templates) relocated from `src/pmb/**.py` into the
  active-by-default `pmb/lang/packs/{ru,uk}.yaml`, merged back from an English
  inline floor. **Cyrillic 521 → 0 lines**, enforced by an empty-allowlist CI
  ratchet; behaviour pinned by `tests/test_regex_parity.py`. Shared categories
  (`first_person`, `stopwords`) were kept dedicated-per-consumer so no set
  silently widened.
- **Recall-path speed (S5/S7/S8/S9).** `project_overview` / `_known_projects`
  memoized by write-generation; the arcs N+1 batched (≤50 queries → 1);
  `session_brief` scoped in SQL. `find_lessons` / `find_decisions` served by a
  new `idx_meta_kind` expression index instead of a `metadata_json LIKE`
  full-scan. Workspace-meta rewrite skipped when unchanged; psutil resolved once
  and RSS skipped on the hot discovery path. `RecallCache` made thread-safe
  (daemon worker threads); `_adherence_nudge` cached 60 s off the write path.
- **Lazy `pmb.mcp` package (S1)** (−3-6 s/hook) and **durable decisions (R7)**.
- **Test harness consolidated (T5).** Removed 80 redundant `sys.path.insert`
  lines + 46 duplicated `tmp_pmb_home`/`tmp_workspace_dir` fixture copies in
  favour of the shared `conftest.py`.

### Fixed
- Tree-wide ruff cleanup; the blocking lint gate is green.
- `test_mcp_recall_answer_quality_and_lessons` quarantined with a documented
  root cause (harness prewarm-thread flake, not a product bug).

## [0.7.0] - 2026-06-10

This release lands the daemon + language packs + MCP token diet + write-quality
work (PLAN.md phases B-E, on top of the 0.6.0 keyed-memory correctness fixes)
plus the recall-singleflight and semantic-intent follow-ups.

### Added (recall + hook follow-ups)

- **Recall singleflight (D4).** Concurrent IDENTICAL top-level recalls (same
  workspace, query and top_k) now collapse to one computation - useful under
  the daemon / multi-agent fan-out; followers reuse the leader's result and
  fall back to an independent recall on timeout, so it can never deadlock.
  Config `recall.singleflight` (default on); a no-op for single-client stdio.
- **Semantic intent fallback (C5, opt-in, default OFF).** When lexical intent
  detection finds nothing AND the engine is warm (daemon-served), the hook can
  classify the message by embedding cosine against per-intent exemplars, so a
  query in a language the lexical patterns don't cover still fires. Default OFF
  and eval-gated by design - the measured finding is the semantic tier doesn't
  beat lexical with the default embedder. Config `hooks.semantic_intents`.

### Fixed (write quality - Phase E)

- **A user negation now CLOSES the keyed value it contradicts.** Task-5 retired
  stale negations when a positive value arrived; the reverse was unhandled - with
  `user::city = Tampa`, "I no longer live in Tampa" left Tampa asserted as current
  forever. The negation now closes the keyed value (the active keyed fact is
  archived and stamped `valid_to` / `closed_by` / `closed_reason`), so recall
  stops asserting it while `keyed_fact_as_of` still sees it as history. Only the
  user's own negation (post-0.6.0 subject-adjacent detector) triggers it; gated
  by `keyed.close_on_negation` (default on).

### Changed (write quality - Phase E)

- **The write-time quality gate now defaults ON.** Safe since the junk detector
  became length-aware in 0.6.0 (it flags only empty / placeholder / test-pattern
  / pure-stopword content, never real short facts like "O+"). Flagged facts are
  down-weighted (importance capped at 0.2) and excluded from keyed promotion,
  never rejected. `pmb doctor` reports how many facts were flagged in the last
  30 days so you can audit; set `write.quality_gate=false` to record everything
  unweighted.
- **Routine activities can't crowd out real facts.** Activity importance is
  capped at `write.max_activity_importance` (default 0.8) unless pinned, with an
  `importance_clamped` breadcrumb - agents habitually pass ≈0.9 for routine
  actions. Facts, lessons and goals are never clamped.
- **Agent guidance gained a "DON'T record" section** (in the MCP server
  instructions): skip secrets, transient tool output / stack traces / file
  listings, and anything trivially re-derivable from the repo; future intent
  goes to a goal, not a fact.

### Changed (MCP token diet - Phase D)

- **Tool descriptions shrank ~71%.** The full multi-paragraph tool docstrings
  duplicated the read-before-write workflow and write-triggers table that are
  already in the server `instructions` block, costing ~15.7 KB (~3.9k tokens)
  of context on every `default`-profile session. Non-`full` profiles now serve
  compact one-line descriptions (purpose + when-to-use + key args): default
  drops to ~4.5 KB, lean ~4.1 KB, minimal ~2.2 KB. The `full` profile keeps the
  long docstrings for debugging. A budget test pins this so a docstring can't
  silently re-bloat every session.
- **Recall responses are trimmed before they go over the wire.** `recall` /
  `recall_smart` / `recall_deep` drop genuinely-null (`None`) top-level fields
  and cap each result's content at `mcp.max_item_chars` (default 600 -
  generous, so normal facts are untouched; only pathologically long items
  shrink). Gated by `mcp.compact_responses` (default on). Structural fields
  (`results`, `lessons`) and 0/False values are always kept.

### Added (MCP token diet - Phase D)

- **`pmb mcp perf`** - per-tool latency (p50/p95), error rate and client-timeout
  count from the `mcp_calls` table, so "did the token diet / daemon make tools
  faster" is a number, not a feeling. `pmb connect` already selects the `lean`
  tool profile when it installs hooks (the read-status tools the hooks cover are
  dropped), which now also benefits from the smaller descriptions.

### Added (language packs - Phase C2/C3)

- **Adding a language is now one YAML file.** PMB's lexical fast-paths
  (stopwords, function-words, verb synonyms, attribute aliases, first-person
  markers) shipped covering EN/RU/UK only. A **language pack**
  (`$PMB_HOME/lang/<code>.yaml`) extends them to any language with no code
  changes. The EN/RU/UK lists stay in code as the floor and packs are
  extend-only, so a workspace with no pack files behaves byte-for-byte as
  before (pinned by a parity test). Built-in German and Spanish templates ship;
  `pmb lang list / enable / disable / detect` manage them. `detect` samples the
  corpus and SUGGESTS packs but never enables one silently - auto-activation by
  script would pollute (German and English share the Latin alphabet), so
  enabling is an explicit opt-in. See `docs/adding-a-language.md`.
- **Offline keyed-fact extraction is now pack-aware (C4).** The first-person
  prefilter that gates the offline LLM keyed-suggestion pass recognises the
  user in an enabled language (German "ich"/"mein" passes once `de` is on),
  so keyed extraction works for packed languages too - third-party facts are
  still rejected.

### Changed (faster cold start - Phase D follow-up)

- **`pmb warmup` suggests `fastembed` when the model cold-load is slow (>10s),**
  a lower-RAM, faster-starting backend for the same multilingual model family
  (with the required `pmb reindex` caveat). The warmup message also now points
  at `pmb daemon start` for warm hook recall (the daemon shipped in 0.6.0).

### Fixed (Unicode-correct tokenization - Phase C1)

- **Tokenizers no longer silently drop non-EN/RU letters.** The keyed-fact label
  normalizer, the PAMVR token/proper-noun extractors, the vocabulary miner, and
  the sentence splitter used Latin+Cyrillic-only character classes
  (`[^0-9a-zа-яё]`, `[a-zA-Zа-яА-Я]`, `[A-ZА-ЯІЇЄҐ]…`) that deleted German
  umlauts, Spanish ñ, Turkish letters, Greek, CJK - and even **Ukrainian
  і/ї/є/ґ** (so "Львові" tokenized as "львов"). They are now Unicode-aware
  (`\w`/`str.isupper`/casefold), **provably byte-identical on EN/RU** (a parity
  test pins this against the old regexes) and additive for every other script;
  proper-noun detection keeps its capital-word shape so acronyms still don't
  match. NOTE: this changes tokenization of Ukrainian and other non-EN/RU
  content already in a workspace - run `pmb reindex` to align the BM25 index
  with the corrected tokenizer.

### Added (persistent memory daemon - Phase B)

- **`pmb daemon` - a persistent warm memory process.** It holds ONE warm Engine
  + embedding model + LanceDB so hook-based auto-recall finally gets REAL
  semantic recall instead of the per-process cold skip (`RECALL_COLD_SKIP`).
  `pmb daemon start` spawns it detached, `status`/`stop`/`restart` manage it.
  It is the same streamable-http MCP server with three internal routes
  (`/internal/health`, `/internal/hook/prepare-context`) mounted via fastmcp's
  `custom_route`, behind a per-start bearer token (`$PMB_HOME/daemon.token`).
- **Hooks are now daemon clients with a hard cold fallback.** `pmb
  prepare-context` asks the warm daemon first (localhost, ~0.6s timeout) and
  falls back to the existing in-process cold path the instant the daemon is
  absent or a version mismatch is detected - behaviour is unchanged when no
  daemon runs. When the cold path runs and `daemon.autostart` is on (default),
  a daemon is spawned detached (rate-limited) so the NEXT message is warm.
- **Idle exit.** The daemon exits after `daemon.idle_exit_min` (default 120)
  minutes with no request so it doesn't hold ~400MB forever; the next message
  autostarts a fresh one.

### Fixed (durability + observability - Phase B)

- **`record_batch_async` is crash-safe via a durable outbox.** The batch is
  persisted to a `write_outbox` SQLite table SYNCHRONOUSLY before returning,
  then replayed by a background drainer; a crash between accept and write loses
  nothing (`recover_outbox()` replays leftovers on the next start). The old
  fire-and-forget daemon-thread path - which dropped items on process death -
  is kept only behind `write.outbox=False`. Gated ON by default.
- **Swallowed exceptions leave a breadcrumb.** A new `error_log` table + the
  `pmb.core.errlog` helper replace several bare `except: pass` sites (negation
  tombstone, suggested-key tagging, declutter apply, outbox drain) so a
  silently-degrading path shows up in `pmb doctor` ("Recent errors (24h)")
  instead of being invisible.

## [0.6.0] - 2026-06-09

### Fixed (keyed-memory correctness - Phase A)

- **Negation detection no longer archives facts about OTHER people.** The
  detector previously checked a user cue and a negation INDEPENDENTLY anywhere
  in the text, so "I learned that Alice no longer lives in Paris" was read as
  the USER negating their own city - and recording the user's real city then
  auto-archived Alice's fact. The user subject must now sit immediately before
  the negated verb, evaluated per sentence. Third-party and possessive-chain
  forms ("my sister doesn't work at Google", "my sister's employer is unknown")
  return None.
- **Offline keyed-state suggestions can no longer mislabel a third party as the
  user.** `suggest_keyed_from_llm` gained three gates: a first-person prefilter
  before the LLM is called, an explicit `subject=="user"` field the LLM must
  return, and exclusion of `suspect_junk`-flagged facts. "Alice relocated to
  Berlin" can never become `user::city`.
- **Offline LLM passes are now wall-clock bounded.** A shared `LLMBudget`
  (config `llm.offline_max_calls`=40, `llm.offline_budget_s`=120) caps the WHOLE
  pass, not just a single call - keyed suggestions and the declutter judge can
  no longer run for many minutes on a slow local model.
- **`hometown` is a separate key from current `city`.** "I'm from Kyiv" no
  longer overwrites "I live in Tampa" - origin and current residence are
  distinct keyed attributes.
- **`pmb declutter` stops treating short facts as junk.** The `<8 chars → junk`
  rule archived real memories like `O+`, `HIV+`, `ADHD`, `Tampa`. Short
  non-stopword facts are now surfaced as `short_review` and excluded from
  `--apply` unless `--aggressive` is passed; keyed values are never near-empty
  candidates.
- **Duplicate resolution keeps the most valuable copy.** Exact-duplicate
  archiving previously always kept the newest; it now keeps the copy with the
  highest importance, then access count, then recency, and stamps the archived
  copies with `duplicate_of`.
- **`pmb consolidate` runs the keyed-suggestion pass even with zero clusters,**
  and the command's `--backend`/`--model` now reach that pass (previously it
  returned early on quiet workspaces and ignored the chosen backend).
- **A freshly recorded name takes effect on the next recall.** The user-name
  cache is marked dirty on a "My name is X" write instead of refreshing only
  every 25 events, and the per-recall `SELECT COUNT(*)` is gone (a flag check
  on the common path) - important for a long-lived process.
- **`pmb warmup` no longer over-promises.** Its message now states that warmup
  only warms the current process; hook-based auto-recall stays SQL-only until
  the persistent daemon ships.

### Fixed (Phase 0.6.0 baseline - previously merged)

- **No personal-name or test-name literals leak into ranking.** The recall
  identity-marker boost no longer hardcodes a name - it is driven by the mined
  user-name cache, and the router's identity-intent detection matches the
  user's OWN learned names dynamically instead of a baked-in literal.
  `DEFAULT_NAMED_ENTITIES` (which leaked benchmark names like alice/stripe/adyen
  into every production query) is now empty; real entities come from the dynamic
  proper-noun extractor + the user-name cache.
- **Negation / "unknown" facts no longer linger as stale noise.** Once a
  positive keyed value exists (e.g. `user::city = Tampa`), older facts that
  negate or mark-unknown that attribute ("the user does not currently live in
  Warsaw; current city is unknown") are archived (`superseded_by`,
  archive-only). Runs at write time and as a `pmb repair-keyed` pass. Gated by
  `keyed.archive_obsolete_negations` (default ON). Lessons and pinned events are
  never touched.
- **The CLI is English-only.** Russian docstrings/examples that leaked into
  `--help` are translated; functional multilingual trigger templates
  (`pmb connect`) are unchanged.
- **Recall never crashes on an empty/degenerate BM25 corpus.** `rank_bm25`
  divides by zero when the corpus has no terms (a fresh-workspace recall before
  the embed/index queue drained); search now guards it and falls back to
  vector-only ranking instead of raising. Also stabilized 4 long-flaky tests
  (temporal proximity, rehearse cap, auto-consolidate, MCP e2e) so the full
  suite is deterministic.

### Added

- **Status dashboard.** Bare `pmb` (no subcommand) prints a workspace status
  panel - active workspace + how it resolved, storage sizes, event counts,
  running MCP servers, embedding warm/cold. `pmb --help` is unchanged. Slow
  paths (`recall`/`remember` first run, `index`, `migrate-workspaces`,
  `compact`, `declutter`) show a loading spinner.
- **Workspace switching.** `pmb workspace use <name>` persists a default
  workspace (resolution: env → project `.pmb/workspace.yaml` → saved default →
  git/cwd), `pmb workspace current` shows the active one + which rule won, and
  `pmb workspaces` marks the active one. Fully additive - setups that never run
  `use` resolve exactly as before.
- **Time-based forgetting.** `pmb decay --archive-cold` archives facts/activities
  that are old AND never recalled AND low-value (never pinned/keyed/lessons/
  goals). Dry-run by default; config `decay.archive_cold_days` (90),
  `decay.archive_cold_max_importance` (0.25).
- **Junk sweeper.** `pmb declutter` archives test artifacts, near-empty/stopword
  content, exact duplicates, and obsoleted negation tombstones; optional `--llm`
  judge (bounded, capped, circuit-broken) reviews borderline low-value facts.
  Dry-run + archive-only.
- **Plans become goals.** A future-intent fact ("remember we'll do X next") is
  routed to a goal: MCP docstrings + the `pmb connect` template carry the rule,
  `record_batch` accepts `{"type": "plan"}` (a goal with `kind=plan`), and
  `record_fact` flags forward-looking phrasing with `metadata.suggest_goal`
  (a hint - never an auto-convert). New `pmb goals` / `pmb goals done <ulid>`.
- **Offline LLM keyed-state tier.** During consolidation, an offline bounded LLM
  proposes keyed current-state (`{attribute, value, negation, confidence}`) for
  facts the cheap regex missed; confidence≥0.8 positives upsert via the
  canonical keyed path, weaker ones are tagged `metadata.suggested_key`. Gated by
  `consolidate.suggest_keyed`; never on the recall hot path.
- **Per-deployment reference data.** Optional `PMB_HOME/reference.yaml` extends
  attribute aliases / known techs / stopwords / function-words (extend-only) and
  overrides kind priorities - no Python edits. Missing file = identical
  behaviour.

### Changed

- **Write-time quality gate (opt-in, `write.quality_gate`, default OFF).** When
  on, suspected-junk facts are flagged (`quality_flag=suspect_junk`) and capped
  at importance 0.2 and excluded from keyed promotion - never rejected.

## [0.5.0]

### Fixed

- **recall_smart no longer hangs the interactive path.** It is bounded by an
  overall wall-clock deadline (`recall.smart_deadline_ms`, default 15s) with a
  local-only fast path - it never resolves an LLM / spawns the Claude CLI on the
  foreground path (the cause of 120s timeouts). LLM query-decomposition is opt-in
  (`recall.smart_allow_llm`) and, when on, is clamped to the remaining budget. A
  new explicit `recall_deep` tool/method runs the slow, thorough pass on demand.
  Each pack carries an `escalation` field (stages run + why it stopped) so callers
  don't fan out redundant recalls.
- **Stale personal attributes no longer out-rank the current value.** Keyed-fact
  attribute names are canonicalized (`city` / `current_city` / `current_city_2026`
  / `lives_in` / `город` → one key), so an update supersedes the old value instead
  of creating a competing key. Old values are kept as history.
- **Current-state facts become keyed facts.** A plain "I now live in X" / "сейчас
  живу в X" is detected (conservatively, with a negation guard so a "do not say X"
  instruction is never promoted) and upserts the matching keyed attribute.

### Added

- **`pmb repair-keyed`** - two-pass keyed-fact repair: promote current-state facts
  buried in plain text into keyed facts, then collapse alias/duplicate keys onto
  one canonical value. Archive-only, dry-run by default.
- **`pmb migrate-workspaces`** - merge a per-project workspace into a unified
  memory, tagged `project=<name>`; the source is left intact (reversible). The
  `recall` tool gains an optional `project` filter over the one memory.
- **`pmb mcp status`** + a running-server registry - see how many MCP servers are
  live and their memory; an HTTP `pmb mcp serve` refuses to start a second instance
  on a live host:port (per-session servers each load the model + LanceDB).
- **Backend circuit breaker** - a repeatedly-failing/slow deep backend is
  temporarily disabled for the interactive path (`recall.breaker_threshold`,
  `recall.breaker_cooldown_s`); state exposed via `breaker_status`.
- **Performance dashboard** now records & shows per-call recall_smart stages,
  client-timeout (vs server completion), backend, and cache hit/miss.

### Changed

- New (additive) config keys: `recall.smart_deadline_ms`, `recall.smart_allow_llm`,
  `keyed.auto_detect_current_state`, `recall.breaker_threshold`,
  `recall.breaker_cooldown_s`. Defaults preserve prior behaviour.
- MCP tool profiles: default now 30 tools, lean 26 (added `recall_deep`).

## [0.4.0]

### Added

- **Ambient memory (auto-write).** PMB journals the agent's work even when it
  forgets to call `record_batch`. A PostToolUse hook logs significant actions
  (`pmb track-action`); a Stop hook synthesizes one activity entry per turn
  *only if* the agent didn't record its own (`pmb autowrite`), gated by a
  git-free, outcome-based importance score (tests passed / failure fixed /
  deploy ran - not raw file count). Every entry is tagged `source=autowrite`,
  shown as auto, and removable with `pmb forget-auto`. Works on Claude Code
  (hooks), OpenAI Codex (rollout parse, `pmb codex-notify`), and MCP-only hosts
  (git observer, `pmb ambient-watch`). `pmb hooks capabilities` reports
  per-agent support. ON by default (`autowrite.enabled`).
- **`lean` MCP tool profile** (25 tools): the default set minus the read-status
  browse tools a host hook already covers, set automatically by
  `pmb connect claude-code` so the agent isn't offered slow MCP versions of what
  auto-recall / session-restore already inject for free.
- **Lesson follow-through `not_applicable` state.** The Stop-hook followcheck
  classifies a surfaced lesson with zero overlap with the turn's work as
  not-applicable and excludes it from the adherence denominator, instead of
  counting it as a phantom "not followed". Follow-rate is now over *applicable*
  surfaces - it reflects relevant lessons, not surfacing volume.
- **Opt-in semantic lesson tier** (`recall.lesson_semantic`, experimental, off
  by default): cosine over the existing embeddings to catch paraphrase /
  cross-lingual lesson matches the lexical gate can't.

### Changed

- **Lesson-surfacing precision.** Surfacing uses a shared tokenizer + stopword
  set (`pmb.core.text_match`, symmetric with followcheck): generic and
  filesystem-path noise is filtered, and a relevance gate
  (`recall.lesson_min_overlap`, default 1) replaces the old "any shared 3-char
  word" match. Auto-recall also skips non-message blocks (task-notifications /
  system reminders). Materially cuts irrelevant surfacing on real workspaces.
- **`graph.async_llm`** (default on): LLM entity extraction runs in a background
  worker off the write hot path, so records return instantly; `pmb regraph` is
  the backstop.

### Fixed

- **`lean` profile was silently ignored** - the post-registration tool filter
  fell back to the full default set; it now selects the lean set correctly.
- **`Engine.close()` drains the async batch-write, deferred-graph, and touch
  queues** (each bounded) so in-flight work isn't dropped on shutdown.
- **Persistent WAL no longer forced on non-PMB databases** - the global
  `sqlite3.connect` pragma patch applies `journal_mode=WAL` only to PMB-owned
  DBs (under `PMB_HOME` / named `events.sqlite` / `:memory:`); third-party
  connections in the same process get only the ephemeral pragmas.
- Hyphenated compound words (`lesson-surfacing`) are matched by their parts.
- `SCHEMA_VERSION` bumped to 6 to match the shipped `lesson_surfaces` table.

## [0.3.0]

### Added

- Auto-recall, session-restore, and lesson follow-through lifecycle hooks;
  optional HTTP transport with bearer-token auth for shared/team workspaces.

## [0.2.1]

### Added

- **`pmb learn "..."` + `pmb lessons`** - procedural memory: teach PMB durable
  lessons ("this repo uses pnpm, never npm") that surface via the hybrid +
  predicate-aware ranker, and review them.
- **`pmb learn --failed` / negative memory** - record failures ("numpy 2.x
  broke lancedb") that recall flags with a warning so they're not repeated.
- **`pmb distill` + auto-distill on session end** - LLM extracts durable
  lessons/failures from a session's events automatically (zero-command when
  `lessons.auto_distill_on_session_end` is on). Off the recall hot path.
- **Lesson-intent boost** - on how-to/convention queries, lesson & failure
  memories are gently boosted so the agent applies them. Scoped to
  lesson/failure events only, so it cannot affect recall on datasets without
  them (LoCoMo stays exactly 94.5%). Config: `recall.lesson_boost`.
- **Trust signals in recall** - source attribution, source-derived confidence
  (high/med/low), and a freshness/staleness flag, all display-only.
- **`pmb audit` memory-health** - counts of lessons, failures, stale, low-
  confidence, and conflicting memories.
- **`pmb note "..."`** - instant memory capture from the terminal, no agent.
- **`pmb audit`** - "what does PMB know about me?": a grouped, read-only view
  of everything stored, by type and by source.
- **Source attribution on recall** - every hit shows where it came from
  ("from: chatgpt · Project planning", "from: note (cli)", etc.). Trust feature.
- **`pmb watch <file|dir>`** - auto-capture: new paragraphs in a notes file or
  folder (e.g. `~/journal.md`) get ingested as memory; content-hash dedup.
- **6 more agent integrations (9 total).** `pmb connect` now wires
  windsurf, gemini, vscode, zed, opencode and continue in addition to
  claude-code / cursor / codex. `pmb connect --list` shows every agent and
  its config path; `--config-path` overrides the location.
- **Git-backed workspace sync.** `pmb workspace init|push|pull|status|clone`
  versions and syncs a workspace to any git remote - cross-device, team
  memory, and backups with no server.
- **Encrypted workspace bundles.** `pmb workspace export|import` packs a
  workspace into a single authenticated-encrypted file (scrypt + AES/HMAC),
  safe to store even on a public remote. Needs `pip install 'pmb-ai[crypto]'`.
- **Memory import.** `pmb import chatgpt|claude|mem0|markdown <path>` brings
  existing history into a fresh workspace; the entity graph rebuilds after.
- **`pmb why "<query>"`.** Explains recall ranking with a full trace of which
  PAMVR rules fired and each multiplier - no more black box.
- **Pluggable embedders.** `embedding.backend` now also accepts `ollama` and
  `openai`. A dimension guard refuses to mix embedders of different vector
  sizes in one workspace (which would corrupt recall).
- **`scripts/benchmarks/vs_mem0.py`.** Reproducible head-to-head: same data,
  same queries, same scorer. PMB measured live; mem0 measured with
  `--with-mem0` or shown from published numbers (clearly labelled).

### Fixed

- PyPI page now renders the README logo + screenshots (absolute image URLs).
- LanceDB table is created with the active embedder's real vector dimension
  instead of a hardcoded 384, enabling non-default embedders on fresh
  workspaces.

### Notes

- 57 new regression tests. The headline 94.5% LoCoMo recall@10 and 70ms p50
  are unchanged - the hardening suite verifies no recall regression.

## [0.2.5]

### Onboarding, agent logging, cross-platform & docs

- **`pmb setup`** - guided first-time setup: detects your agent, asks active vs
  conservative logging, and wires PMB in one go (`--yes` for non-interactive).
- **`pmb connect <agent> --active`** - proactive-logging rules: the agent records
  its own decisions / done / lessons / failures / goals during coding, not just
  on "remember". Recall stays lazy; the conservative default is unchanged.
  - **Pro config:** `agent.log_decisions` / `.log_completed` / `.log_lessons` /
    `.log_failures` / `.log_goals` toggle exactly what gets logged, and
    `agent.apply_lessons` enables the **self-improvement loop** - recall + apply
    past lessons/failures before a task so the agent gets better at the project
    over time. Set via `pmb config set` / `pmb tune`, then re-run
    `pmb connect <agent> --active` to regenerate the rules.
- **`pmb overview "<topic>"` + MCP `overview` tool** - a structured "what do I
  know about X?" synthesis (key facts & decisions, lessons, failures, goals, a
  timeline, related topics) from memory, no LLM, fully local. The MCP tool lets
  an agent get up to speed on a project/feature in ONE call instead of several
  recalls. Config: `overview.max_events`.
- **`agent.active_mode`** - auto-logging switch: when on, `pmb connect` /
  `pmb setup` install the proactive rules by default (no `--active` needed).
- **Session continuity (`pmb session brief` + MCP `session_brief`)** - a digest
  of what was decided / done / learned **this session**, so an agent can
  re-orient after its OWN context window compacts in a long session instead of
  re-asking the user what it already did. Active-mode rules tell the agent to
  use it (`agent.context_continuity`); fallback window `session.brief_minutes`.
- **`docs/COMMANDS.md`** - full command reference grouped by task, with examples
  and a clear mark of which commands run fully offline vs need an LLM backend.
- **macOS in CI** - a macOS (arm64) job added to the test matrix so the
  "runs on macOS" claim is tested, not just asserted.
- New tests: `tests/test_connect_active.py` (10: active rules + agent detection).

### MCP correctness: new tools exposed + `session_brief` fixes

End-to-end MCP tests (`tests/test_mcp_e2e.py`, via the fastmcp in-memory client)
drive the real tool routing for the new features on a long-chat scenario, and
caught three bugs that made `session_brief` / `overview` unusable from an agent:

- **`session_brief` and `overview` are now actually exposed.** The post-
  registration tool-profile filter (minimal / default whitelists) was silently
  dropping both new tools, so an agent that called them got *"Unknown tool"*.
  Added `session_brief` to the minimal profile and `overview` to the default.
- **`session_brief` now covers the whole session, not just activities.** Only
  `record_activity` auto-binds an event to the session; facts / goals / lessons
  do not, so the old tag-only scope dropped them once a session existed. It now
  scopes as a union: tagged with the session **or** recorded since the session
  began. (This is why a long chat's lessons/goals went missing from the brief.)
- **`session_brief` classifies decisions / done correctly.** It keyed off
  `metadata.kind`, but `record_activity` stores the kind under `activity_kind`,
  so every decision / completed item fell into "other". It now reads both.
- **Tool-profile filter is event-loop-safe.** It used `asyncio.run(list_tools())`,
  which raised (and left an un-awaited coroutine) when the server was built
  inside a running loop (in-memory client / embedded host). It now detects a
  running loop and skips the introspection cleanly; the stdio server path -
  where gating actually matters - is unchanged.

Tests: `tests/test_mcp_e2e.py` (3: tool exposure, long-chat `session_brief`,
recall answer-quality + lessons + `overview`).

### Answer-ready recall output + temporal validity windows

Three product-code levers targeting end-to-end answer quality (the gap between
strong retrieval and the final answer), informed by per-question J-score
failure analysis. All additive / out of the recall ranking hot path - retrieval
recall@10 and latency are unchanged.

- **Resolved date in every recall result.** `RecallResult.to_dict()` now
  includes a human-readable `date`, resolved as event_time (the date the
  content refers to) -> session date -> creation time. An agent can answer
  "when …?" without epoch math and anchors relative dates to the EVENT, not to
  "today". `to_text()` (prompt injection) uses it too.
- **More write-time atomic-fact patterns** (no-LLM regex): relationship status
  ("X is single") and origin ("X moved from Y"). Still opt-in via
  `consolidate.write_atomic_facts` (default off); turning it on by default is
  gated on a LoCoMo regression run.
- **Temporal validity windows + as-of query.** `record_keyed_fact` now stamps
  `valid_from` on the new value and `valid_to` on each superseded value, and
  `Engine.keyed_fact_as_of(subject, attribute, at_time)` returns the value that
  was current at a past time (Zep-style "what was true in March") - prior
  values stay queryable instead of just being archived.

Tests: `tests/test_jscore_levers.py` (10).

## [0.2.4]

### LLM-as-judge (J-score) eval harness improvements

The `--judge` mode of `scripts/benchmarks/benchmark_locomo.py` reports a
J-score (LLM-as-judge) - the end-to-end metric mem0 / Zep / Letta publish,
where a reader LLM answers from retrieved context and a judge LLM grades the
answer against gold. This release makes that harness fair and debuggable:

- **Full reader context.** Per-chunk cap raised 1500 -> 6000 and total
  6000 -> 20000. Previously the answer turn could be truncated out of the
  reader's view, capping the J-score far below retrieval recall@10.
- **Reader prompt** now scans every numbered chunk (not just the first) and
  resolves relative dates step by step.
- **Per-question failure logging** (question / gold / prediction / verdict)
  in the output JSON, so a miss can be attributed to reader vs retrieval vs
  judge.

EVAL-harness only - no product / recall code is touched, so recall@10 and
latency are unchanged. The J figure is still measured on small samples; a
full multi-conversation run (with a fast LLM backend) is needed for a
publishable number.

### Local-use & own-your-data commands

New CLI surface for using PMB as a personal memory you fully own and organize
offline. All of these are CLI + display + write-layer only - **none touch the
recall hot path**, so the 94.5% LoCoMo recall@10 and ~70ms p50 are unchanged
(verified: full hardening regression suite green).

- **`pmb timeline`** - chronological, day-grouped view of your memory
  (`--days`, `--type`, `--newest-first`).
- **`pmb insights`** - personal analytics: totals, type breakdown, growth per
  week, top topics (entity graph), and lessons/failures/goals/pinned counts.
- **`pmb digest [today|week|month]`** - quick recap of recent memories
  (`--days N`).
- **`pmb export [--format markdown|json] [--out FILE]`** - dump all memory to
  readable Markdown or JSON (`--include-archived`). Plain/unencrypted; for an
  encrypted portable bundle use `pmb workspace export`.
- **`pmb forget-topic <topic>`** - archive every memory about a topic in one
  command (`--dry-run`, `--yes`, `--in content|tag|source`). Reversible
  (archived, not deleted).
- **TTL / expiry** - `pmb ttl <ulid> 30d` (or `clear`), a `--ttl` option on
  `note` / `learn` / `fact`, and `pmb prune-expired` to sweep. Enforced only by
  the explicit sweep, never inside recall.
- **Tags / collections** - `pmb tag`, `pmb untag`, `pmb tags`, and
  `pmb tagged <tag>` for local organization.
- **`pmb reminders`** - surfaces open goals that are overdue or due soon
  (`--within N`, `--all`).
- **`pmb snapshot create|list|restore`** - local, offline, timestamped
  workspace snapshots (WAL-checkpointed copy; restore auto-backs-up current
  state first).

New store helpers: `EventStore.set_metadata` (annotate an event without
touching its content or embeddings) and `EventStore.list_all` (export /
analytics, optionally including archived rows). 34 new tests in
`tests/test_local_features.py`.

### Hardening pass 2 - lazy LanceDB + use-case clarity

- **`pmb stats` and friends now run in ~1 s (was ~14 s).** The single biggest user-facing cost was `import lancedb` itself: on Windows it pulls in pyarrow + torch and takes ~22 s to import. `HybridSearch.__init__` triggered that import for every CLI command, even ones that never touch vectors. Fix: `HybridSearch` defers `lancedb.connect(...)` and the table-open to a lazy `_table` property; `Engine.__init__` no longer eagerly calls `reload_bm25()`. The cost is now paid on the first `recall()` or write that actually needs vector search.
- **README "What gets stored, when" gets a `Which features help which use case` table.** Honest mapping of feature flags to workloads (multi-hop, narrative, temporal, code, multilingual, etc.) - so contributors don't assume every feature must boost LoCoMo recall.
- **All 88 core tests + 9 smoke tests still pass.**

### Hardening pass 1 - import hygiene + test foundation

- **Lazy top-level imports.** `import pmb` no longer pulls in LanceDB, sentence-transformers, numpy, rank_bm25, fastmcp, yaml, torch or transformers. Heavy attributes (`Engine`, `Workspace`, `detect_workspace`) are exposed via PEP 562 `__getattr__` and loaded on first access. Measured: bare `import pmb` is now **2.4 ms** (was ~14 s when the engine was eagerly imported). Same treatment applied to `pmb.core`.
- **`tests/conftest.py`** added with shared fixtures (`tmp_pmb_home`, `tmp_workspace_dir`, `isolated_engine`) and a `sys.path` fallback so new tests don't need the boilerplate.
- **`tests/test_lightweight_imports.py`** added - 9 smoke tests that spawn fresh interpreters and assert which heavy modules are loaded after specific imports. If a future change re-introduces an eager heavy import at `pmb/__init__.py` it will fail this suite.
- **No behaviour change.** All 88 core tests (the `make test` set used in CI) still pass; `from pmb import Engine` still works.

### Recall ablation findings + default changes

Ran a full-system ablation across 19 retrieval components on LoCoMo conv-26/30/41 (see `scripts/benchmarks/ablation_full.py`). Key findings:

- **`recall.typo_correction = True` was hurting recall by ~6.2 points.** The Levenshtein-≤2 fuzzy rewriter was "correcting" correctly-spelled tokens into similar-looking but wrong entity names. **Default flipped to `False`.** Existing per-workspace overrides are unaffected.
- **BM25-heavy fusion outperformed the symmetric default.** `recall.bm25_weight` raised `0.5 -> 0.7`. Vector-only retrieval loses 18 points on this dataset; the embedding channel adds only marginal signal.
- **Net effect on the full 10-conversation LoCoMo benchmark: 91.6 % -> 94.1 % evidence-recall@10** (+2.5 pp, every conversation improved, range now 91.2-96.2 %).
- **Twelve of nineteen ablated features show 0.000 delta on LoCoMo:** tiers, causation walk, arc expansion, collapse-reflections, reflection-to-edges, predictive cache, code-AST, LRU cache, multi-entity bonus, PPR, adaptive routing, spreading activation, temporal. These are not removed - some are designed for long-term dynamics that LoCoMo doesn't probe - but the README and docs no longer claim they carry the benchmark number.
- **Cross-encoder reranker (`recall.rerank = True`) regresses 17 points and adds 845 ms p50.** Recommendation removed from default setup; flag is still available as experimental.

See `scripts/benchmarks/ablation_full.py` and `docs/HARDENING_NOTES.md` for the raw data and methodology.

## [0.1.0] - Initial public release

### Highlights

- Single-file install: `pip install -e .` exposes `pmb` on `$PATH`.
- 91.6 % evidence-recall@10 on LoCoMo full 10-conversation run (vs published mem0 / Letta / Zep numbers of 70-80 %).
- 90-140 ms p50 recall latency, 2 ms async writes.
- 13 semantic layers, 5 access paths, 3 tiers - every layer optional and configurable.
- Web dashboard + 5-tab terminal TUI for inspection.
- MCP server for Claude Code, Codex CLI, Cursor.
- Optional Ollama integration for fully-local LLM ops (consolidation, dedup verify, pmb-chat).

### Architecture decisions worth knowing

- **Local-first.** SQLite + LanceDB on disk; no daemon, no service, no network.
- **Lazy-by-default agent prompt.** PMB is OFF until an explicit trigger ("remember", personal fact, "what did I…"). General Q&A bypasses PMB entirely.
- **Async writes (fire-and-forget MCP).** `record_batch` returns in ~2 ms; embedding + LanceDB indexing happen in a background thread.
- **BM25 fallback for cold reads.** First `recall` after process start returns text-match results in ~100 ms while the sentence-transformers model finishes loading.
- **Multi-layer dedup.** L1 exact (5 ms) + L2 cosine ≥ 0.92 (50 ms) at write time; L2.5 borderline queue for LLM-verified merges; L3 dashboard review tab for ambiguous pairs.
- **Conservative defaults.** False-merge risk minimised: never merges across event types, threshold 0.92 leaves a safe gap.

### Tools and surfaces

- `pmb tui` - five tabs: Memory, Recall, Stats, Dedup, Tune
- `pmb dashboard` - HTTP dashboard on :8765 with Graph, Events, Performance, Duplicates, Recall Debug
- `pmb tune` - settings-only TUI (67 knobs)
- `pmb ollama` - health, install/use models, smoke test
- `pmb connect codex | claude | cursor` - wires MCP into the agent's config and rules file
- `pmb dedupe` / `pmb regraph` / `pmb prune-graph` - maintenance ops

### Settings (67 across 9 categories)

`recall` (36), `consolidate` (9), `dedup` (6), `chat` (5), `decay` (3), `embedding` (3), `feedback` (2), `ollama` (2), `mcp` (1). All exposed in TUI Tune tab, CLI `pmb config`, and dashboard.

### Known limits

- Single-machine. No cross-device sync.
- Cold first recall after process start blocks on model load if the BM25 fallback path is disabled.
- Code-AST extraction is Python-only (regex fallback for broken / partial code).
- Dashboard and TUI assume one workspace at a time.
