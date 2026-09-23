# Core engine

This page describes the part of PMB that actually stores, ranks, and serves
memory. It is the implementation-level companion to [Architecture](architecture.md)
and [How it works](how-it-works.md).

## What the Engine owns

`src/pmb/core/engine/base.py` builds one `Engine` per workspace. The class is
composed from focused mixins instead of one giant file, but the object owns the
same runtime resources:

``` mermaid
flowchart LR
  Host["Agent host"] --> Engine["Engine"]
  Engine --> Config["Config"]
  Engine --> Stores["Stores"]
  Engine --> Runtime["Runtime"]
  Engine --> Surfaces["Surfaces"]
  Stores --> Recall["Recall"]
  Runtime --> Recall
  Surfaces --> Recall
```

The constructor does the minimum needed to make commands responsive: it opens
SQLite immediately, but BM25, LanceDB, embedding models, rerankers, and graph
boost caches are lazy where possible. That is why commands such as `pmb stats`
and `pmb config` do not pay the vector-store cold start.

<div class="pmb-widget-grid" markdown>
<div markdown>
<strong>Config</strong>
<span>Workspace detection, layered settings, model choice, and feature gates.</span>
</div>
<div markdown>
<strong>Stores</strong>
<span>SQLite for truth, LanceDB for vectors, BM25 for exact terms, graph tables for links.</span>
</div>
<div markdown>
<strong>Runtime</strong>
<span>Session tracker, recall cache, write outbox, embed queue, and touch buffer.</span>
</div>
<div markdown>
<strong>Surfaces</strong>
<span>Write, batch, recall, lessons, overview, goals, dedup, health, and ambient APIs.</span>
</div>
</div>

## Boot sequence

``` mermaid
sequenceDiagram
  autonumber
  participant Caller
  participant Engine
  participant Workspace
  participant SQLite as events.sqlite
  participant Search as HybridSearch
  participant Graph as GraphStore

  Caller->>Engine: create Engine()
  Engine->>Workspace: detect workspace
  Workspace-->>Engine: id, root, storage paths
  Engine->>SQLite: open EventStore and run migrations
  Engine->>Search: configure backend and BM25 weight
  Engine->>Graph: create graph tables in same SQLite file
  Engine->>Engine: initialize caches and background queues
  Engine-->>Caller: ready for CLI, MCP, hooks, or dashboard
```

Workspace detection is deterministic. PMB checks explicit arguments, then
`PMB_WORKSPACE`, then `.pmb/workspace.yaml`, persisted defaults, git remote,
git root path, and finally the current directory path. Storage lands in
`~/.pmb/workspaces/<workspace-id>/`.

## Storage schema

PMB uses SQLite as the source of truth and LanceDB as the vector side index.
The active SQLite file is `events.sqlite`; the vector directory is beside it.

| Store | Table or file | Purpose |
|---|---|---|
| SQLite | `events` | Main memory log: ULID, workspace, type, content, metadata JSON, timestamp, importance, access counters, archive marker, session id, and memory tier. |
| SQLite | `migrations` | Tracks applied schema versions. The current event-store constant is `SCHEMA_VERSION = 6`; newer additive tables are created with `IF NOT EXISTS`. |
| SQLite | `event_edges` | Direct event-to-event reasoning edges such as cause, support, conflict, or derived links. |
| SQLite | `arcs`, `arc_events` | Narrative threads built from related events. |
| SQLite | `predictive_cache` | Sleep-stage cache for likely future queries and their precomputed top results. |
| SQLite | `lesson_surfaces` | Audit trail of which lessons were shown to an agent and whether the agent later marked them followed. |
| SQLite | `write_outbox` | Durable queue for async `record_batch` writes. A crash after acceptance can be replayed. |
| SQLite | `error_log` | Swallowed background errors surfaced later by diagnostics. |
| SQLite | `embed_queue_pending` | Durable embedding queue with retry and dead-letter status. |
| SQLite | `graph_entities` | Canonical entities extracted from memory text, keyed by workspace, kind, and name. |
| SQLite | `graph_event_entities` | Many-to-many links from events to entities. |
| SQLite | `graph_edges` | Weighted undirected co-mention edges between entities. |
| SQLite | `mcp_calls` | Best-effort MCP latency and error telemetry for dashboard and diagnostics. |
| SQLite | `dedup_pending` | Borderline duplicate pairs awaiting optional LLM verification. |
| LanceDB | `events` table | Vector rows with `ulid`, `vector`, and `text`. |
| File cache | `bm25_index.pkl` | Cached BM25 token index and ULID list, rebuilt when missing or stale. |
| File cache | `vocab_bridges.json` | Workspace-mined vocabulary bridges used by the recall reranking layer. |

Soft deletion sets `archived_at`; hard deletion removes the event row, vector,
graph links, and related indexes where applicable.

## Write path in detail

