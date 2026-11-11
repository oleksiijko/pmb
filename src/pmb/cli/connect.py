"""
`pmb connect <agent>` - one-shot MCP wiring for the common agents.

What this does (and does not do):
- Updates the agent's MCP config file in place, ADDING a `pmb` (or
  `pmb-remote`) entry without touching any other MCP servers the user
  already configured.
- For local agents the command is the venv's pmb-mcp + the current cwd
  as PMB_CWD. For --remote it's the SSH-tunneled form.
- Does NOT install the agent. Does NOT log in. The user runs Claude
  Code / Cursor as usual; this command just edits the JSON config they
  already have.
- `--probe` runs pmb-mcp for ~5s and checks an MCP `initialize` reply,
  so a syntactic config failure surfaces immediately rather than at
  the next agent launch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ----------------------------------------------------------------------
# Agent instruction templates - written into AGENTS.md / CLAUDE.md so the
# AI follows PMB rules from the FIRST message, without needing manual setup.
# ----------------------------------------------------------------------

PMB_AGENT_RULES_START = "<!-- PMB-RULES-START (managed by `pmb connect`) -->"
PMB_AGENT_RULES_END   = "<!-- PMB-RULES-END -->"

PMB_AGENT_RULES_BODY = """\
## PMB - persistent memory (via MCP)

This project has a PMB workspace attached. The user reviews your
adherence to these rules in the PMB dashboard's "Adherence" tab; your
prepare-rate, lesson-followthrough, and read/write ratio are visible
to them. Skipping the rules below means writing into a void the user
can see is unused.

### REQUIRED - call BEFORE the first substantive action of any task

```
prepare(message="<the user's first or current message>")
```

That single call returns project_context, surfaced lessons (with
`surface_id`), recent_activity, open_goals. ~10 ms. If the response
has a `lessons` array, READ THEM before acting - they OVERRIDE your
defaults ("use pnpm not npm" means use pnpm, no debate).

After acting on a surfaced lesson:

```
mark_lesson_followed(surface_id=<from prepare>, followed=True|False,
                     applicable=True|False,
                     note="<one line: what you did>")
```

Use applicable=False when the lesson was unrelated to the task. Use
followed=False only when it applied but you deliberately did not follow it.
Lessons never marked remain unknown.

When recording a project-specific lesson via record_batch, include
`"project":"<project name>"`. Omit `project` only for genuinely global rules.

### OK to skip prepare() ONLY when

- User asks a pure theory question ("what is X / how does Y work")
  with no project context. Answer from training.
- Continuation of a task already prepared earlier this session.
- One-line trivial change (rename a variable, fix a typo).

If you're unsure - call prepare(). It's 10 ms.

### BEFORE a kind of action you haven't done before this session

Before introducing a tool / library / pattern / config / command you
have NOT already used in this session, CHECK memory first:

```
find_lessons(query="<what you're about to do>")     # rules / gotchas
recall(query="<what you're about to do>")           # past decisions / why
```

Then:
- A lesson or past decision covers it → FOLLOW it. Don't re-litigate a
  settled call without a concrete new reason.
- Nothing comes back → proceed with your best judgement, AND record the
  choice so the next session doesn't start from zero:
  `record_batch(items=[{"type":"activity","kind":"decision",
   "content":"Chose X for <task> because <reason>; no prior memory on it"}])`

This is the loop that makes the agent get better at THIS project over
time: act → record why → next time it surfaces as a decision/lesson.

### Context that arrives automatically (PMB hooks)

If PMB hooks are installed you'll sometimes see, prepended to a message,
blocks titled `== PMB auto-context ==` or `== PMB session restore ==`.
That's PMB pre-loading memory for you - treat its lessons/decisions as
if you'd fetched them yourself. After a compaction, the session-restore
block tells you what THIS session already did: pick the thread back up,
don't re-ask the user.

### WRITE - one `record_batch` per turn, only on these triggers

| Trigger | What to write |
| --- | --- |
| "remember / this is important" | fact, importance=0.95, pin=true |
| User shares a personal fact | fact, importance=0.7 |
| You completed substantive work | activity, kind=completed |
| You made a project-shaping choice | activity, kind=decision |
| User corrected you OR a non-obvious rule emerged | lesson (high-importance) |
| User states a personal attribute that CAN CHANGE | record_keyed_fact(subject, attribute, value) |
| FUTURE intent - "next we'll do X / the plan is …" | goal (record_goal or batch {"type":"goal"/"plan", status="pending"}), NOT a fact |

Never call `recall` after writing to "verify". Never call `pin`
separately. Use absolute dates ("On May 25, 2026"), not "today".

### Anti-patterns the user watches for

- ❌ Writing 3 lessons in one session without calling prepare once.
  → looks like LARP, not memory use.
- ❌ Long boilerplate "I'll save this..." narration.
  → the user does not read it; tools already returned.
- ❌ Marking lessons followed=True without a `note`.
  → looks like compliance theatre.
- ❌ Calling recall(...) with a 1-2 word query.
  → use prepare(message=full_user_text) instead.

### Style

