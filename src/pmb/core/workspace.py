"""
Workspace - isolated per-project memory storage.

Detection priority:
1. PMB_WORKSPACE env variable (explicit override)
2. .pmb/workspace.yaml in the project
3. git remote (hash) if there's a .git
4. cwd path (hash)
5. "default"

Storage: ~/.pmb/workspaces/{workspace_id}/
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

DEFAULT_PMB_HOME = Path.home() / ".pmb"


def _hash_short(s: str, length: int = 12) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:length]


def _git_remote(path: Path) -> str | None:
    """Get the git remote origin URL, if this is a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _git_root(path: Path) -> Path | None:
    """Find the git repo root."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


@dataclass
class Workspace:
    """Isolated workspace for a single project."""

    id: str
    name: str
    root: Path
    pmb_home: Path = field(default_factory=lambda: DEFAULT_PMB_HOME)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    source: str = "auto"  # "env" | "config" | "git" | "cwd" | "explicit"

    @property
    def storage_dir(self) -> Path:
        return self.pmb_home / "workspaces" / self.id

    @property
    def db_path(self) -> Path:
        return self.storage_dir / "events.sqlite"

    @property
    def vector_path(self) -> Path:
        return self.storage_dir / "vectors.lance"

    @property
    def meta_path(self) -> Path:
        return self.storage_dir / "meta.yaml"

    def ensure_dirs(self):
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def save_meta(self):
        self.ensure_dirs()
        payload = yaml.safe_dump({
            "id": self.id,
            "name": self.name,
            "root": str(self.root),
            "created_at": self.created_at,
            "source": self.source,
        }, allow_unicode=True)
        # S8: Engine.__init__ calls save_meta on every start, but the metadata
        # only changes on create/rename. Skip the disk write (and OneDrive/AV
        # churn it triggers) when the serialized content is byte-identical.
        try:
            if self.meta_path.exists() and \
                    self.meta_path.read_text(encoding="utf-8") == payload:
                return
        except Exception:
            pass
        with open(self.meta_path, "w", encoding="utf-8") as f:
            f.write(payload)

    @classmethod
    def load_meta(cls, storage_dir: Path, pmb_home: Path) -> Workspace:
        meta_file = storage_dir / "meta.yaml"
        with open(meta_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(
            id=data["id"],
            name=data["name"],
            root=Path(data["root"]),
            pmb_home=pmb_home,
            created_at=data["created_at"],
            source=data.get("source", "config"),
        )


def _default_workspace_file(pmb_home: Path) -> Path:
    """File holding the persisted default workspace id (set via
    `pmb workspace use`). One line, just the workspace id."""
    return pmb_home / "current_workspace"


def read_default_workspace(pmb_home: Path) -> str | None:
    """Return the persisted default workspace id, or None if unset/unreadable."""
    try:
        f = _default_workspace_file(pmb_home)
        if f.exists():
            val = f.read_text(encoding="utf-8").strip()
            return val or None
    except Exception:
        pass
    return None


def set_default_workspace(pmb_home: Path, ws_id: str | None) -> None:
    """Persist (ws_id) or clear (None) the default workspace selection."""
    f = _default_workspace_file(pmb_home)
    if ws_id is None:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(ws_id.strip() + "\n", encoding="utf-8")


def detect_workspace(
    cwd: Path | None = None,
    pmb_home: Path | None = None,
    explicit_id: str | None = None,
) -> Workspace:
    """
    Detect or create the workspace for the current directory.

    Detection priority:
    1. explicit_id (if passed)
    2. env var PMB_WORKSPACE
    3. .pmb/workspace.yaml in cwd or its parents
    4. git remote URL
    5. git root path
    6. cwd path
    """
    cwd = cwd or Path.cwd()
    pmb_home = pmb_home or Path(os.environ.get("PMB_HOME", DEFAULT_PMB_HOME))

    # 1. Explicit
    if explicit_id:
        return _build_workspace(explicit_id, str(cwd), cwd, pmb_home, "explicit")

    # 2. Env var
    env_ws = os.environ.get("PMB_WORKSPACE")
    if env_ws:
        return _build_workspace(env_ws, str(cwd), cwd, pmb_home, "env")

    # 3. Local config (.pmb/workspace.yaml)
    p = cwd
    for _ in range(10):
        config = p / ".pmb" / "workspace.yaml"
        if config.exists():
            with open(config, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return _build_workspace(
                data["id"],
                data.get("name", data["id"]),
                p,
                pmb_home,
                "config",
            )
        if p.parent == p:
            break
        p = p.parent

    # 4. Persisted default (set via `pmb workspace use <name>`). Slots in
    # AFTER a project's explicit .pmb/workspace.yaml but BEFORE the
    # git/cwd auto-detection fallback - so a user who picked a default
    # ("personal") gets it everywhere except inside a configured project,
    # while setups that never run `use` resolve exactly as before.
    default_id = read_default_workspace(pmb_home)
    if default_id:
        storage = pmb_home / "workspaces" / default_id
        if (storage / "meta.yaml").exists():
            ws = Workspace.load_meta(storage, pmb_home)
            ws.source = "default"
            return ws

    # 5. Git remote
    remote = _git_remote(cwd)
    if remote:
        ws_id = _hash_short(f"git:{remote}")
        ws_name = remote.rsplit("/", 1)[-1].replace(".git", "")
        root = _git_root(cwd) or cwd
        return _build_workspace(ws_id, ws_name, root, pmb_home, "git")

    # 6. Git root path
    root = _git_root(cwd)
    if root:
        ws_id = _hash_short(f"path:{root}")
        return _build_workspace(ws_id, root.name, root, pmb_home, "git")

    # 7. cwd path
    ws_id = _hash_short(f"path:{cwd.resolve()}")
    return _build_workspace(ws_id, cwd.name, cwd, pmb_home, "cwd")


def workspace_hint() -> str | None:
    """Workspace id a hook client sends to the shared daemon so it can refuse
    to serve a different workspace's memory (S4). Same resolution as the
    daemon's own build (PMB_WORKSPACE first, then config/default/git/cwd).
    None when it can't be resolved - callers must then skip the daemon, since
    an unnamed request is served whatever workspace the daemon holds."""
    try:
        return detect_workspace().id or None
    except Exception:
        return None


def _build_workspace(ws_id: str, name: str, root: Path, pmb_home: Path, source: str) -> Workspace:
    storage = pmb_home / "workspaces" / ws_id
    if (storage / "meta.yaml").exists():
        # Existing workspace - load preserved metadata
        ws = Workspace.load_meta(storage, pmb_home)
        return ws
    ws = Workspace(id=ws_id, name=name, root=root, pmb_home=pmb_home, source=source)
    return ws


def list_workspaces(pmb_home: Path | None = None) -> list[Workspace]:
    """List all existing workspaces."""
    pmb_home = pmb_home or DEFAULT_PMB_HOME
    ws_dir = pmb_home / "workspaces"
    if not ws_dir.exists():
        return []
    out = []
    for d in ws_dir.iterdir():
        meta = d / "meta.yaml"
        if meta.exists():
            try:
                out.append(Workspace.load_meta(d, pmb_home))
            except Exception:
                continue
    return out
