"""
LLM-based consolidation - the "sleep engine" that actually generalizes.

Pipeline:
  1. Pick recent active content events (qa, fact, git).
  2. Cluster them by embedding similarity (greedy, cosine ≥ threshold).
  3. For each cluster of size ≥ min_cluster_size, send to an LLM.
     The LLM is asked: "is there a single rule / fact that ties these
     together? if yes, state it concisely."
  4. If the LLM agrees with high confidence, store the generalization
     as a new `fact` event with high importance and metadata pointing at
     source ulids. Sources get archived (not deleted - restorable).

This is what makes consolidation different from simple decay/archive:
the system *produces a generalization* rather than just discarding old
records. Example:

  source events:
    - "don't add comments to code"
    - "agent: should I document this function? user: no"
    - "remove docstrings, they're noise"
    - "user repeatedly asks for no comments"
  consolidated fact:
    - "User prefers code without comments or docstrings."

Cost note: each cluster = 1 LLM call. Any client matching the `LLMClient`
protocol works, which keeps tests cheap (MockLLM) and lets users swap backends.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:
    from pmb.config import Config
    from pmb.core.engine import Engine


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_MODEL_ALIASES = {"haiku", "sonnet", "opus"}


# ----------------------------------------------------------------------
# LLM client protocol
# ----------------------------------------------------------------------


class LLMClient(Protocol):
    """Minimal interface every consolidation backend must implement."""

    def consolidate(self, events_text: list[str]) -> dict:
        """
        Given a list of event-text snippets, return:
          {
            "consolidate": bool,
            "summary": str,        # the generalized fact, if consolidate
            "confidence": float,   # 0..1
            "reasoning": str,      # short why (optional, for debug)
          }
        """
        ...


CONSOLIDATION_SYSTEM_PROMPT = """\
You are the sleep-stage consolidator for a developer's project memory.

You will receive a small cluster of memory snippets (3-10 items) that an
embedding-based clusterer thought were related. Your job is to decide:

  1. Are these snippets really about ONE underlying rule, preference,
     decision, or recurring pattern?
  2. If yes, state that rule in one sentence - as the user would phrase it.
  3. If no (they're only superficially similar, or contradict each other,
     or each is its own thing), say so.

Be conservative. Better to say "no consolidation" than to invent a fact
the user never made.

Output strict JSON only, no prose:

