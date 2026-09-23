"""Local workspace snapshots with SQLite online backup and integrity checks."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pmb.core.workspace import Workspace


def snapshot_path(workspace: Workspace, snapshot_id: str) -> Path:
    root = workspace.storage_dir / "snapshots"
    if (not snapshot_id or snapshot_id in {".", ".."}
            or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id.startswith(".")):
        raise ValueError("Snapshot ID must be a name from `pmb snapshot list`.")
    path = root / snapshot_id
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != root.resolve():
        raise ValueError(f"Snapshot not found: {snapshot_id}")
    return path


def _checksums(directory: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Snapshots cannot contain symlinks: {path.name}")
        if path.is_file() and path != directory / "snapshot.json":
            with path.open("rb") as source:
                hashes[path.relative_to(directory).as_posix()] = hashlib.file_digest(
                    source, "sha256").hexdigest()
    return hashes


def verify_snapshot(directory: Path, workspace_id: str) -> dict:
    """Reject damaged or foreign snapshots before any live data is changed."""
    manifest = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("workspace") != workspace_id:
        raise ValueError("Snapshot belongs to a different workspace or has an invalid manifest.")
    version = manifest.get("format_version", 0)
    if version not in (0, 1):
        raise ValueError(f"Unsupported snapshot format: {version}")
    hashes = _checksums(directory)
    if version == 1 and hashes != manifest.get("files"):
        raise ValueError("Snapshot checksum mismatch: files are missing, changed, or unexpected.")
    for name in hashes:
        if name.endswith(".sqlite"):
            # New backups are self-contained; legacy copies may need their WAL
            # replayed to verify the actual committed database state.
            mode = "immutable=1" if version == 1 else "mode=ro"
            uri = (directory / name).resolve().as_uri() + f"?{mode}"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                errors = connection.execute("PRAGMA integrity_check").fetchall()
            if errors != [("ok",)]:
                raise ValueError(f"SQLite integrity check failed: {name}")
    return manifest


def create_snapshot(workspace: Workspace, note: str = "", *, prefix: str = "") -> Path:
    """Publish a complete snapshot only after every copied file has been verified."""
    workspace.ensure_dirs()
    root = workspace.storage_dir / "snapshots"
    if root.is_symlink():
        raise ValueError("Snapshot directory must not be a symlink.")
    root.mkdir(exist_ok=True)
    stamp = prefix + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    destination = root / stamp
    with tempfile.TemporaryDirectory(prefix=".creating-", dir=root) as temporary:
        staging = Path(temporary)
        for item in workspace.storage_dir.iterdir():
            if item.name == "snapshots" or item.name.endswith(("-wal", "-shm", "-journal")):
                continue
            if item.is_symlink():
                raise ValueError(f"Workspace contains a symlink: {item.name}")
            target = staging / item.name
            if item.is_file() and item.suffix == ".sqlite":
                with closing(sqlite3.connect(item.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(target)) as backup:
                        source.backup(backup)
                        backup.execute("PRAGMA journal_mode=DELETE")
            elif item.is_dir():
                shutil.copytree(item, target, symlinks=True)
            else:
                shutil.copy2(item, target)
        manifest = {
            "format_version": 1,
            "id": stamp,
            "created_at": datetime.now(UTC).isoformat(),
            "workspace": workspace.id,
            "note": note,
            "files": _checksums(staging),
        }
        (staging / "snapshot.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        verify_snapshot(staging, workspace.id)
        staging.rename(destination)
    return destination


def restore_snapshot(workspace: Workspace, snapshot_id: str) -> Path:
    """Restore a verified snapshot, retaining a verified safety copy and rollback on I/O errors.

    The caller must stop writers first. SQLite backup makes creation safe for a
    live database; replacing a workspace requires exclusive use of its files.
    """
    source = snapshot_path(workspace, snapshot_id)
    verify_snapshot(source, workspace.id)
    root = workspace.storage_dir / "snapshots"
    with tempfile.TemporaryDirectory(prefix=".restoring-", dir=root) as temporary:
        staging = Path(temporary) / "incoming"
        shutil.copytree(source, staging)
        verify_snapshot(staging, workspace.id)
        safety = create_snapshot(workspace, f"Before restoring {snapshot_id}", prefix="pre-restore-")
        rollback = Path(temporary) / "previous"
        rollback.mkdir()
        installed: list[Path] = []
        moved: list[Path] = []
        try:
            for item in workspace.storage_dir.iterdir():
                if item.name != "snapshots":
                    target = rollback / item.name
                    item.rename(target)
                    moved.append(target)
            for item in staging.iterdir():
                if item.name != "snapshot.json":
                    target = workspace.storage_dir / item.name
                    item.rename(target)
                    installed.append(target)
        except OSError:
            for item in installed:
                item.rename(staging / item.name)
            for item in moved:
                item.rename(workspace.storage_dir / item.name)
            raise
    return safety
