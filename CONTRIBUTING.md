# Contributing to PMB

Thanks for your interest. PMB is intentionally small and opinionated; here is what helps and what doesn't.

## Development setup

```bash
git clone https://github.com/oleksiijko/pmb.git
cd pmb
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -c requirements-dev.lock -e ".[dev,crypto]"
python scripts/prewarm_models.py
```

The prewarm step downloads the essential multilingual embedder once. It fails
clearly if that model is unavailable; optional CLIP/reranker downloads are
best-effort. After prewarming, pytest loads models offline. A clean checkout
must run this step before running model-dependent tests.

## Running tests

| Command | Coverage |
|---|---|
| `make test` or `bash scripts/test.sh` | Full blocking suite, including property and MCP integration tests |
| `make test-core` | Smaller engine/security edit loop; not a release gate |
| `make test-smoke` | Lightweight-import regression tests |
| `python -m pytest tests/ -q -m quarantined` | Explicitly quarantined scenarios, also visible in CI |
| `make lint` | Ruff over source, tests, and scripts |

CI runs Python 3.11–3.13 on Linux and Windows, plus Python 3.12 on macOS.
Numerical/timing tests marked `platform_sensitive` gate on Linux; they are
excluded from the Windows/macOS matrix. The local default runs them too.
Whole-package coverage must remain at least 61%. Tests use temporary PMB homes
and Git settings; they must not modify the contributor's memory or identity.

`requirements-dev.lock` pins the dependency set used by CI and development
without restricting downstream users to exact versions. To refresh it deliberately:

```bash
uv pip compile pyproject.toml --extra dev --extra crypto --universal --output-file requirements-dev.lock
pip install -c requirements-dev.lock -e ".[dev,crypto]"
python scripts/prewarm_models.py
make test
make lint
```

Review the dependency diff and full CI matrix before merging an update.
There is no configured static type checker yet; syntax compilation and Ruff
are not a substitute for one.

## Project layout

```
src/pmb/
  core/             - engine, events, search, workspace, recall_cache
  graph/            - entities, persons, store (SQLite-backed)
  reasoning/        - facts, reflect, causation, arcs, temporal, dedup, typo_fix
  mcp/              - FastMCP server, perf tracking, tools schema
  dashboard/        - local web UI (HTTP, no framework)
  cli/              - typer entry points (main, ollama_cmd, connect)
  agent_wrapper/    - pmb-chat (optional standalone chat loop)
  health/           - consolidation, doctor checks
  eval/             - LoCoMo judge helpers
tests/              - pytest, grouped by subsystem (lang/recall/engine/hooks/…)
scripts/            - benchmarks, demos, profilers
```

## Where to add things

| You want to… | Touch this |
|---|---|
| add a recall ranking signal | `core/engine.py` (search the `recall(` method, ~12 stages) |
| change a tunable knob | `config.py` (single SCHEMA dict) |
| add an MCP tool | `mcp/server.py` (decorate with `@mcp.tool()`) |
| add a CLI command | `cli/main.py` |
| add a dashboard tab | `dashboard/static/index.html` + a handler in `dashboard/server.py` |

## Code style

- Keep modules focused. The recall pipeline is already large; do not add new top-level layers without a clear reason.
- Public methods on `Engine` should be readable from a docstring alone.
- All write-path additions must be sub-100 ms on warm cache. Profile before merging if you add embedding work.
- Pure-Python where possible. PyTorch is fine (already a dep); avoid adding new heavy dependencies.

## Tests

- Tests are grouped by subsystem under `tests/`: `lang/`, `recall/`, `engine/`, `hooks/`, `mcp/`, `ingest/`, `maintenance/`, `security/`, `cli/`, `integration/`, `eval/`, `meta/`. Put a new test in the folder matching what it exercises; frozen baselines live in `tests/fixtures/`.
- Tests use temp workspaces (`tempfile.mkdtemp`); don't write to `~/.pmb/` from a test.
- For features that touch recall scoring, add a test in `tests/engine/test_graph.py` style that asserts ordering, not exact scores.
- The full LoCoMo bench (`scripts/benchmark_locomo.py`) is the integration test for retrieval quality.

## Pull requests

- One concern per PR.
- Include a short description of what changed and **a benchmark line** if recall accuracy or latency could be affected.
- Updating the README's benchmark numbers is fine when you have new data - link to the run that produced them.

## Hardening passes (style for cross-cutting refactors)

If you are doing a sweeping technical-debt pass (lazy imports, exception handling, type annotations, etc.) please follow this protocol so it stays reviewable:

1. **Phase 0 - inventory first.** Before changing anything, post the current state to the PR description: file/line numbers, what each call does, why it exists. For exception-handler changes specifically, see `docs/HARDENING_NOTES.md` for the categorisation we want to preserve.
2. **One phase per commit.** Lazy imports, conftest, smoke tests, exception logging - each is its own commit so we can bisect a regression.
3. **Run `make test` after every commit.** Not `make test-all-WARN`.
4. **Behaviour-preserving by default.** If a change alters runtime behaviour (e.g. converting a silent fallback into an exception), call it out explicitly and add a test.
5. **Update CHANGELOG.md.** Hardening counts as a real change; document the measurable effect (`import pmb` time, test count, etc.).

## What we are not looking for

- Multi-user, multi-device, cloud sync. PMB is single-user single-machine on purpose.
- Frontend frameworks. The dashboard is plain HTML/JS. Keep it that way.
- New embedding backends. We have sentence-transformers (default) and fastembed (optional). Adding a third needs a strong case.

## Filing issues

Useful issues include: a minimal repro (or a workspace dump), the version of PMB (`pip show pmb`), and what command/agent triggered it. "It feels slow" without timing data is harder to act on - the dashboard's Performance tab (`pmb dashboard`) shows the real numbers.

## Licensing of contributions

PMB is licensed under **Apache License 2.0** (see [`LICENSE`](LICENSE)).

By submitting a pull request, you agree that your contribution is licensed under the same terms - this is the default behaviour spelled out in Apache 2.0 §5 ("Submission of Contributions"). No separate CLA, no sign-off chain. Just open the PR.

If your contribution includes code or assets you did not write, list their origin and license in the PR description so we can add them to [`NOTICE`](NOTICE).