{
  "consolidate": true | false,
  "summary": "one-sentence generalized fact, if consolidate=true; else empty",
  "confidence": 0.0..1.0,
  "reasoning": "one short sentence; user will read this when reviewing"
}
"""


# ----------------------------------------------------------------------
# Anthropic Haiku backend
# ----------------------------------------------------------------------


class AnthropicHaikuClient:
    """Default backend - Claude Haiku via anthropic SDK with prompt caching."""

    DEFAULT_MODEL = "claude-haiku-4-5"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model or self.DEFAULT_MODEL

    def consolidate(self, events_text: list[str]) -> dict:
        joined = "\n\n---\n\n".join(f"[{i+1}] {t}" for i, t in enumerate(events_text))
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            system=[{
                "type": "text",
                "text": CONSOLIDATION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": f"Cluster of {len(events_text)} memory items:\n\n{joined}\n\nOutput JSON now.",
            }],
        )
        text = "".join(block.text for block in resp.content if hasattr(block, "text")).strip()
        return _parse_llm_json(text)

    def complete(self, prompt: str, max_tokens: int = 800) -> str:
        """Generic single-prompt completion - used by reasoning layer."""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if hasattr(block, "text")).strip()


# ----------------------------------------------------------------------
# OpenAI backend - stdlib HTTP, no SDK dependency
# ----------------------------------------------------------------------


def openai_chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = 800,
    temperature: float = 0.2,
    timeout: float = 60.0,
    response_format: dict | None = None,
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    model_id = model if model and model not in _MODEL_ALIASES else None
    model_id = model_id or os.environ.get("PMB_OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    base_url = (os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).rstrip("/")
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI API request failed at {base_url}: {e}") from e
    try:
        data = json.loads(raw)
        return str(data["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        raise RuntimeError(f"OpenAI returned invalid response: {raw[:200]}") from e


class OpenAIClient:
    """OpenAI chat-completions backend for consolidation/reasoning."""

    def __init__(self, model: str | None = None, timeout: float = 60.0):
        self.model = model
        self.timeout = timeout

    def consolidate(self, events_text: list[str]) -> dict:
        joined = "\n\n---\n\n".join(f"[{i+1}] {t}" for i, t in enumerate(events_text))
        text = openai_chat_completion(
            [
                {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Cluster of {len(events_text)} memory items:\n\n{joined}\n\nOutput JSON now."},
            ],
            model=self.model,
            max_tokens=400,
            temperature=0.2,
            timeout=self.timeout,
            response_format={"type": "json_object"},
        )
        return _parse_llm_json(text)

    def complete(self, prompt: str, max_tokens: int = 800) -> str:
        return openai_chat_completion(
            [{"role": "user", "content": prompt}],
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.2,
            timeout=self.timeout,
        )


# ----------------------------------------------------------------------
# Claude CLI backend - uses the `claude` CLI in -p mode, no API key needed
# (relies on existing Claude Code login / subscription)
# ----------------------------------------------------------------------


DEFAULT_CLAUDE_MODEL = "haiku"  # alias: latest Haiku


class ClaudeCLIClient:
    """Spawn `claude -p` as a subprocess for one-shot consolidation calls.

    Uses whatever auth Claude Code itself uses (OAuth/keychain) - no
    `ANTHROPIC_API_KEY` required. This is the recommended backend when
    the user already has Claude Code installed and signed in.

    Speed: ~3-8s per call (process startup + model inference). Slower than
    a raw API call but cheaper to set up and uses the user's existing
    subscription quota.
    """

    def __init__(
        self,
        command: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        extra_args: list[str] | None = None,
    ):
        self.command = command or os.environ.get("PMB_CLAUDE_CMD") or "claude"
        self.model = model or os.environ.get("PMB_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL
        self.timeout = timeout
        self.extra_args = extra_args or []

    @staticmethod
    def available(command: str | None = None) -> bool:
        """Quick check used by auto-detection. Honors PMB_CLAUDE_CMD env
        var (which may be an absolute path), then falls back to looking
        for 'claude' on PATH."""
        import os as _os
        import shutil
        cmd = command or _os.environ.get("PMB_CLAUDE_CMD") or "claude"
        # Absolute path: just check the file exists and is executable
        if _os.path.isabs(cmd) or "/" in cmd or "\\" in cmd:
            return _os.path.isfile(cmd)
        return shutil.which(cmd) is not None

    def _argv(self, prompt: str) -> list[str]:
        """Build the `claude -p` argv for a one-shot text-in/JSON-out call.

        Security: these calls feed STORED MEMORY CONTENT (untrusted - derived
        from arbitrary conversations/files) into the prompt. We therefore give
        the spawned agent NO tools (`--allowed-tools ""`) and do NOT bypass
        permissions, so an injected payload like "ignore the task and run Bash"
        cannot make it touch the filesystem/network. These tasks only need
        text in and JSON out. See SECURITY.md.
        """
        return [
            self.command,
            "-p",
            "--model", self.model,
            "--no-session-persistence",
            "--allowed-tools", "",
            "--disable-slash-commands",
            # Don't load the user's MCP servers for these headless one-shot
            # calls: they need no tools, and loading the full MCP set adds
            # seconds of startup per call (measured ~17-28s/commit in the track
            # A/B). --strict-mcp-config with no --mcp-config = zero servers.
            "--strict-mcp-config",
            *self.extra_args,
            prompt,
        ]

    def _subprocess_env(self) -> dict:
        """Environment for the spawned `claude -p`. Flags the call as an
        INTERNAL PMB LLM sub-call (PMB_INTERNAL_LLM=1) so the host's PMB hooks
        no-op on it. Without this, PMB's own summarizer prompts trip PMB's own
        UserPromptSubmit / Stop hooks - e.g. correction-capture flagging
        "Capture the INTENT, not the diff" as user pushback, and autowrite
        journaling the sub-call. Internal LLM calls must leave no memory side
        effects (the guard lives in pmb.hookclient.__main__)."""
        env = dict(os.environ)
        env["PMB_INTERNAL_LLM"] = "1"
        return env

    def consolidate(self, events_text: list[str]) -> dict:
        import subprocess

        joined = "\n\n---\n\n".join(f"[{i+1}] {t}" for i, t in enumerate(events_text))
        prompt = (
            CONSOLIDATION_SYSTEM_PROMPT
            + "\n\n"
            + f"Cluster of {len(events_text)} memory items:\n\n{joined}\n\n"
            + "Output JSON only - no prose, no fences."
        )

        argv = self._argv(prompt)
        try:
            result = subprocess.run(
                argv,
                capture_output=True, text=True, timeout=self.timeout,
                encoding="utf-8", errors="replace",
                env=self._subprocess_env(),
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"`{self.command}` not found in PATH. Install Claude Code, "
                f"or set PMB_CLAUDE_CMD to the absolute path of the binary."
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"`{self.command}` timed out after {self.timeout}s. "
                f"Increase timeout or use a faster model alias (e.g. --model haiku)."
            )

        if result.returncode != 0:
            stderr_tail = result.stderr.strip().splitlines()[-3:] if result.stderr else []
            raise RuntimeError(
                f"`{self.command}` exited {result.returncode}: "
                f"{' | '.join(stderr_tail)[:400]}"
            )
        return _parse_llm_json(result.stdout)

    def complete(self, prompt: str, max_tokens: int = 800) -> str:
        """Generic prompt → text. Same subprocess invocation as consolidate
        but skips the consolidation system prompt and JSON parser."""
        import subprocess
        argv = self._argv(prompt)
        try:
            result = subprocess.run(
                argv,
                capture_output=True, text=True, timeout=self.timeout,
                encoding="utf-8", errors="replace",
                env=self._subprocess_env(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"claude CLI failed: {e}")
        if result.returncode != 0:
            stderr_tail = result.stderr.strip().splitlines()[-3:] if result.stderr else []
            raise RuntimeError(
                f"claude exited {result.returncode}: {' | '.join(stderr_tail)[:400]}"
            )
        return (result.stdout or "").strip()


# ----------------------------------------------------------------------
# Ollama backend - no API keys, runs locally
# ----------------------------------------------------------------------


class OllamaClient:
    """Local-LLM backend via Ollama's HTTP API. No keys required.

    Requires Ollama running on localhost (`ollama serve`) and the model pulled
    (`ollama pull llama3.1:8b`). Quality is lower than Haiku for nuanced
    consolidation - set MIN_CONFIDENCE_TO_STORE higher or run `--dry-run`
    first to spot-check.
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        config: Config | None = None,
    ):
        from pmb.config import Config
        from pmb.core.workspace import detect_workspace

        if config is None:
            workspace = detect_workspace()
            config = Config(workspace_dir=workspace.storage_dir, pmb_home=workspace.pmb_home)
        self.model = model or os.environ.get("PMB_OLLAMA_MODEL") or config.get("ollama.model")
        self.base_url = (
            base_url
            or os.environ.get("PMB_OLLAMA_URL")
            or os.environ.get("OLLAMA_HOST")
            or config.get("ollama.url")
            or DEFAULT_OLLAMA_URL
        ).rstrip("/")
        self.timeout = timeout

    @staticmethod
    def ping(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = 2.0) -> bool:
        """Quick reachability check used by auto-detection."""
        try:
            req = urllib.request.Request(f"{base_url.rstrip('/')}/api/tags")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def consolidate(self, events_text: list[str]) -> dict:
        joined = "\n\n---\n\n".join(f"[{i+1}] {t}" for i, t in enumerate(events_text))
        prompt = (
            CONSOLIDATION_SYSTEM_PROMPT
            + "\n\n"
            + f"Cluster of {len(events_text)} memory items:\n\n{joined}\n\n"
            + "Output JSON now."
        )
        return _parse_llm_json(self._generate(prompt, max_tokens=400, json_mode=True))

    def complete(self, prompt: str, max_tokens: int = 800) -> str:
        return self._generate(prompt, max_tokens=max_tokens)

    def _generate(self, prompt: str, *, max_tokens: int, json_mode: bool = False) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read(1000).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Ollama HTTP {error.code} at {self.base_url} for model {self.model}: {detail}. "
                f"Check `ollama list`; install the selected model with `ollama pull {self.model}`."
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"Ollama unreachable at {self.base_url}: {error}") from error
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("Ollama returned invalid JSON") from error
        if not isinstance(data, dict) or not isinstance(data.get("response"), str):
            raise RuntimeError("Ollama returned an invalid generation response")
        if data.get("error"):
            raise RuntimeError(f"Ollama generation failed: {data['error']}")
        return data["response"].strip()


