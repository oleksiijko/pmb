# Project assessment and release direction

Assessment date: 2026-09-23. Baseline: `133f3a3`, version 1.2.3.

## Product and architecture

PMB is persistent local memory for coding agents: SQLite holds durable events,
LanceDB supplies semantic candidates, BM25 supplies lexical candidates, and the
Engine combines these with graph, temporal, lesson, and relevance signals. The
CLI, MCP server, warm daemon, lifecycle hooks, and vanilla-JavaScript dashboard
are frontends to that engine. Optional LLM work runs during maintenance or
explicit operations; it is not required for ordinary recall.

The core engine is already divided into mixins under `src/pmb/core/engine/`.
Existing configuration, durable queues, registry, workspace detection, and search
APIs should be extended rather than replaced. No frontend framework migration,
new embedding backend, or cloud service is needed for this release.

## Confirmed problems

| Finding | Evidence | Resolution in 1.3 |
|---|---|---|
| Red CI | Run 32685507164: unused import and Windows consolidation dry-run failure | Fix lint; seed deterministic vectors in the write-isolation test while retaining real semantic clustering tests |
| Native crash on macOS | Full baseline suite crashed during simultaneous model inference in recall/write and background embedding | Serialize shared embedding inference; exercise concurrent calls across eight workspaces |
| Ollama selected model ignored | Issue #81; `ollama use` writes config but the maintenance client read only environment/defaults | Shared client reads saved model/URL, auto probes the same endpoint, CLI honors configured backend |
| In-flight embedding missed | Consolidation watched only an in-memory list, whose item is removed before embedding finishes | Wait on the existing durable queue; test its last in-flight item |
| Unreliable recovery copy | Snapshot code checkpointed best-effort then copied SQLite/WAL files independently | Online SQLite backup, file hashes, integrity check, staged publication and verified recovery |
| Tests modify developer identity | `test_git_sync.env` invoked `git config --global` against the real user file | Isolate global Git config in the temporary test directory |
| Cold-write test intercepts unrelated engines | Linux CI exposed background model loads counted by a process-wide test trap | Scope the trap to the tested engine and remove its platform exclusion |
| Test entry points disagree | CONTRIBUTING describes an obsolete 88-test subset; shell script omits property/MCP suites | Document prewarming and run the complete blocking suite from every default entry point |
| Distribution versions drift | MCP manifest still said 1.2.2; citation said 0.2.1 | Align versions and add a cross-distribution regression check |

## Release: 1.3.0 — Reliable local memory

The user-facing addition is verifiable backup and recovery. It complements the
local-first product: users can inspect, validate, and recover memory before
maintenance. Fixing model selection and native concurrency makes the existing
features dependable. This is a feature release without intentionally breaking
existing APIs, so 1.3 is more appropriate than a cosmetic 2.0.

New commands: `pmb snapshot verify <id>` and `pmb snapshot list --json`.
Creation publishes only complete snapshots. Restore rejects damaged or foreign
inputs, retains a verified safety snapshot, removes stale files, and rolls back
file moves on ordinary I/O errors. See [backup limits](guide/backups.md).

## Follow-up priorities

1. Measure recall quality and latency on representative personal workspaces,
   with particular attention to CJK and cross-language top-1. The existing
   multilingual evals are the baseline; do not promote extraction defaults or
   replace the embedder based on unmeasured claims.
2. Centralize process ownership and lifecycle so all writers—not only registered
   MCP/daemon processes—participate in maintenance locks. Current restore still
   requires the user to stop dashboards and direct writers.
3. Add static typing incrementally at external input, storage, and queue
   boundaries. There is no configured type checker; a wholesale annotation
   rewrite would obscure functional defects.
4. Extend transactional backup semantics to git sync and encrypted exports.
   This release changes snapshots; those separate export paths still deserve
   their own concurrency and recovery tests.
5. Consider dashboard backup management only after the recovery API is settled.
   Keep the current vanilla frontend and show real states and failures.

## Validation scope

Local verification uses Python 3.12 on macOS, real cached multilingual models,
Ruff, syntax compilation, full pytest, and the existing 61% coverage floor.
New tests cover WAL backup, corrupt/missing/extra files, path traversal, symlinks,
recovery rollback, cancellation, live-server refusal, backend configuration,
HTTP failures, malformed model output, and concurrent embedding calls.

The independent autoreview preflight cannot proceed because TruffleHog is not
installed. A local adversarial review is performed; this is not represented as
an independent clean verdict. Linux/Windows and the other supported Python
versions must be checked by the PR's CI matrix before publication.

Initial post-fix full run: 1593 passed. Final functional run: 1630 passed,
3 skipped, 1 quarantined test deselected; the quarantined test also passes when
run separately. Final coverage: 67.28% (1630 tests). The built wheel was installed in a separate Python 3.14
environment and exercised through real CLI subprocesses: help, stats, config,
doctor, writes, snapshot creation, verification, and restore. This smoke test
does not replace the supported Python-version CI matrix.
