# Ollama

This guide is for anyone who wants PMB to run **completely offline**: no Anthropic key, no OpenAI key, nothing sent to the cloud. The vector embedder is local (sentence-transformers). The optional LLM operations (consolidation, dedup verification, `pmb-chat`) go through Ollama running on the same machine.

## What you need

- A machine with at least **8 GB RAM free** (16 GB recommended for the balanced model).
- Python 3.12 (3.11 works).
- ~10 GB free disk for one Ollama model plus PMB's embedder.

That's it. No accounts, no API keys.

## Step 1 - install Ollama

Linux / macOS:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Windows: download the installer from <https://ollama.com/download>.

Start it:

```bash
ollama serve              # runs in foreground; use & to background it on Unix
```

If you're on Windows the installer registers a service, so `ollama serve` may already be running. Check with `curl http://localhost:11434/api/tags` - anything but a connection error means it's up.

## Step 2 - pull a model

PMB needs one Ollama model for LLM operations. Pick by RAM budget:

| Preset | Model | Disk | RAM during inference | Use |
|---|---|---|---|---|
| tiny     | `gemma3:1b`   | ~1 GB | ~2 GB | older laptops, very fast |
| small    | `llama3.2:3b` | ~2 GB | ~3 GB | fast, OK quality |
| **balanced** | **`llama3.1:8b`** | ~5 GB | ~8 GB | **recommended default** |
| quality  | `qwen2.5:14b` | ~9 GB | ~12 GB | best dedup/consolidation accuracy |

```bash
ollama pull llama3.1:8b
```

(Replace with another tag if you chose a different preset.)

## Step 3 - install PMB

```bash
git clone <repo-url> pmb
cd pmb
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -e .
```

## Step 4 - point PMB at Ollama

```bash
pmb ollama use balanced
```

This writes to `~/.pmb/config.yaml`:

```yaml
ollama:
  model: llama3.1:8b
consolidate:
  backend: ollama
chat:
  transport: ollama
```

Verify everything is wired correctly:

```bash
pmb ollama status
```

You should see "Status: online", the model in the installed list, and a check mark next to each PMB operation that will use Ollama.

Optional 1-shot smoke test:

```bash
pmb ollama test
```

Asks the model to reply "PONG". If you see PONG-ish text in under ~30 s, you're good.

## Step 5 - hook up your AI agent

The agent itself (Claude Code / Codex CLI / Cursor) still uses its own LLM - PMB is *memory*, not the agent's brain. PMB only uses Ollama internally for its own sleep-mode operations.

```bash
pmb connect codex     # or claude / cursor
```

Restart the agent. From now on, `record_batch`, `recall`, `pin`, etc. are available as MCP tools and PMB's memory is persistent across sessions.

## What runs where

| Operation | Where it runs | Talks to |
|---|---|---|
| Embedding (sentence-transformers) | your machine | nothing |
| Vector search (LanceDB), BM25, graph | your machine | nothing |
| `record_batch`, `recall`, `pin` (MCP) | your machine | nothing |
| Dedup L1+L2 (exact + cosine) | your machine | nothing |
| Dedup L2.5 (LLM verify, optional) | your machine | Ollama (localhost:11434) |
| Consolidation (LLM sleep ops) | your machine | Ollama |
| `pmb-chat` (optional standalone chat) | your machine | Ollama |
| The AI agent itself (Claude / Codex / Cursor) | depends on the agent | the agent's provider |

PMB itself is offline. The agent has its own networking.

## Troubleshooting

**`pmb ollama status` says "not reachable"**
Make sure `ollama serve` is running. If you set a custom address, point PMB at it:

```bash
export PMB_OLLAMA_URL=http://192.168.1.10:11434   # or wherever
pmb ollama status
```

**Dedup borderline queue stays full**
You haven't drained it. Run:

```bash
pmb dedupe --run-pending --backend ollama
```

This iterates over `dedup_pending` rows and asks the model "are these the same fact?", merges yes-cases automatically.

**Model is too slow**
Drop to a smaller preset:

```bash
pmb ollama use small      # llama3.2:3b
# or
pmb ollama use tiny       # gemma3:1b
```

**Different host (you run Ollama on another box)**
Set the URL once globally:

```bash
pmb config set ollama.url http://192.168.1.10:11434
```

PMB will use it for every subsequent operation.

## Switching back to Anthropic / OpenAI later

```bash
pmb config set consolidate.backend anthropic
pmb config set chat.transport anthropic
export ANTHROPIC_API_KEY=...

# or
pmb config set consolidate.backend openai
pmb config set chat.transport openai
export OPENAI_API_KEY=...
```

Your stored memory doesn't change - only the LLM provider for sleep-mode ops.

## Updating the Ollama model

```bash
ollama pull llama3.1:8b      # re-pulls latest of same tag
# or
ollama pull llama3.2:3b      # different model
pmb ollama use llama3.2:3b   # tell PMB about the new one
```

## Model and endpoint precedence

Maintenance clients use explicit model/URL arguments first, then
`PMB_OLLAMA_MODEL` / `PMB_OLLAMA_URL` (or `OLLAMA_HOST` for the URL), then
workspace/global `ollama.model` and `ollama.url`, then built-in defaults.
`pmb ollama status` and `pmb ollama test` use the same settings.
`pmb consolidate` honors `consolidate.backend` unless `--backend` is supplied;
`--backend auto` explicitly requests auto detection.

```bash
pmb ollama use llama3.2:3b --for consolidate
pmb config set ollama.url http://localhost:11434
pmb consolidate --dry-run
```

An HTTP 404 from a running Ollama server can mean the selected model is missing.
Check `ollama list` and pull that exact model; it is distinct from a connection
failure. Embedding-model settings remain separate from generation-model settings.
