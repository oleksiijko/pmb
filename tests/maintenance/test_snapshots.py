"""Snapshots preserve committed WAL data and refuse damaged recovery inputs."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pmb.cli.main import app
from pmb.core.snapshots import create_snapshot, restore_snapshot, snapshot_path, verify_snapshot
from pmb.core.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    workspace = Workspace(id="backup-test", name="Backup", root=tmp_path, pmb_home=tmp_path)
    workspace.ensure_dirs()
    return workspace


def test_snapshot_captures_live_wal_and_restores_exact_files(workspace):
    with closing(sqlite3.connect(workspace.db_path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE memory (content TEXT)")
        connection.execute("INSERT INTO memory VALUES ('before')")
        connection.commit()
        (workspace.storage_dir / "config.yaml").write_text("setting: before")
        snapshot = create_snapshot(workspace, "working WAL")
        assert not (snapshot / "events.sqlite-wal").exists()
        manifest = verify_snapshot(snapshot, workspace.id)
        assert manifest["note"] == "working WAL"
        connection.execute("INSERT INTO memory VALUES ('after')")
        connection.commit()
    (workspace.storage_dir / "stale.txt").write_text("must disappear on restore")
    (workspace.storage_dir / "config.yaml").write_text("setting: after")
    safety = restore_snapshot(workspace, snapshot.name)
    with closing(sqlite3.connect(workspace.db_path)) as restored:
        assert restored.execute("SELECT content FROM memory").fetchall() == [("before",)]
    assert not (workspace.storage_dir / "stale.txt").exists()
    assert (workspace.storage_dir / "config.yaml").read_text() == "setting: before"
    assert (safety / "stale.txt").read_text() == "must disappear on restore"
    verify_snapshot(safety, workspace.id)
    with closing(sqlite3.connect(safety / "events.sqlite")) as previous:
        assert previous.execute("SELECT count(*) FROM memory").fetchone()[0] == 2


@pytest.mark.parametrize("damage", ["change", "remove", "extra", "manifest", "foreign"])
def test_damaged_snapshot_never_changes_live_workspace(workspace, damage):
    live = workspace.storage_dir / "memory.txt"
    live.write_text("original")
    snapshot = create_snapshot(workspace)
    live.write_text("current")
    if damage == "change":
        (snapshot / "memory.txt").write_text("damaged")
    elif damage == "remove":
        (snapshot / "memory.txt").unlink()
    elif damage == "extra":
        (snapshot / "extra").write_text("unexpected")
    elif damage == "manifest":
        (snapshot / "snapshot.json").write_text("[]")
    else:
        manifest = json.loads((snapshot / "snapshot.json").read_text())
        manifest["workspace"] = "other"
        (snapshot / "snapshot.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore_snapshot(workspace, snapshot.name)
    assert live.read_text() == "current"


@pytest.mark.parametrize("name", ["..", ".", "../outside", "a/../b", "a\\b", "/tmp", ""])
def test_snapshot_id_cannot_escape_workspace(workspace, name):
    with pytest.raises(ValueError):
        snapshot_path(workspace, name)


def test_failed_creation_is_not_listed(workspace, monkeypatch):
    (workspace.storage_dir / "file").write_text("memory")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("pmb.core.snapshots.shutil.copy2", fail)
    with pytest.raises(OSError, match="disk full"):
        create_snapshot(workspace)
    assert list((workspace.storage_dir / "snapshots").iterdir()) == []


def test_restore_rolls_back_on_failed_install(workspace, monkeypatch):
    live = workspace.storage_dir / "file"
    live.write_text("old")
    snapshot = create_snapshot(workspace)
    live.write_text("current")
    rename = Path.rename
    def fail_incoming(self, target):
        if self.parent.name == "incoming":
            raise OSError("install failed")
        return rename(self, target)
    monkeypatch.setattr(Path, "rename", fail_incoming)
    with pytest.raises(OSError, match="install failed"):
        restore_snapshot(workspace, snapshot.name)
    assert live.read_text() == "current"
    assert len(list((workspace.storage_dir / "snapshots").glob("pre-restore-*"))) == 1


def test_snapshot_cli_json_verify_and_running_server_guard(workspace, monkeypatch):
    monkeypatch.setattr("pmb.cli.commands.snapshot.detect_workspace", lambda: workspace)
    runner = CliRunner()
    assert runner.invoke(app, ["snapshot", "create", "--note", "safe"]).exit_code == 0
    listing = runner.invoke(app, ["snapshot", "list", "--json"])
    entry, = json.loads(listing.stdout)
    assert entry["note"] == "safe"
    assert runner.invoke(app, ["snapshot", "verify", entry["id"]]).exit_code == 0
    monkeypatch.setattr("pmb.mcp.registry.list_servers", lambda **kwargs: [{"alive": True}])
    result = runner.invoke(app, ["snapshot", "restore", entry["id"], "--yes"])
    assert result.exit_code == 1
    assert "Stop running PMB servers" in result.stdout


def test_snapshot_rejects_symlink_content(workspace, tmp_path):
    outside = tmp_path / "private"
    outside.write_text("outside workspace")
    link = workspace.storage_dir / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation requires privileges on this platform")
    with pytest.raises(ValueError, match="symlink"):
        create_snapshot(workspace)
    assert outside.read_text() == "outside workspace"


def test_legacy_snapshot_checks_database_without_checksums(workspace):
    snapshot = create_snapshot(workspace)
    manifest = json.loads((snapshot / "snapshot.json").read_text())
    manifest.pop("format_version")
    manifest.pop("files")
    (snapshot / "snapshot.json").write_text(json.dumps(manifest))
    assert verify_snapshot(snapshot, workspace.id)["id"] == snapshot.name


def test_invalid_sqlite_never_publishes_a_snapshot(workspace):
    workspace.db_path.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        create_snapshot(workspace)
    assert list((workspace.storage_dir / "snapshots").iterdir()) == []


def test_cancelled_restore_leaves_workspace_unchanged(workspace, monkeypatch):
    monkeypatch.setattr("pmb.cli.commands.snapshot.detect_workspace", lambda: workspace)
    monkeypatch.setattr("pmb.mcp.registry.list_servers", lambda **kwargs: [])
    live = workspace.storage_dir / "file"
    live.write_text("original")
    snapshot = create_snapshot(workspace)
    live.write_text("current")
    result = CliRunner().invoke(app, ["snapshot", "restore", snapshot.name], input="n\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.stdout
    assert live.read_text() == "current"
