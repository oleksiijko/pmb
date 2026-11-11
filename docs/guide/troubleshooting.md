# Troubleshooting

Most problems come down to the daemon, a port clash, or an agent that was not
restarted after wiring. Start here, then run `pmb doctor` for a full check.

## First step: run the doctor

```bash
pmb doctor
```

It checks the install, the MCP wiring, and the runtime state, and points at what
is wrong. Add `--remote user@host:/path` to print an SSH-tunneled MCP snippet for
a remote daemon.

## The agent does not seem to use memory

1. Restart the agent after `pmb setup` or `pmb connect`. Hosts read their MCP and
   rules config at startup, so changes only take effect on the next launch.
2. Confirm the wiring with `pmb doctor`, and that memory exists with `pmb stats`.
3. Remember how each host receives memory: Claude Code uses lifecycle hooks,
   while Cursor, Zed, VS Code, Windsurf, Gemini, and Continue call PMB as MCP
   tools when their installed rules say memory is relevant. See
   [Usage](usage.md).

## The dashboard and daemon fight over a port

Both default to `127.0.0.1:8765`. Current PMB versions auto-skip the default
dashboard port when it is already busy:

```bash
pmb dashboard
```

If you want a fixed dashboard port, pass one explicitly:

```bash
pmb dashboard --port 18888
```

If the OS still refuses the bind, check what is already running:

```bash
pmb daemon status
```

## The daemon is not warm, or processes piled up

```bash
pmb daemon status      # is the daemon up and warm?
pmb daemon restart     # restart it, for example after changing the model
pmb daemon kill-all    # stop the daemon and every stray PMB process, then reset
```

`pmb daemon kill-all` is the reset button: if duplicate warm processes pile up
and slow your machine, it stops all of them and clears the registry so you can
start fresh with `pmb daemon start`.

## The first recall is slow

The slow part is loading the embedding model. Warm it once so the next recall is
instant:

```bash
pmb warmup
```

The shared daemon keeps it warm after that. If no daemon is running, a cold hook
answers from an in-process engine and asks a daemon to start in the background,
so the following message is warm.

## Recall returns nothing for a topic you know is there

The precision gates keep the read path quiet, so a vague or conversational query
surfaces nothing rather than a weak match. Query it directly and inspect the
ranking:

```bash
pmb recall "your query"
pmb why "your query"      # which ranking rules fired, and each multiplier
```

## Recall is weak in another language

Recall works in any language on day one, but exact top-1 is weaker for CJK and a
few lower-resource languages. Switch to a stronger multilingual embedder:

```bash
pmb config set embedding.model BAAI/bge-m3
pmb reindex
pmb daemon restart
```

See [Adding a language](../contributing/adding-a-language.md) for the details.

## Changing the model did not take effect

Switching the embedder needs a re-embed. Use the one-step command instead of
editing config by hand:

```bash
pmb model            # download, re-embed memory, and restart the daemon
```

## Ollama problems

For a fully offline LLM backend, see the dedicated [Ollama](SETUP_OLLAMA.md)
page, which covers status checks, model presets, and remote hosts.
## The daemon changed port

`pmb daemon restart` reuses the running daemon's address by default. If the
port is occupied, PMB prints the new port. Re-run `pmb connect <agent> --daemon`
with the original scope while the daemon is running, or update the port in
the client's MCP URL. Restart the agent to load the updated configuration.
