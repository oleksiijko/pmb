"""Local snapshot creation, inspection, verification, and recovery."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer
from rich.markup import escape as esc
from rich.table import Table

from pmb.cli._common import console
from pmb.core.snapshots import create_snapshot, restore_snapshot, snapshot_path, verify_snapshot
from pmb.core.workspace import detect_workspace

snapshot_app = typer.Typer(help="Verified local snapshots of your workspace (no cloud).")


@snapshot_app.command("create")
def snapshot_create(
    note: str | None = typer.Option(None, "--note", "-m", help="Label for this snapshot"),
):
    """Back up SQLite consistently, copy workspace files, and record checksums."""
    try:
        destination = create_snapshot(detect_workspace(), note or "")
    except (OSError, ValueError, sqlite3.Error) as error:
        console.print(f"[red]Snapshot failed:[/] {esc(str(error))}")
        raise typer.Exit(1) from error
    console.print(f"[green]Snapshot[/] [cyan]{destination.name}[/] created and verified"
                  + (f" — {esc(note)}" if note else ""))
    console.print(f"[dim]{esc(str(destination))}[/]")


@snapshot_app.command("list")
def snapshot_list(
    json_output: bool = typer.Option(False, "--json", help="Print snapshot metadata as JSON"),
):
    """List complete local snapshots; mark unreadable manifests as invalid."""
    workspace = detect_workspace()
    root = workspace.storage_dir / "snapshots"
    directories = sorted(
        (path for path in root.iterdir()
         if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")),
        reverse=True,
    ) if root.exists() else []
    items = []
    for directory in directories:
        try:
            manifest = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("Invalid manifest")
            items.append({
                "id": directory.name,
                "created_at": manifest.get("created_at", "?"),
                "note": manifest.get("note", ""),
                "format_version": manifest.get("format_version", 0),
            })
        except (OSError, ValueError):
            items.append({"id": directory.name, "created_at": "?", "note": "Invalid manifest",
                          "format_version": None})
    if json_output:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        console.print("[yellow]No snapshots yet.[/] Create one: [cyan]pmb snapshot create[/]")
        return
    table = Table(title=f"Snapshots ({len(items)})")
    table.add_column("ID", style="cyan")
    table.add_column("Created", style="dim")
    table.add_column("Format")
    table.add_column("Note")
    for item in items:
        version = item["format_version"]
        label = {1: "checksummed", 0: "legacy"}.get(version, "invalid") if isinstance(version, int) else "invalid"
        table.add_row(esc(str(item["id"])), esc(str(item["created_at"])), label, esc(str(item["note"])))
    console.print(table)


@snapshot_app.command("verify")
def snapshot_verify(
    snapshot_id: str = typer.Argument(..., help="Snapshot ID (see `pmb snapshot list`)"),
):
    """Check files, workspace identity, and SQLite integrity without restoring."""
    workspace = detect_workspace()
    try:
        manifest = verify_snapshot(snapshot_path(workspace, snapshot_id), workspace.id)
    except (OSError, ValueError, sqlite3.Error) as error:
        console.print(f"[red]Verification failed:[/] {esc(str(error))}")
        raise typer.Exit(1) from error
    console.print(f"[green]Verified[/] {esc(snapshot_id)}")
    if not manifest.get("format_version"):
        console.print("[yellow]Legacy snapshot: SQLite checked, file checksums unavailable.[/]")


@snapshot_app.command("restore")
def snapshot_restore(
    snapshot_id: str = typer.Argument(..., help="Snapshot ID (see `pmb snapshot list`)"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Verify and restore a snapshot; save the current state first. Stop all writers first."""
    from pmb.mcp.registry import list_servers

    workspace = detect_workspace()
    if any(server["alive"] for server in list_servers(with_rss=False)):
        console.print("[red]Stop running PMB servers before restoring.[/] Use `pmb daemon stop` "
                      "and close agent connections and dashboards.")
        raise typer.Exit(1)
    try:
        verify_snapshot(snapshot_path(workspace, snapshot_id), workspace.id)
        console.print(f"[yellow]This replaces current memory with snapshot {esc(snapshot_id)}.[/]")
        if not yes and not typer.confirm("All PMB writers stopped? Restore with a safety backup?"):
            console.print("[yellow]Cancelled.[/]")
            return
        safety: Path = restore_snapshot(workspace, snapshot_id)
    except (OSError, ValueError, sqlite3.Error) as error:
        console.print(f"[red]Restore failed:[/] {esc(str(error))}")
        raise typer.Exit(1) from error
    console.print(f"[green]Restored[/] {esc(snapshot_id)}. "
                  f"[dim]Previous state: {safety.name}[/]")
    console.print("[dim]Restart PMB/agents to reopen the restored workspace.[/]")
