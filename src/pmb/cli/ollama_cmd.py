"""`pmb ollama` subcommand - status / configure / recommend models.

For users who want a fully-local setup (no Anthropic / OpenAI API key).
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pmb.config import Config
from pmb.core.workspace import detect_workspace
from pmb.health.consolidate import OllamaClient

console = Console()
app = typer.Typer(no_args_is_help=True, help="Ollama integration - fully local LLM ops")


# Recommended models for different PMB ops
_RECOMMENDED = {
    "small": ("llama3.2:3b", "fastest, good enough for dedup verify"),
    "balanced": ("llama3.1:8b", "default - quality vs speed balance"),
    "quality": ("qwen2.5:14b", "best dedup/consolidation accuracy, slower"),
    "tiny": ("gemma3:1b", "1B params, runs on potato hardware"),
}


def _ollama_url() -> str:
    return OllamaClient().base_url


def _ping(url: str | None = None) -> bool:
    return OllamaClient.ping(base_url=url or _ollama_url())


def _list_models(url: str | None = None) -> list[dict]:
    url = url or _ollama_url()
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("models", []) or []
    except Exception:
        return []


@app.command()
def status():
    """Show Ollama health, configured URL, available models, and which PMB
    operations would use Ollama with current config.
    """
    url = _ollama_url()
    ok = _ping(url)
    console.print(f"[bold]Ollama URL:[/] {url}")
    if not ok:
        console.print("[red]Status: not reachable[/]")
        console.print("\n[yellow]To start Ollama:[/]")
        console.print("  1. Install:    https://ollama.com/download")
        console.print("  2. Run:        [cyan]ollama serve[/]")
        console.print(f"  3. Pull model: [cyan]ollama pull {_RECOMMENDED['balanced'][0]}[/]")
        raise typer.Exit(1)
    console.print("[green]Status: online ✓[/]\n")

    models = _list_models(url)
    if models:
        table = Table(title="Installed models", show_header=True)
        table.add_column("Name")
        table.add_column("Size", justify="right")
        table.add_column("Modified")
        for m in models:
            size_gb = m.get("size", 0) / (1024**3)
            table.add_row(
                m.get("name", "?"),
                f"{size_gb:.1f} GB",
                m.get("modified_at", "")[:10],
            )
        console.print(table)
    else:
        console.print("[yellow]No models installed.[/] Pull one:")
        for level, (name, desc) in _RECOMMENDED.items():
            console.print(f"  [cyan]ollama pull {name}[/]  - {desc}")

    # Show which PMB ops would use Ollama
    cfg = Config(
        workspace_dir=detect_workspace().storage_dir,
        pmb_home=Path(os.environ.get("PMB_HOME") or (Path.home() / ".pmb")),
    )
    console.print("\n[bold]PMB ops with Ollama-eligible backend:[/]")
    ops = [
        ("consolidate", cfg.get("consolidate.backend")),
        ("chat",        cfg.get("chat.transport")),
        ("dedup verify", "via run_pending --backend ollama"),
    ]
    for name, backend in ops:
        marker = "✓" if backend in ("auto", "ollama") else "✗"
        color = "green" if backend in ("auto", "ollama") else "dim"
        console.print(f"  [{color}]{marker} {name:<20s} backend={backend}[/]")


@app.command()
def use(
    model: str = typer.Argument(
        "balanced",
        help="Model preset (small/balanced/quality/tiny) or explicit ollama tag",
    ),
    backend_for: str = typer.Option(
        "all",
        "--for",
        help="Which PMB ops: all / consolidate / chat / dedup",
    ),
):
    """Configure PMB to use Ollama by default.

    Examples:
      pmb ollama use balanced            # llama3.1:8b for all LLM ops
      pmb ollama use quality --for consolidate
      pmb ollama use llama3.2:3b
    """
    # Resolve preset → model name
    if model in _RECOMMENDED:
        model_name = _RECOMMENDED[model][0]
    else:
        model_name = model

    cfg = Config(
        workspace_dir=detect_workspace().storage_dir,
        pmb_home=Path(os.environ.get("PMB_HOME") or (Path.home() / ".pmb")),
    )
    if backend_for not in {"all", "consolidate", "chat", "dedup"}:
        raise typer.BadParameter("Choose all, consolidate, chat, or dedup.", param_hint="--for")
    cfg.set_global("ollama.model", model_name)

    if backend_for in ("all", "consolidate"):
        cfg.set_global("consolidate.backend", "ollama")
        console.print(f"[green]✓[/] consolidate.backend = ollama, model = {model_name}")
    if backend_for in ("all", "chat"):
        cfg.set_global("chat.transport", "ollama")
        console.print("[green]✓[/] chat.transport = ollama")
    if backend_for in ("all", "dedup"):
        console.print("[green]✓[/] dedup verify will use ollama via `pmb dedupe --backend ollama`")

    console.print("\nSaved to global config: [dim]~/.pmb/config.yaml[/]")
    if not _ping():
        console.print("\n[yellow]Note:[/] Ollama is not running. Start it before using PMB ops:")
        console.print(f"  [cyan]ollama serve &  ollama pull {model_name}[/]")


@app.command()
def recommend():
    """Show recommended models for different RAM/quality tradeoffs."""
    table = Table(title="Recommended Ollama models for PMB", show_header=True)
    table.add_column("Preset")
    table.add_column("Model")
    table.add_column("Size")
    table.add_column("Description")
    table.add_row("tiny",     _RECOMMENDED["tiny"][0],     "~1 GB",  _RECOMMENDED["tiny"][1])
    table.add_row("small",    _RECOMMENDED["small"][0],    "~2 GB",  _RECOMMENDED["small"][1])
    table.add_row("balanced", _RECOMMENDED["balanced"][0], "~5 GB",  _RECOMMENDED["balanced"][1] + "  (recommended)")
    table.add_row("quality",  _RECOMMENDED["quality"][0],  "~9 GB",  _RECOMMENDED["quality"][1])
    console.print(table)
    console.print("\nQuickstart:")
    console.print("  [cyan]ollama serve &[/]")
    console.print("  [cyan]ollama pull llama3.1:8b[/]")
    console.print("  [cyan]pmb ollama use balanced[/]")


@app.command()
def test():
    """Quick smoke-test: ping Ollama, ask a 1-shot question, verify response."""
    url = _ollama_url()
    if not _ping(url):
        console.print(f"[red]Ollama not reachable at {url}[/]")
        raise typer.Exit(1)

    model = OllamaClient().model
    console.print(f"[bold]Testing {model} at {url}...[/]\n")

    import time
    t0 = time.perf_counter()
    try:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{url.rstrip('/')}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        dt = (time.perf_counter() - t0) * 1000
        content = data.get("message", {}).get("content", "")
        console.print(f"[green]✓ Response in {dt:.0f}ms:[/]")
        console.print(f"  {content!r}")
        if "PONG" in content.upper():
            console.print("\n[bold green]All good. Ollama is configured correctly for PMB.[/]")
        else:
            console.print("\n[yellow]Got response but not PONG. Model may need a different prompt.[/]")
    except Exception as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1)
