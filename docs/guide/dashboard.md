# Dashboard

`pmb dashboard` opens a local web UI for browsing your memory. It is read mostly,
runs on your machine, and binds to `127.0.0.1` only, so nothing is exposed to the
network.

## Launch

```bash
pmb dashboard                 # serves at http://127.0.0.1:8765 by default
pmb dashboard --port 18888    # use another port
```

The default port is 8765, which is also the default for the shared daemon. If
the default port is unavailable, the operating system assigns a free port.
PMB prints the bound URL and uses it for `--open`. If you pass a fixed
`--port`, PMB prints a short diagnostic if the OS refuses the bind.

## Tabs

- **Map**: the entity graph, drawn live. Nodes are entities (people, files,
  tools, topics), edges are co-occurrence in the same memory. Click a node to see
  the events linked to it.
- **Timeline**: your memory by day, grouped by project, like a commit graph.
- **Overview**: a structured "what do I know about this" summary.
- **Entities**: the most mentioned entities, filterable by kind.
- **Arcs**: narrative threads that connect related events over time.
- **Lessons**: the procedural memory, with each rule's follow rate and detection
  of lessons that never fire.
- **Adherence**: how well the agent reads before it writes, with a daily chart.
- **Duplicates**: near duplicate candidates with inline merge.
- **Performance**: per tool latency, so you can spot a regression early.
- **Recall**: a debugging view of the ranker for a query you type.

## Deleting from the dashboard

Click any memory to open its detail panel. At the bottom you have:

- **Pin**: keep it at maximum importance, protected from automatic cleanup.
- **Archive**: hide it reversibly. Restore it later with `pmb restore`.
- **Delete** (the red button): remove it permanently, with a confirmation first.

After you act, the panel closes and the current view refreshes. For the full
picture of soft versus hard deletion, see
[Deleting memories](deleting-memories.md).