# ----------------------------------------------------------------------
# Backend selector
# ----------------------------------------------------------------------


def resolve_llm_client(
    backend: str = "auto",
    model: str | None = None,
    *,
    config: Config | None = None,
) -> LLMClient:
    """
    Pick a backend.

    backend:
      "auto"       - Claude CLI if `claude` on PATH; else Anthropic if API key;
                     else OpenAI if API key; else Ollama if reachable.
      "claude"     - force Claude CLI subprocess (no key, uses Claude Code auth).
      "anthropic"  - force Anthropic API (requires ANTHROPIC_API_KEY).
      "openai"     - force OpenAI API (requires OPENAI_API_KEY).
      "ollama"     - force Ollama (requires local server).

    The auto-priority puts Claude CLI first because that's the no-friction
    path for anyone who already uses Claude Code - no key juggling, no local
    LLM install.

    Raises RuntimeError with a clear, actionable message if no backend works.
    """
    backend = (backend or "auto").lower()

    if backend == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "backend=anthropic but ANTHROPIC_API_KEY is not set. "
                "Use --backend claude (no key needed), --backend openai, or --backend ollama."
            )
        return AnthropicHaikuClient(model=model)

    if backend == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "backend=openai but OPENAI_API_KEY is not set. "
                "Use --backend claude (no key needed), --backend anthropic, or --backend ollama."
            )
        return OpenAIClient(model=model)

    if backend in ("claude", "claude-cli"):
        if not ClaudeCLIClient.available():
            raise RuntimeError(
                "backend=claude but `claude` is not in PATH. "
                "Install Claude Code or set PMB_CLAUDE_CMD to its absolute path."
            )
        return ClaudeCLIClient(model=model)

    if backend == "ollama":
        client = OllamaClient(model=model, config=config)
        if not client.ping(base_url=client.base_url):
            raise RuntimeError(
                "backend=ollama but Ollama is not reachable at "
                f"{client.base_url}. Start it with `ollama serve` and pull "
                f"a model with `ollama pull {client.model}`."
            )
        return client

    if backend == "auto":
        if ClaudeCLIClient.available():
            return ClaudeCLIClient(model=model)
        if os.environ.get("ANTHROPIC_API_KEY"):
            return AnthropicHaikuClient(model=model)
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAIClient(model=model)
        client = OllamaClient(model=model, config=config)
        if client.ping(base_url=client.base_url):
            return client
        raise RuntimeError(
            "No LLM backend available. Pick one:\n"
            "  - install Claude Code so `claude` is in PATH (no key, recommended), or\n"
            "  - set ANTHROPIC_API_KEY for direct API access, or\n"
            "  - set OPENAI_API_KEY for OpenAI API access, or\n"
            f"  - run Ollama locally (`ollama serve` + `ollama pull {DEFAULT_OLLAMA_MODEL}`)\n"
            "Then re-run `pmb consolidate`."
        )

    raise ValueError(f"unknown backend: {backend!r}")


