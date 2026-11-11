# Team and remote

> **You don't need this for local use.** The default `pmb connect <agent>`
> keeps PMB local. Supported local clients use one warm daemon on
> `127.0.0.1`; stdio-only clients such as Codex reach it through
> `pmb mcp proxy`. This page covers shared memory and custom HTTP clients.

PMB's memory is a SQLite file plus a vector index. By default it lives next
to you and only your local agent talks to it. If you want **one shared
memory** - a team on the same workspace, or your own laptop + desktop -
you run the MCP server once over HTTP and point every agent at its URL.

## Local versus shared

| Mode | Use it when | Transport |
|---|---|---|
| Local default | One developer on one machine, even with several agents | Local daemon on `127.0.0.1`; Codex uses `pmb mcp proxy` to bridge stdio to the daemon. |
| Local stdio fallback | A host cannot use the daemon/proxy path | One MCP server process per client. |
| Team / multi-machine | Several machines or people need one memory | One `pmb mcp serve` process over streamable HTTP with a bearer token. |

## When you'd want it

| Situation | Why HTTP |
|---|---|
| A team of 5-15 on one workspace | One person records "don't touch X", everyone sees it |
| Your own multiple machines | Work laptop + home desktop on one memory |
| Memory on a server / NAS | One persistent process; agents connect from anywhere |

If none of these apply, stop here and use the normal local daemon/proxy setup.

## Run the shared server

One persistent process, living next to the SQLite file (a homelab box, a
Tailscale node, a small VM):

```bash
pmb mcp serve --transport streamable-http --host 0.0.0.0 --port 8765 \
              --bearer-token "$(openssl rand -hex 32)"
```

Then on each developer's machine:

```bash
pmb connect claude-code --remote http://memo.local:8765/mcp \
                        --bearer-token <same secret>
```

Same MCP, same tools, one shared memory.

## Connect a local assistant

For a voice assistant or another custom client on the same machine, start a
server on loopback. Set the same `PMB_MCP_BEARER_TOKEN` in both processes.

```bash
pmb mcp serve --host 127.0.0.1 --port 8875 --workspace assistant
```

Store one fact per item after the assistant decides what should be remembered.
Use a stable entity ID across languages and resolve aliases in the assistant.
The metadata keys below are a convention for your integration. PMB stores
them alongside the fact, and `importance` controls the fact's priority.

```python
import asyncio
import os

from fastmcp import Client


async def remember() -> None:
    async with Client(
        "http://127.0.0.1:8875/mcp", auth=os.environ["PMB_MCP_BEARER_TOKEN"]
    ) as client:
        await client.call_tool("record_batch", {"items": [{
            "type": "fact",
            "content": "The user prefers quiet reminders in the evening.",
            "importance": 0.8,
            "metadata": {
                "kind": "preference",
                "entity": "user",
                "attribute": "reminder_style",
                "source": "voice",
                "chat_id": "example-session",
                "confidence": 0.95,
            },
        }]})


asyncio.run(remember())
```

## Why the bearer token is required

Over local stdio there is no network surface. Over the local daemon, PMB keeps
the server bound to `127.0.0.1` and uses its own local token/proxy wiring.

Over **HTTP** the moment you open a port, anyone who can reach it can read
and write your memory. The bearer token is the only thing separating "my
agent" from "a random host on the network / mesh". So in HTTP mode it isn't
optional hardening - it's the boundary.

PMB does a constant-time comparison (`hmac.compare_digest`) so a leaked log
line can't side-channel a partial match. CORS preflights (`OPTIONS`) and the
health endpoint (`/healthz`, `/`) pass through unauthenticated; everything
else needs `Authorization: Bearer <token>`.

## Config via environment

`pmb mcp serve` sets these env vars before handing off to the MCP server. If
you run `pmb-mcp` directly from a systemd unit or Docker image, set them
yourself:

| Env | Default | Meaning |
|---|---|---|
| `PMB_MCP_TRANSPORT` | `stdio` in `pmb-mcp`; `pmb mcp serve` sets `streamable-http` by default | `streamable-http` for the shared server |
| `PMB_MCP_HOST` | `127.0.0.1` | `0.0.0.0` to accept LAN / mesh |
| `PMB_MCP_PORT` | `8765` | bind port (don't collide with `pmb dashboard`) |
| `PMB_MCP_PATH` | `/mcp` | mount path |
| `PMB_MCP_BEARER_TOKEN` | _(empty)_ | shared secret; empty = unauthenticated |

> Heads-up: `pmb dashboard` also defaults to port 8765. If you run both on
> one host, give one of them a different port.

## Tests

The auth contract is pinned in `tests/integration/test_http_bearer_auth.py`: no
token returns 401 when auth is configured, a wrong token returns 401, the
correct token returns 200, preflight and health pass through, and an empty token
disables the gate.