``` mermaid
flowchart LR
  Tool["record_*"] --> Normalize["Normalize"]
  Normalize --> Event[("events")]
  Normalize --> Outbox["Outbox"]
  Event --> Embed["Embed queue"]
  Event --> Dedup["Dedup"]
  Event --> Entities["Entities"]
  Embed --> Vectors[("vectors")]
  Entities --> Graph[("graph")]
  Event --> Ready["Recall-ready"]
  Vectors --> Ready
  Graph --> Ready
```

Normal writes are durable first: the event lands in SQLite before optional
background work can matter. Embeddings, graph extraction, dedup verification,
and ambient bookkeeping are allowed to lag, but they have retry paths or safe
fallbacks. The recall path can still find a freshly written event through the
SQLite and BM25 side even when the embedder is still warming.

## Recall path in detail

``` mermaid
flowchart LR
  Query["Query"] --> Worthy{"Worth recalling?"}
  Worthy -->|No| Empty["Quiet"]
  Worthy -->|Yes| Candidates["Candidates"]
  Candidates --> BM25["BM25"]
  Candidates --> Vectors["Vectors"]
  BM25 --> Merge["Fusion"]
  Vectors --> Merge
  Merge --> Rank["Rank"]
  Rank --> Gates["Gates"]
  Gates --> Context["Context"]
```

The hot path does not call an LLM. Optional LLM work happens at write time,
maintenance time, or for explicit commands such as `pmb consolidate`,
`pmb reflect`, `pmb distill`, and `pmb dedupe --run-pending`.

## Concurrency and durability

PMB is designed for several local clients touching one workspace at once.

<div class="pmb-note-grid" markdown>
<div markdown>
<strong>SQLite pragmas</strong>
<span>Connections use WAL and busy timeouts so dashboard, MCP, hooks, and CLI commands can coexist.</span>
</div>
<div markdown>
<strong>Single warm runtime</strong>
<span>The daemon keeps one warm Engine, model, and vector store instead of loading them per agent.</span>
</div>
<div markdown>
<strong>Crash-safe queues</strong>
<span>Async batch writes and pending embeddings have durable tables that can replay after restart.</span>
</div>
<div markdown>
<strong>Buffered touches</strong>
<span>Recall access-count updates are coalesced so parallel recalls do not fight over SQLite writes.</span>
</div>
</div>

## Local runtime versus team runtime

| Mode | Transport | What runs |
|---|---|---|
| Local default | Local daemon plus HTTP, with stdio proxy where needed | One warm runtime on your machine. Claude Code and Cursor can point at the daemon directly; Codex uses `pmb mcp proxy`. |
| Local stdio fallback | stdio | One per-client server process. Useful for compatibility, but it can pay more cold-start cost. |
| Team or multi-machine | streamable HTTP | `pmb mcp serve --transport streamable-http --host 0.0.0.0 --bearer-token <secret>` exposes one shared server to remote agents. |

The team mode is the only mode that should listen beyond localhost. Use a bearer
token, and put it behind a private network such as Tailscale or an SSH tunnel.

## Code map

| Concern | Source |
|---|---|
| Engine composition | `src/pmb/core/engine/base.py` |
| Write, batch, recall, lessons, goals, health | `src/pmb/core/engine/` |
| Event schema and archive/delete lifecycle | `src/pmb/core/events.py` |
| BM25, LanceDB, embedding backends | `src/pmb/core/search.py` |
| Durable embed queue | `src/pmb/core/embed_queue.py` |
| Workspace detection and storage paths | `src/pmb/core/workspace.py` |
| Entity graph storage | `src/pmb/graph/store.py` |
| MCP tools and server | `src/pmb/mcp/tools.py`, `src/pmb/mcp/server.py` |
| Warm daemon and proxy | `src/pmb/mcp/daemon.py`, `src/pmb/cli/commands/hooks.py` |
| CLI command groups | `src/pmb/cli/commands/` |

## Snapshot and maintenance APIs

- `core.snapshots.create_snapshot(workspace, note)` publishes a verified local
  snapshot after SQLite backup and file hashing finish.
- `core.snapshots.verify_snapshot(path, workspace_id)` checks the manifest,
  workspace, files, and SQLite integrity; legacy snapshots lack file hashes.
- `core.snapshots.restore_snapshot(workspace, id)` validates and stages incoming
  files, creates a safety snapshot, replaces workspace files, and rolls back
  file moves on an I/O error. Callers must stop all writers first.
- `core.snapshots.snapshot_path(workspace, id)` restricts user-supplied IDs to
  direct, non-symlink snapshot directories.
- `health.consolidate.OllamaClient` resolves saved model/URL settings and owns
  generation response validation. `resolve_llm_client` probes that same URL.
- Consolidation waits for the existing durable embedding queue, including its
  in-flight item, before reading vectors. Shared model inference is serialized
  by the process-wide model cache lock across workspaces.