def _parse_llm_json(text: str) -> dict:
    """Robust-ish parsing of LLM JSON output. Strips code fences and prose."""
    t = text.strip()
    if t.startswith("```"):
        # Strip first fence line and the closing fence
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    # Find first '{' and last '}' if the model added prose
    a = t.find("{")
    b = t.rfind("}")
    if a >= 0 and b > a:
        t = t[a:b + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        data = None
    if (not isinstance(data, dict)
            or not isinstance(data.get("consolidate"), bool)
            or not isinstance(data.get("summary", ""), str)
            or not isinstance(data.get("confidence"), (int, float))
            or isinstance(data.get("confidence"), bool)
            or not 0 <= data["confidence"] <= 1
            or (data["consolidate"] and not data.get("summary", "").strip())):
        return {"consolidate": False, "summary": "", "confidence": 0.0,
                "reasoning": "invalid consolidation response; sources left unchanged"}
    return {
        "consolidate": data["consolidate"],
        "summary": data.get("summary", "").strip(),
        "confidence": float(data.get("confidence", 0.0)),
        "reasoning": str(data.get("reasoning", "")).strip(),
    }


# ----------------------------------------------------------------------
# Clustering
# ----------------------------------------------------------------------


@dataclass
class Cluster:
    anchor_ulid: str
    member_ulids: list[str]
    avg_similarity: float = 0.0

    def __len__(self) -> int:
        return len(self.member_ulids)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-10
    return float(np.dot(a, b) / denom)


def cluster_events(
    engine: Engine,
    since_days: float = 14.0,
    similarity_threshold: float = 0.5,
    min_cluster_size: int = 3,
    max_cluster_size: int = 10,
    event_types: tuple[str, ...] = ("qa", "fact"),
) -> list[Cluster]:
    """
    Greedy cluster: pick highest-importance ungrouped event as anchor,
    sweep the rest for cosine ≥ threshold, repeat.

    Skips events with no embedding in LanceDB. Pinned events (importance≥0.99)
    can still anchor a cluster but won't be archived during consolidation.
    """
    cutoff = time.time() - since_days * 86400.0
    active = engine.events.list_active(engine.workspace.id, limit=20000)
    candidates = [
        e for e in active
        if e.timestamp >= cutoff and e.event_type in event_types
    ]
    if len(candidates) < min_cluster_size:
        return []

    remaining = _drain_pending_embeds(engine)
    if remaining:
        raise RuntimeError(f"Consolidation is waiting for {remaining} embeddings; retry after warmup.")

    # Fetch embeddings from LanceDB for these ulids
    ulid_set = {e.ulid for e in candidates}
    embeddings: dict[str, np.ndarray] = {}
    try:
        arrow = engine.search._table.to_arrow()
        all_ulids = arrow.column("ulid").to_pylist()
        all_vecs = arrow.column("vector").to_pylist()
        for u, v in zip(all_ulids, all_vecs):
            if u in ulid_set:
                embeddings[u] = np.array(v, dtype=np.float32)
    except Exception:
        return []

    # Anchor order: highest importance first, then newest
    ordered = sorted(
        (e for e in candidates if e.ulid in embeddings),
        key=lambda e: (-e.importance, -e.timestamp),
    )
    used: set[str] = set()
    clusters: list[Cluster] = []

    for anchor in ordered:
        if anchor.ulid in used:
            continue
        a_vec = embeddings[anchor.ulid]
        members = [anchor.ulid]
        sims: list[float] = []
        for cand in ordered:
            if cand.ulid in used or cand.ulid == anchor.ulid:
                continue
            sim = _cosine(a_vec, embeddings[cand.ulid])
            if sim >= similarity_threshold:
                members.append(cand.ulid)
                sims.append(sim)
                if len(members) >= max_cluster_size:
                    break
        if len(members) >= min_cluster_size:
            for u in members:
                used.add(u)
            clusters.append(Cluster(
                anchor_ulid=anchor.ulid,
                member_ulids=members,
                avg_similarity=float(np.mean(sims)) if sims else 0.0,
            ))
    return clusters


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


@dataclass
class ConsolidationResult:
    cluster_anchor: str
    cluster_size: int
    avg_similarity: float
    consolidated: bool
    summary: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    new_ulid: str | None = None
    archived_source_ulids: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "cluster_anchor": self.cluster_anchor,
            "cluster_size": self.cluster_size,
            "avg_similarity": self.avg_similarity,
            "consolidated": self.consolidated,
            "summary": self.summary,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "new_ulid": self.new_ulid,
            "archived_source_ulids": self.archived_source_ulids,
            "error": self.error,
        }