Use read results as your own knowledge - weave naturally. Never say
"I found in memory / according to the records / I recorded that". Don't
narrate which tools you called.

PMB is local-only. Apache 2.0. The user owns every byte.
"""


# Active-mode rules are BUILT from per-category toggles (config `agent.log_*`)
# so pro users control exactly what the agent logs. Opt-in via
# `pmb connect <agent> --active`. The recall side stays LAZY (no recall on
# general questions = the speed win); only the WRITE side becomes proactive.

_ACTIVE_CATEGORY_LINES: dict[str, str] = {
    "decisions": (
        '- **Decision** - chose a library / pattern / schema / config:\n'
        '  `{"type":"activity","kind":"decision","content":"Chose Postgres over Mongo for JSONB"}`'
    ),
    "completed": (
        '- **Done** - shipped a feature, fixed a bug, refactored:\n'
        '  `{"type":"activity","kind":"completed","content":"Fixed JWT 24h-expiry bug in auth.py"}`'
    ),
    "lessons": (
        '- **Lesson** - found a project convention / gotcha, or the user corrected you:\n'
        '  `{"type":"lesson","content":"This repo uses pnpm, never npm"}`'
    ),
    "failures": (
        '- **Failure** - something you tried did NOT work (so it is not repeated):\n'
        '  `{"type":"failure","content":"Bumping numpy to 2.x broke lancedb - stay on 1.x"}`'
    ),
    "goals": (
        '- **Goal** - the user states an intent / plan:\n'
        '  `{"type":"goal","title":"Ship v1 by June","status":"in_progress"}`'
    ),
}

_ACTIVE_DEFAULT_TOGGLES = {
    "log_decisions": True, "log_completed": True, "log_lessons": True,
    "log_failures": True, "log_goals": True, "apply_lessons": True,
    "context_continuity": True,
}


def build_active_addendum(toggles: dict | None = None) -> str:
    """Build the proactive-logging addendum from per-category toggles.

    Keys (all bool): log_decisions / log_completed / log_lessons /
    log_failures / log_goals / apply_lessons. None -> all enabled.
    """
    t = {**_ACTIVE_DEFAULT_TOGGLES, **(toggles or {})}
    cats = [c for c in ("decisions", "completed", "lessons", "failures", "goals")
            if t.get(f"log_{c}")]
    cat_block = "\n".join(_ACTIVE_CATEGORY_LINES[c] for c in cats) or \
        "- (all logging categories are disabled in config)"

    loop = ""
    if t.get("apply_lessons"):
        loop = (
            "\n\n### Self-improvement loop (use what you learned - don't repeat mistakes)\n\n"
            'At the START of a non-trivial task, call `recall("<task topic> '
            'lessons failures")` ONCE. If a ★lesson or ⚠failure comes back, '
            "FOLLOW it. When the user corrects you or something fails, record it "
            "(above) so the NEXT session is smarter - the agent should get better "
            "at THIS project over time."
        )

    cont = ""
    if t.get("context_continuity"):
        cont = (
            "\n\n### Don't lose the thread (long sessions)\n\n"
            "In a long session your own context window compacts and you lose "
            "detail. PMB is your durable session memory - the logging above keeps "
            "the record. If you've lost track (after a compaction, or many turns "
            "in), call `session_brief` ONCE to re-orient on what was decided/done "
            "this session, then continue. Don't re-ask the user what you already did."
        )

    return (
        "\n\n### AI-AGENT ACTIVE MODE (this connection was set up with --active)\n\n"
        'When YOU do real work, LOG IT proactively - do not wait for "remember" '
        "(still exactly ONE `record_batch` per turn):\n\n"
        f"{cat_block}\n\n"
        "Still skip general Q&A and trivial steps. Recall stays lazy on general "
        "and technical questions."
        f"{loop}{cont}\n"
    )


def _build_agent_rules_block(active: bool = False,
                             active_toggles: dict | None = None) -> str:
    """The full rules block including markers. When `active`, append the
    proactive-logging addendum built from `active_toggles` (config-driven;
    None = all categories on)."""
    body = PMB_AGENT_RULES_BODY + (build_active_addendum(active_toggles) if active else "")
    return f"\n{PMB_AGENT_RULES_START}\n{body}\n{PMB_AGENT_RULES_END}\n"


def install_agent_rules(path: Path, active: bool = False,
                        active_toggles: dict | None = None) -> str:
    """Append (or update) the PMB rules block in the agent's markdown
    instructions file. Returns one of: 'created', 'updated', 'added'.

    Uses BEGIN/END markers so repeated `pmb connect` calls don't duplicate.
    `active` installs the proactive-logging variant (built from `active_toggles`).
    """
    block = _build_agent_rules_block(active=active, active_toggles=active_toggles)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        # Create from scratch with a small header
        header = (
            "# Agent Instructions\n\n"
            "This file gives the AI agent persistent rules across sessions.\n"
        )
        path.write_text(header + block, encoding="utf-8")
        return "created"

    existing = path.read_text(encoding="utf-8")
    if PMB_AGENT_RULES_START in existing and PMB_AGENT_RULES_END in existing:
        # Replace existing block
        before = existing.split(PMB_AGENT_RULES_START)[0].rstrip()
        after = existing.split(PMB_AGENT_RULES_END, 1)[1].lstrip()
        new = before + "\n" + block.strip() + "\n"
        if after:
            new += "\n" + after
        path.write_text(new, encoding="utf-8")
        return "updated"

    # Append at end
    path.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")
    return "added"


def instruction_paths_for_agent(agent: str, cwd: Path) -> list[Path]:
    """Where each agent looks for instruction markdown."""
    home = Path.home()
    if agent == "claude-code":
        # Claude Code reads CLAUDE.md (project) and ~/.claude/CLAUDE.md (global)
        return [home / ".claude" / "CLAUDE.md", cwd / "CLAUDE.md"]
    if agent == "cursor":
        # Cursor uses .cursorrules at project root or .cursor/rules/*.md
        return [cwd / ".cursorrules"]
    if agent == "codex":
        # OpenAI Codex CLI reads AGENTS.md (project) and ~/.codex/AGENTS.md (global)
        return [home / ".codex" / "AGENTS.md", cwd / "AGENTS.md"]
    return []


# ----------------------------------------------------------------------
# Path resolution per agent
# ----------------------------------------------------------------------


@dataclass
class AgentTarget:
    name: str
    config_paths: list[Path]  # ordered: first existing one wins
    fallback_path: Path       # used when none exists


def claude_code_paths(scope: str, cwd: Path) -> AgentTarget:
    """Resolve where Claude Code keeps its MCP config.

    `scope` ∈ {'project', 'global'}.
      - project → <cwd>/.mcp.json   (Claude Code's standard project config)
      - global  → ~/.claude.json    (Claude Code's user-level config)

    For project mode we create the file if missing. For global we never
    create it from scratch - only edit one that Claude Code already wrote;
    otherwise we fall back to project so the user isn't surprised.
    """
    if scope == "project":
        proj = cwd / ".mcp.json"
        return AgentTarget("claude-code", [proj], proj)
    home = Path.home()
    candidates = [home / ".claude.json"]
    fallback = candidates[0]
    return AgentTarget("claude-code", candidates, fallback)


def cursor_paths(cwd: Path) -> AgentTarget:
    """Cursor's MCP config:
      project: <cwd>/.cursor/mcp.json
      global:  ~/.cursor/mcp.json
    We prefer the project file when the user is inside a project.
    """
    proj = cwd / ".cursor" / "mcp.json"
    glob = Path.home() / ".cursor" / "mcp.json"
    return AgentTarget("cursor", [proj, glob], proj)


def codex_paths(cwd: Path) -> AgentTarget:
    """OpenAI Codex CLI's MCP config: ~/.codex/config.toml.

    Codex uses TOML and stores MCP servers as `[mcp_servers.<name>]`
    sections. No project-level config - global only.
    """
    global_path = Path.home() / ".codex" / "config.toml"
    return AgentTarget("codex", [global_path], global_path)


# ----------------------------------------------------------------------
# JSON merge - never trample existing mcpServers
# ----------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import tomllib  # stdlib (3.11+)
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_toml(path: Path, data: dict) -> None:
    try:
        import tomli_w
    except ImportError as e:
        raise RuntimeError(
            "Codex MCP support requires tomli_w (writes TOML). "
            "Install: pip install tomli-w"
        ) from e
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def make_local_entry(
    workspace_cwd: Path,
    workspace_id: str | None = None,
    pmb_home: Path | None = None,
    tool_profile: str | None = None,
) -> dict:
    """The local pmb MCP server entry.

    workspace_id forces a SPECIFIC workspace (override the cwd-based auto-
    detection). Use this to share one workspace across multiple AI clients -
    e.g. Claude Code + Cursor both pointing at the same `personal` workspace.

    pmb_home overrides PMB_HOME (where workspaces live on disk). Useful for
    multi-user shared memory (point at a shared NAS path).

    tool_profile (minimal | lean | default | full) is written as
    PMB_TOOL_PROFILE in the server env - `pmb connect` sets "lean" for a
    hook-enabled host so the agent isn't offered MCP tools the hooks already
    cover. None = leave it to the server default.
    """
    venv_python = sys.executable
    pmb_mcp = shutil.which("pmb-mcp")
    env: dict = {"PMB_CWD": str(workspace_cwd)}
    if workspace_id:
        env["PMB_WORKSPACE"] = workspace_id
    if pmb_home:
        env["PMB_HOME"] = str(pmb_home)
    if tool_profile:
        env["PMB_TOOL_PROFILE"] = tool_profile
    if pmb_mcp:
        return {"command": pmb_mcp, "env": env}
    return {
        "command": venv_python,
        "args": ["-m", "pmb.mcp.server"],
        "env": env,
    }


def make_remote_entry(remote: str, bearer_token: str | None = None) -> dict:
    """Build an MCP entry for a remote PMB server.

    Two forms are supported:

      • SSH tunnel:    `user@host:/abs/path/to/repo` - wraps `pmb-mcp` via ssh
                       (stdio over an SSH tunnel; uses the remote box's PMB).
      • HTTP URL:      `http://host:8765/mcp` or `https://...` - connects to
                       a `pmb mcp serve --transport streamable-http` process.

    For HTTP, pass `bearer_token` if the server was started with
    `--bearer-token` - the resulting entry will include the
    `Authorization: Bearer <token>` header.
    """
    if remote.startswith(("http://", "https://")):
        entry: dict = {
            "type": "http",
            "url": remote,
        }
        if bearer_token:
            entry["headers"] = {
                "Authorization": f"Bearer {bearer_token}",
            }
        return entry

    if ":" not in remote:
        raise ValueError(
            f"--remote must be one of:\n"
            f"  • user@host:/abs/path/to/repo   (SSH tunnel)\n"
            f"  • http://host:port/mcp           (streamable HTTP)\n"
            f"got {remote!r}"
        )
    target, remote_cwd = remote.split(":", 1)
    remote_cmd = f'PMB_CWD="{remote_cwd}" pmb-mcp'
    return {
        "command": "ssh",
        "args": [target, remote_cmd],
    }


def _prep_daemon_http(pmb_home: Path | None, tool_profile: str | None) -> bool:
    """S6: decide whether the shared HTTP-daemon entry is viable, and if so make
    it keep the MCP connection warm. Returns False (→ stdio fallback) when
    daemon.autostart is off. When viable, pins `daemon.idle_exit_min=0` so the
    daemon doesn't disappear mid-session (HTTP has no client-owned lifecycle the
    way stdio does), and records the tool profile the daemon should serve."""
    try:
        import os

        from pmb.config import Config
        home = (Path(pmb_home) if pmb_home
                else Path(os.environ.get("PMB_HOME") or (Path.home() / ".pmb")))
        cfg = Config(pmb_home=home)
        if not bool(cfg.get("daemon.autostart")):
            return False
        cfg.set_global("daemon.idle_exit_min", 0)
        if tool_profile:
            cfg.set_global("daemon.tool_profile", tool_profile)
        return True
    except Exception:
        return False


def make_daemon_entry(
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
) -> dict:
    """S6: a streamable-HTTP MCP entry pointing at the LOCAL warm daemon, so N
    AI clients share ONE warm process (one Engine + one ~400 MB model) instead
    of spawning a stdio server each. The bearer token is the PERSISTENT daemon
    token (created here if absent) - stable across daemon restarts, so the baked
    `Authorization` header doesn't go stale when the daemon idle-exits and the
    hook autostarts it again.

    Shape matches `make_remote_entry`'s HTTP form (`type: http`), which the
    big-three JSON hosts already accept. Codex/extended (stdio-shaped) hosts
    keep the local stdio entry - the caller decides."""
    from pmb.mcp.daemon import write_daemon_token
    token = write_daemon_token()   # reuse-if-present (persistent)
    entry: dict = {"type": "http", "url": f"http://{host}:{int(port)}{path}"}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return entry


def make_proxy_entry(
    workspace_cwd: Path,
    workspace_id: str | None = None,
    pmb_home: Path | None = None,
) -> dict:
    """S6 (codex): a stdio entry that launches the lightweight `pmb mcp proxy`
    bridge instead of the full `pmb-mcp` server.

    Codex is stdio-only (TOML, command-shaped) - it can't take the `type: http`
    daemon entry the JSON hosts use. The proxy bridges that gap: it speaks stdio
    MCP to codex and forwards to the ONE warm daemon over HTTP (autostarting it
    if needed), holding NO model itself. So codex shares the single ~400 MB warm
    process instead of cold-loading its own Engine+model every session - which is
    exactly the codex start-up lag.

    The daemon bearer token is read from disk by the proxy AT RUNTIME, so nothing
    stale is baked into the config (survives daemon restarts / token rotation)."""
    pmb_cli = shutil.which("pmb")
    env: dict = {"PMB_CWD": str(workspace_cwd)}
    if workspace_id:
        env["PMB_WORKSPACE"] = workspace_id
    if pmb_home:
        env["PMB_HOME"] = str(pmb_home)
    if pmb_cli:
        return {"command": pmb_cli, "args": ["mcp", "proxy"], "env": env}
    return {
        "command": sys.executable,
        "args": ["-m", "pmb.cli", "mcp", "proxy"],
        "env": env,
    }


def merge_entry(existing: dict, name: str, entry: dict) -> tuple[dict, str]:
    """Add or replace the named entry. Returns (new_config, action_str)."""
    cfg = dict(existing) if existing else {}
    servers = dict(cfg.get("mcpServers") or {})
    action = "replaced" if name in servers else "added"
    servers[name] = entry
    cfg["mcpServers"] = servers
    return cfg, action


def merge_codex_entry(
    existing: dict, name: str, entry: dict,
) -> tuple[dict, str]:
    """Codex stores MCP servers as nested `[mcp_servers.<name>]` tables.

    TOML round-trips through dict-of-dicts - same structure, different
    serialization. We touch ONLY the `mcp_servers.<name>` key, never the
    rest of the config (which has marketplaces / plugins / projects etc.).
    """
    cfg = dict(existing) if existing else {}
    servers = dict(cfg.get("mcp_servers") or {})
    action = "replaced" if name in servers else "added"
    # Codex expects 'command', 'args', 'env' (no nested 'mcpServers' wrap)
    codex_entry = {}
    if "command" in entry:
        codex_entry["command"] = entry["command"]
    if "args" in entry:
        codex_entry["args"] = list(entry["args"])
    else:
        codex_entry["args"] = []
    if entry.get("env"):
        codex_entry["env"] = dict(entry["env"])
    # Codex-specific: timeout in case PMB cold start is slow (model load)
    codex_entry["startup_timeout_sec"] = 120
    # Per-call timeout: Codex defaults to 120s per tools/call, which a
    # legitimately slow FIRST warm call (cold model load under memory
    # pressure - paging on a machine already hosting the daemon's model) can
    # exceed. 300s gives the cold path headroom; normal calls are ms.
    codex_entry["tool_timeout_sec"] = 300
    servers[name] = codex_entry
    cfg["mcp_servers"] = servers
    return cfg, action


# ----------------------------------------------------------------------
# Extended agent registry (Sprint 1) - data-driven specs for agents that
# wire MCP through a JSON or YAML config. The big-three (claude-code /
# cursor / codex) keep their dedicated code paths above; everything here
# is purely additive so their behaviour and tests are untouched.
#
# Each agent differs in three ways we have to model:
#   1. WHERE the config file lives (project vs home, XDG vs %APPDATA%).
#   2. WHICH top-level key holds the servers ("mcpServers" / "servers" /
#      "context_servers" / "mcp").
#   3. The SHAPE of a single server entry (flat command, command-as-list,
#      command-wrapped-object, env vs environment, list-of-objects YAML).
#
# `--config-path` on the CLI lets a user override (1) when their install
# puts the file somewhere our default guess doesn't cover.
# ----------------------------------------------------------------------


@dataclass
class JsonAgentSpec:
    name: str
    servers_key: str            # top-level key the agent reads servers from
    shape: str                  # "claude" | "vscode" | "zed" | "opencode" | "continue-yaml"
    fmt: str = "json"           # "json" | "yaml"
    project_path: str | None = None   # relative to cwd
    global_path: str | None = None     # relative to home; ".config/..." → XDG
    instruction_file: str | None = None
    instruction_in_home: bool = False
    docs: str = ""              # one-liner shown in help / docs


JSON_AGENT_SPECS: dict[str, JsonAgentSpec] = {
    # Codeium Windsurf - Claude-shaped mcpServers JSON.
    "windsurf": JsonAgentSpec(
        name="windsurf",
        servers_key="mcpServers",
        shape="claude",
        global_path=".codeium/windsurf/mcp_config.json",
        instruction_file=".windsurfrules",
        docs="Codeium Windsurf (~/.codeium/windsurf/mcp_config.json)",
    ),
    # Google Gemini CLI - Claude-shaped mcpServers JSON in ~/.gemini/settings.json.
    "gemini": JsonAgentSpec(
        name="gemini",
        servers_key="mcpServers",
        shape="claude",
        global_path=".gemini/settings.json",
        instruction_file=".gemini/GEMINI.md",
        instruction_in_home=True,
        docs="Google Gemini CLI (~/.gemini/settings.json)",
    ),
    # VS Code native MCP / GitHub Copilot - project .vscode/mcp.json, "servers" key.
    "vscode": JsonAgentSpec(
        name="vscode",
        servers_key="servers",
        shape="vscode",
        project_path=".vscode/mcp.json",
        instruction_file=".github/copilot-instructions.md",
        docs="VS Code / Copilot MCP (<project>/.vscode/mcp.json)",
    ),
    # Zed editor - context_servers, command wrapped as {path,args,env}.
    "zed": JsonAgentSpec(
        name="zed",
        servers_key="context_servers",
        shape="zed",
        global_path=".config/zed/settings.json",
        docs="Zed editor (~/.config/zed/settings.json)",
    ),
    # OpenCode (sst/opencode) - "mcp" key, command-as-list, type:local.
    "opencode": JsonAgentSpec(
        name="opencode",
        servers_key="mcp",
        shape="opencode",
        global_path=".config/opencode/opencode.json",
        instruction_file="AGENTS.md",
        docs="OpenCode (~/.config/opencode/opencode.json)",
    ),
    # Continue.dev - YAML config, mcpServers is a LIST of objects.
    "continue": JsonAgentSpec(
        name="continue",
        servers_key="mcpServers",
        shape="continue-yaml",
        fmt="yaml",
        global_path=".continue/config.yaml",
        docs="Continue.dev (~/.continue/config.yaml)",
    ),
}


def supported_agents() -> list[str]:
    """All agent ids `pmb connect` accepts, in display order."""
    return ["claude-code", "cursor", "codex", *sorted(JSON_AGENT_SPECS)]


def detect_installed_agents(
    home: Path | None = None, cwd: Path | None = None,
) -> list[str]:
    """Best-effort: which supported agents already have a config file on disk.

    Used by `pmb setup` to suggest what to wire without making the user guess.
    Read-only. `home`/`cwd` are injectable for testing.
    """
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    checks: dict[str, list[Path]] = {
        "claude-code": [home / ".claude.json", cwd / ".mcp.json",
                        home / ".claude" / "CLAUDE.md"],
        "cursor": [cwd / ".cursor" / "mcp.json", home / ".cursor" / "mcp.json"],
        "codex": [home / ".codex" / "config.toml", home / ".codex" / "AGENTS.md"],
    }
    for aid, spec in JSON_AGENT_SPECS.items():
        paths: list[Path] = []
        if spec.global_path:
            if spec.global_path.startswith(".config/"):
                base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
                paths.append(base / spec.global_path[len(".config/"):])
            else:
                paths.append(home / spec.global_path)
        if spec.project_path:
            paths.append(cwd / spec.project_path)
        checks[aid] = paths
    found = {aid for aid, paths in checks.items() if any(p.exists() for p in paths)}
    return [a for a in supported_agents() if a in found]


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def _resolve_extended_path(
    spec: JsonAgentSpec, cwd: Path, scope: str, override: str | None,
) -> Path:
    """Pick the config file for an extended agent.

    Precedence: explicit --config-path > project file (when the agent has
    one and scope=project) > global file. `.config/...` global paths are
    routed through XDG_CONFIG_HOME so Linux/macOS custom configs are honored.
    """
    if override:
        return Path(override).expanduser()
    if spec.project_path and (scope == "project" or not spec.global_path):
        return cwd / spec.project_path
    if spec.global_path:
        if spec.global_path.startswith(".config/"):
            return _xdg_config_home() / spec.global_path[len(".config/"):]
        return Path.home() / spec.global_path
    if spec.project_path:
        return cwd / spec.project_path
    raise ValueError(f"no config path resolvable for agent {spec.name!r}")


def _split_entry(entry: dict) -> tuple[str, list[str], dict]:
    """Pull (command, args, env) out of a make_local/remote_entry dict."""
    command = entry.get("command", "")
    args = list(entry.get("args") or [])
    env = dict(entry.get("env") or {})
    return command, args, env


def shape_entry(shape: str, command: str, args: list[str], env: dict) -> dict:
    """Render a single server entry in the agent-specific shape."""
    if shape in ("claude", "vscode"):
        # Flat: {command, args?, env?}. VS Code lives under "servers" but the
        # per-entry shape is identical to the Claude one.
        e: dict = {"command": command}
        if args:
            e["args"] = args
        if env:
            e["env"] = env
        return e
    if shape == "zed":
        # Zed wraps the launch spec in a "command" object.
        return {"command": {"path": command, "args": args, "env": env}}
    if shape == "opencode":
        # OpenCode: command is a list, env is "environment", explicit enabled.
        return {
            "type": "local",
            "command": [command, *args],
            "enabled": True,
            "environment": env,
        }
    raise ValueError(f"unknown entry shape {shape!r}")


def merge_keyed_entry(
    existing: dict, servers_key: str, name: str, shaped: dict,
) -> tuple[dict, str]:
    """Add/replace `name` under a dict-shaped servers key (preserve siblings)."""
    cfg = dict(existing) if existing else {}
    servers = dict(cfg.get(servers_key) or {})
    action = "replaced" if name in servers else "added"
    servers[name] = shaped
    cfg[servers_key] = servers
    return cfg, action


def merge_continue_entry(
    existing: dict, name: str, command: str, args: list[str], env: dict,
) -> tuple[dict, str]:
    """Continue.dev stores mcpServers as a LIST of {name, command, ...}."""
    cfg = dict(existing) if existing else {}
    servers = list(cfg.get("mcpServers") or [])
    new_entry: dict = {"name": name, "command": command}
    if args:
        new_entry["args"] = args
    if env:
        new_entry["env"] = env
    action = "added"
    for i, s in enumerate(servers):
        if isinstance(s, dict) and s.get("name") == name:
            servers[i] = new_entry
            action = "replaced"
            break
    else:
        servers.append(new_entry)
    cfg["mcpServers"] = servers
    return cfg, action


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_yaml(path: Path, data: dict) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def connect_extended_agent(
    agent: str,
    *,
    cwd: Path,
    scope: str,
    remote: str | None,
    name_override: str | None,
    workspace_id: str | None,
    pmb_home: Path | None,
    config_path: str | None = None,
    active: bool = False,
    active_toggles: dict | None = None,
    bearer_token: str | None = None,
) -> dict:
    """Wire one of the JSON_AGENT_SPECS agents. Returns the same dict shape
    as `connect()` so the CLI layer renders both paths identically."""
    spec = JSON_AGENT_SPECS[agent]

    if remote:
        base = make_remote_entry(remote, bearer_token=bearer_token)
        name = name_override or "pmb-remote"
    else:
        base = make_local_entry(cwd, workspace_id=workspace_id, pmb_home=pmb_home)
        name = name_override or ("pmb-shared" if workspace_id else "pmb")

    command, args, env = _split_entry(base)
    path = _resolve_extended_path(spec, cwd, scope, config_path)

    if spec.fmt == "yaml":
        existing = _load_yaml(path)
        new_cfg, action = merge_continue_entry(existing, name, command, args, env)
        shaped = next(s for s in new_cfg["mcpServers"] if s.get("name") == name)
        _save_yaml(path, new_cfg)
    else:
        shaped = shape_entry(spec.shape, command, args, env)
        existing = _load_json(path)
        new_cfg, action = merge_keyed_entry(existing, spec.servers_key, name, shaped)
        _save_json(path, new_cfg)

    rules_written: list[dict] = []
    if spec.instruction_file:
        try:
            if spec.instruction_in_home:
                inst_path = Path.home() / spec.instruction_file
            else:
                inst_path = cwd / spec.instruction_file
            written = install_agent_rules(inst_path, active=active,
                                          active_toggles=active_toggles)
            rules_written.append({"path": str(inst_path), "action": written})
        except Exception as e:  # noqa: BLE001 - surface to caller, never crash connect
            rules_written.append({"error": str(e)})

    return {
        "agent": agent,
        "scope": scope,
        "config_path": str(path),
        "entry_name": name,
        "action": action,
        "entry": shaped,
        "workspace_id": workspace_id,
        "instruction_rules": rules_written,
    }


# ----------------------------------------------------------------------
# Probe - run pmb-mcp briefly and check it speaks MCP
# ----------------------------------------------------------------------


def probe_mcp(timeout_seconds: float = 6.0) -> tuple[bool, str]:
    """Spawn pmb-mcp, send an `initialize` request over stdio, read response.

    Returns (ok, message). On Windows we keep the timeout short - the goal
    is to surface obvious 'doesn't even start' problems, not full
    integration testing.
    """
    pmb_mcp = shutil.which("pmb-mcp")
    if pmb_mcp:
        cmd = [pmb_mcp]
    else:
        cmd = [sys.executable, "-m", "pmb.mcp.server"]

    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pmb-connect-probe", "version": "0"},
        },
    }
    payload = json.dumps(init) + "\n"

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            # CREATE_NO_WINDOW on Windows so the short-lived probe server does not
            # flash a console window (0 elsewhere). Attr exists only on Windows.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return False, "pmb-mcp not found in PATH"

    try:
        out, err = proc.communicate(input=payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        # Timeout is actually a *good* sign: the server is alive and waiting
        # for more input. We just haven't taught it to exit cleanly on EOF.
        return True, "server stayed alive past initialize (no crash within probe window)"

    if proc.returncode == 0 and ("result" in (out or "") or "id" in (out or "")):
        return True, "got an MCP-shaped response"
    if proc.returncode != 0:
        return False, f"server exited {proc.returncode}: {(err or '').strip()[:300]}"
    return False, f"no recognizable response. stdout head: {(out or '')[:200]!r}"


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


# Hosts with real lifecycle hooks. Installing them lets the reflexive
# read/write run out-of-band, so we can trim the MCP surface (see _lean_for).
_HOOK_HOSTS = {"claude-code", "codex"}


def _lean_for(agent: str) -> str | None:
    """MCP tool profile to PIN for `agent` when hooks ARE installed.

    The built-in default is now the core-10 ("minimal") surface, so most hosts
    need no override. claude-code installs a per-turn auto-recall HOOK
    (UserPromptSubmit) that makes even the browse tools redundant, so we pin it
    explicitly to "minimal" — self-documenting in the generated config and
    robust to a future change of the built-in default. Codex installs only
    AMBIENT hooks (no per-turn read hook), so it inherits the server default.
    """
    return "minimal" if agent == "claude-code" else None


def connect(
    agent: str,
    *,
    cwd: Path,
    scope: str = "project",
    remote: str | None = None,
    name_override: str | None = None,
    workspace_id: str | None = None,
    pmb_home: Path | None = None,
    config_path: str | None = None,
    active: bool = False,
    active_toggles: dict | None = None,
    bearer_token: str | None = None,
    install_hooks: bool = False,
    use_daemon: bool = False,
) -> dict:
    """Write the MCP entry into the right config file.

    workspace_id and pmb_home are forwarded to the MCP server via env vars
    (PMB_WORKSPACE, PMB_HOME). When set, the server uses that exact workspace
    regardless of cwd - letting multiple AI clients share one memory.

    `remote` accepts either an SSH form (`user@host:/abs/path`) or an
    HTTP URL (`http://host:8765/mcp`). For HTTP, pass `bearer_token` if
    the server was started with --bearer-token.
    """
    # Extended agents (windsurf / gemini / vscode / zed / opencode / continue)
    # have their own config formats - handle them and return early. The
    # big-three below keep their original code path untouched.
    if agent in JSON_AGENT_SPECS:
        return connect_extended_agent(
            agent, cwd=cwd, scope=scope, remote=remote,
            name_override=name_override, workspace_id=workspace_id,
            pmb_home=pmb_home, config_path=config_path, active=active,
            active_toggles=active_toggles, bearer_token=bearer_token,
        )

    if agent == "claude-code":
        target = claude_code_paths(scope, cwd)
    elif agent == "cursor":
        target = cursor_paths(cwd)
    elif agent == "codex":
        target = codex_paths(cwd)
    else:
        raise ValueError(
            f"unsupported agent {agent!r}. Try one of: "
            + ", ".join(supported_agents())
        )

    path = next((p for p in target.config_paths if p.exists()), target.fallback_path)

    # Install hooks (reflexive read/write) for hook-capable hosts, and trim the
    # MCP surface accordingly. Lean profile is set ONLY when the hooks that
    # cover those tools are actually installed (else the agent would lose both
    # the tool AND the hook). Remote servers manage their own profile.
    will_install_hooks = install_hooks and agent in _HOOK_HOSTS and not remote
    tool_profile = _lean_for(agent) if will_install_hooks else None

    # S6: `--daemon` points the host at the one warm local daemon instead of its
    # own stdio Engine+model. Two shapes:
    #   • JSON hosts (claude-code / cursor) take a `type: http` entry directly.
    #   • codex (TOML, stdio-only) can't take HTTP, so it spawns the lightweight
    #     `pmb mcp proxy` stdio<->daemon bridge - still ONE shared warm process.
    # remote always keeps its own entry.
    daemon_http = bool(use_daemon and not remote and agent in ("claude-code", "cursor"))
    if daemon_http:
        # Gate on daemon.autostart + pin idle_exit_min=0 so the shared daemon
        # actually stays reachable for the MCP connection; falls back to stdio
        # when autostart is off.
        daemon_http = _prep_daemon_http(pmb_home, tool_profile)
    daemon_proxy = bool(use_daemon and not remote and agent == "codex")
    if daemon_proxy:
        # Same gate as the HTTP path (autostart on + pin idle). tool_profile is
        # left untouched (None) - codex needs the read tools, and even the
        # 'lean' profile keeps prepare/recall/session_brief/find_lessons.
        daemon_proxy = _prep_daemon_http(pmb_home, None)
    if remote:
        entry = make_remote_entry(remote, bearer_token=bearer_token)
        name = name_override or "pmb-remote"
    elif daemon_http:
        from pmb.mcp.registry import find_live_daemon

        live = find_live_daemon(str(pmb_home) if pmb_home else None)
        entry = make_daemon_entry(port=int(live["port"])) if live else make_daemon_entry()
        name = name_override or ("pmb-shared" if workspace_id else "pmb")
    elif daemon_proxy:
        entry = make_proxy_entry(cwd, workspace_id=workspace_id, pmb_home=pmb_home)
        name = name_override or ("pmb-shared" if workspace_id else "pmb")
    else:
        entry = make_local_entry(
            cwd, workspace_id=workspace_id, pmb_home=pmb_home,
            tool_profile=tool_profile,
        )
        name = name_override or ("pmb-shared" if workspace_id else "pmb")

    # Codex uses TOML (mcp_servers.<name>), others use JSON (mcpServers.<name>)
    if agent == "codex":
        existing = _load_toml(path)
        new_cfg, action = merge_codex_entry(existing, name, entry)
        _save_toml(path, new_cfg)
    else:
        existing = _load_json(path)
        new_cfg, action = merge_entry(existing, name, entry)
        _save_json(path, new_cfg)

    # Improvement O: also write PMB rules into the agent's instructions
    # file (AGENTS.md / CLAUDE.md / .cursorrules) so the AI knows how to
    # use PMB from the very first message. Idempotent (BEGIN/END markers).
    rules_written: list[dict] = []
    try:
        for inst_path in instruction_paths_for_agent(agent, cwd):
            # Write to each instruction path the agent reads; global (home)
            # is safest because it works across all projects.
            written = install_agent_rules(inst_path, active=active,
                                          active_toggles=active_toggles)
            rules_written.append({
                "path": str(inst_path),
                "action": written,
            })
            break  # only do the first (global) for simplicity
    except Exception as e:
        rules_written.append({"error": str(e)})

    # Install the lifecycle hooks (auto-recall + ambient + restore + follow-
    # through). This is what makes the lean MCP profile safe. Best-effort:
    # never crash connect if the host's settings file is unusual.
    hooks_result = None
    if will_install_hooks:
        try:
            from pmb.cli.hooks import install_hook
            hooks_result = install_hook(agent)
        except Exception as e:  # noqa: BLE001
            hooks_result = {"error": str(e)}

    return {
        "agent": agent,
        "scope": scope,
        "config_path": str(path),
        "entry_name": name,
        "action": action,
        "entry": entry,
        "workspace_id": workspace_id,
        "instruction_rules": rules_written,
        "hooks": hooks_result,
        "tool_profile": tool_profile,
        # S6: True when this host was pointed at the shared warm daemon over
        # HTTP. The CLI uses it to ensure the daemon is up and print RSS math.
        "daemon_http": daemon_http,
        # S6 (codex): True when codex was wired to spawn the stdio<->daemon proxy
        # so it shares the one warm process instead of its own Engine+model.
        "daemon_proxy": daemon_proxy,
        # Signalled when --daemon was asked for but the host can't share the
        # daemon at all (extended/remote) - we kept stdio; surfaced so the CLI
        # can tell the user instead of silently ignoring the flag.
        "daemon_http_unavailable": bool(
            use_daemon and not daemon_http and not daemon_proxy),
    }
