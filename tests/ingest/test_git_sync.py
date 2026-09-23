"""Tests for git-backed workspace sync (killer feature).

Uses a local bare repo as the 'remote' so there's no network. Skips
cleanly if git isn't installed.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pmb.core.git_sync import WorkspaceGitSync, clone_workspace

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _seed_workspace(d: Path):
    """Create a minimal workspace dir that looks like real PMB storage."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.sqlite").write_bytes(b"SQLite format 3\x00fake-db-bytes")
    (d / "meta.yaml").write_text("id: test\nname: Test WS\n", encoding="utf-8")
    (d / "bm25_index.pkl").write_bytes(b"\x80\x04fake-pickle")
    (d / "vocab_bridges.json").write_text('{"foo": ["bar"]}', encoding="utf-8")
    lance = d / "vectors.lance"
    lance.mkdir(exist_ok=True)
    (lance / "data.bin").write_bytes(b"vector-bytes")


def _make_bare_remote(d: Path) -> str:
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(d)], check=True,
                   capture_output=True, text=True)
    return str(d)


@pytest.fixture
def env(monkeypatch):
    """Yield (workspace_dir, remote_url, pmb_home) on a temp tree with git
    identity configured so commits don't fail in CI."""
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "gitconfig"))
        # Git identity + default branch BEFORE creating any repo, so the bare
        # remote advertises `main` as its default (matches GitHub). Without
        # this, a runner whose git defaults to `master` makes the bare HEAD
        # point at a missing branch and clones produce an empty working tree.
        for k, v in [("user.email", "ci@pmb.test"), ("user.name", "PMB CI"),
                     ("init.defaultBranch", "main")]:
            subprocess.run(["git", "config", "--global", k, v],
                           capture_output=True, text=True, check=True)
        ws = root / "workspaces" / "test"
        _seed_workspace(ws)
        remote = _make_bare_remote(root / "remote.git")
        yield ws, remote, root


def test_init_creates_repo(env):
    ws, remote, _ = env
    sync = WorkspaceGitSync(ws)
    assert not sync.is_git()
    res = sync.init(remote=remote)
    assert res.ok
    assert sync.is_git()
    assert (ws / ".gitignore").exists()


def test_init_lean_excludes_caches(env):
    ws, remote, _ = env
    sync = WorkspaceGitSync(ws)
    sync.init(remote=remote, include_cache=False)
    gi = (ws / ".gitignore").read_text(encoding="utf-8")
    assert "bm25_index.pkl" in gi
    assert "vocab_bridges.json" in gi


def test_push_commits_and_pushes(env):
    ws, remote, _ = env
    sync = WorkspaceGitSync(ws)
    sync.init(remote=remote)
    res = sync.push(remote="origin", branch="main")
    assert res.ok, res.detail
    assert res.extra["committed"] is True
    assert res.extra["pushed"] is True


def test_push_auto_inits(env):
    ws, remote, _ = env
    sync = WorkspaceGitSync(ws)
    # no explicit init - push should bootstrap the repo
    # but there's no remote yet, so set it via init first for the push path
    sync.init(remote=remote)
    sync.push()
    # second push with no changes → up to date, still ok
    res2 = sync.push()
    assert res2.ok


def test_status_reports_clean_after_push(env):
    ws, remote, _ = env
    sync = WorkspaceGitSync(ws)
    sync.init(remote=remote)
    sync.push()
    st = sync.status()
    assert st.ok
    assert st.extra["dirty"] is False
    assert st.extra["remote"] == remote


def test_full_mode_tracks_lance_dir(env):
    ws, remote, _ = env
    sync = WorkspaceGitSync(ws)
    sync.init(remote=remote, include_cache=True)
    sync.push()
    # vectors.lance/data.bin should be tracked
    tracked = subprocess.run(
        ["git", "-C", str(ws), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "vectors.lance/data.bin" in tracked
    assert "bm25_index.pkl" in tracked  # full mode keeps caches


def test_clone_roundtrip(env):
    ws, remote, root = env
    # push first so the remote has content
    sync = WorkspaceGitSync(ws)
    sync.init(remote=remote)
    sync.push()

    # clone into a fresh workspace id
    res = clone_workspace(remote, "cloned", root)
    assert res.ok, res.detail
    cloned_dir = root / "workspaces" / "cloned"
    assert (cloned_dir / "events.sqlite").exists()
    assert (cloned_dir / "meta.yaml").exists()
    # content matches what we seeded
    assert (cloned_dir / "events.sqlite").read_bytes().startswith(b"SQLite format 3")


def test_clone_refuses_nonempty_dest(env):
    ws, remote, root = env
    sync = WorkspaceGitSync(ws)
    sync.init(remote=remote)
    sync.push()
    # pre-create a non-empty dest
    dest = root / "workspaces" / "occupied"
    dest.mkdir(parents=True)
    (dest / "x.txt").write_text("hi", encoding="utf-8")
    res = clone_workspace(remote, "occupied", root)
    assert not res.ok
    assert "not empty" in res.detail


def test_pull_after_remote_change(env):
    ws, remote, root = env
    sync = WorkspaceGitSync(ws)
    sync.init(remote=remote)
    sync.push()

    # clone into a second checkout, change it, push
    second = root / "workspaces" / "second"
    clone_workspace(remote, "second", root)
    (second / "meta.yaml").write_text("id: test\nname: Changed\n", encoding="utf-8")
    sync2 = WorkspaceGitSync(second)
    # second clone has its own .git pointing at remote already
    sync2.push(message="change meta")

    # original pulls the change (remote wins)
    res = sync.pull(strategy="theirs")
    assert res.ok, res.detail
    assert "Changed" in (ws / "meta.yaml").read_text(encoding="utf-8")