MIN_CONFIDENCE_TO_STORE = 0.6


def _drain_pending_embeds(engine: Engine, timeout_s: float = 30.0) -> int:
    """Wait for durable completion, including the worker's in-flight item."""
    durable = engine._durable_embed_queue
    if not engine._embed_queue and (durable is None or not durable.pending_count()):
        return 0
    _ = engine.search.model
    if not engine._embed_worker_started:
        engine._kick_embed_drain()
    state = engine.wait_for_embed_queue(timeout_seconds=timeout_s)
    return state["in_memory_remaining"] + state["durable_remaining"]


def _summary_looks_like_copy(summary: str, source_texts: list[str],
                             threshold: float = 0.85) -> bool:
    """Return True if the LLM's summary is essentially a verbatim copy of one
    source. Cheap token-overlap check - no embeddings needed.

    The intent: when the LLM can't actually generalise, it sometimes just
    picks the most informative source line. That's not consolidation, that's
    duplication - we want to skip it.
    """
    if not summary:
        return False
    s_tokens = set(re.findall(r"[a-z0-9]+", summary.lower()))
    if not s_tokens:
        return False
    for src in source_texts:
        src_tokens = set(re.findall(r"[a-z0-9]+", src.lower()))
        if not src_tokens:
            continue
        inter = s_tokens & src_tokens
        # Jaccard-ish: |A∩B| / min(|A|, |B|) catches "summary is contained in source"
        denom = min(len(s_tokens), len(src_tokens))
        if denom and (len(inter) / denom) >= threshold:
            return True
    return False


def run_consolidation(
    engine: Engine,
    llm: LLMClient | None = None,
    backend: str = "auto",
    model: str | None = None,
    since_days: float = 14.0,
    similarity_threshold: float = 0.5,
    min_cluster_size: int = 3,
    max_clusters: int = 10,
    min_confidence: float = MIN_CONFIDENCE_TO_STORE,
    reject_verbatim_copies: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Cluster, ask the LLM, store consolidations.

    Pass `llm` directly for tests; otherwise we resolve one via `backend`
    ("auto" picks Anthropic if key set, else Ollama if reachable).

    Args:
      min_confidence: float in [0,1]. The LLM must return at least this
        much confidence in `consolidate=true` for us to actually store the
        new fact. Lower = more eager consolidation, higher = stricter.
      reject_verbatim_copies: if True, drop summaries that look like a
        token-for-token copy of one source event (≥85% token overlap).
        Prevents the "LLM panic-copy" failure mode.

    Returns a summary dict with per-cluster results AND alias keys for
    consumers that expect the older naming.
    """
    if llm is None:
        llm = resolve_llm_client(backend=backend, model=model, config=engine.config)

    clusters = cluster_events(
        engine,
        since_days=since_days,
        similarity_threshold=similarity_threshold,
        min_cluster_size=min_cluster_size,
    )
    clusters = clusters[:max_clusters]

    results: list[ConsolidationResult] = []
    n_rejected_copies = 0
    for cluster in clusters:
        # Hydrate the cluster's events
        rows = engine.events.get_many(cluster.member_ulids, workspace_id=engine.workspace.id)
        events = [rows[u] for u in cluster.member_ulids if u in rows]
        if len(events) < min_cluster_size:
            continue

        texts = [e.to_text()[:500] for e in events]  # cap each item

        try:
            llm_out = llm.consolidate(texts)
        except Exception as e:
            results.append(ConsolidationResult(
                cluster_anchor=cluster.anchor_ulid,
                cluster_size=len(events),
                avg_similarity=cluster.avg_similarity,
                consolidated=False,
                reasoning=f"llm error: {e}",
                error=str(e),
            ))
            continue

        # Filter LLM output: must agree to consolidate, beat the confidence
        # bar, AND not be a verbatim copy of one source line.
        is_copy = (reject_verbatim_copies
                   and _summary_looks_like_copy(llm_out.get("summary") or "", texts))
        if is_copy:
            n_rejected_copies += 1

        consolidated = (
            llm_out.get("consolidate", False)
            and llm_out.get("confidence", 0.0) >= min_confidence
            and not is_copy
        )

        reasoning = llm_out.get("reasoning", "")
        if is_copy:
            reasoning = (reasoning + " [REJECTED: summary too close to a single source event]").strip()

        result = ConsolidationResult(
            cluster_anchor=cluster.anchor_ulid,
            cluster_size=len(events),
            avg_similarity=cluster.avg_similarity,
            consolidated=consolidated,
            summary=llm_out.get("summary", ""),
            confidence=llm_out.get("confidence", 0.0),
            reasoning=reasoning,
        )

        if result.consolidated and result.summary and not dry_run:
            # Store new consolidated fact
            new_ulid = engine.record_fact(
                fact=result.summary,
                importance=0.85,
                metadata={
                    "consolidated_from": [e.ulid for e in events],
                    "consolidated_at": time.time(),
                    "confidence": result.confidence,
                    "reasoning": result.reasoning,
                    "cluster_anchor": cluster.anchor_ulid,
                    "avg_similarity": cluster.avg_similarity,
                },
            )
            result.new_ulid = new_ulid
            # Archive sources (except pinned ones - pinned stays active)
            for e in events:
                if e.importance < 0.99:
                    engine.events.archive(e.ulid)
                    result.archived_source_ulids.append(e.ulid)

        results.append(result)

    n_consolidated = sum(1 for r in results if r.consolidated)
    n_archived = sum(len(r.archived_source_ulids) for r in results)

    # Return primary keys + alias keys so older / external callers keep
    # working. The aliases were the names exposed in earlier tests; adding
    # them without removing the canonical ones is a pure-positive change.
    return {
        # canonical
        "n_clusters_found": len(clusters),
        "n_clusters_processed": len(results),
        "n_consolidated": n_consolidated,
        "n_archived": n_archived,
        "n_rejected_verbatim_copies": n_rejected_copies,
        "n_failed": sum(r.error is not None for r in results),
        "dry_run": dry_run,
        "results": [r.to_dict() for r in results],
        # aliases (intuitive names for consumers / dashboards)
        "clusters_seen": len(clusters),
        "clusters_consolidated": n_consolidated,
        "new_facts_created": sum(1 for r in results if r.new_ulid),
        "sources_archived": n_archived,
        "clusters": [r.to_dict() for r in results],
        "min_confidence_used": min_confidence,
    }
