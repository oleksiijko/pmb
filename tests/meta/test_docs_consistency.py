"""X8 — docs that cannot lie. Mechanical consistency gates so the version, the
changelog and the config reference can't silently drift from the code:

  * __version__ == pyproject [project].version == the newest CHANGELOG heading.
  * every config key carries a non-empty description (the config reference is
    generated from these, so a blank one ships a lie).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())


def _read(p: str) -> str:
    return (_ROOT / p).read_text(encoding="utf-8")


def test_version_is_consistent_across_init_pyproject_changelog():
    import pmb
    code_v = pmb.__version__

    m = re.search(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"), re.M)
    assert m, "pyproject [project].version not found"
    assert m.group(1) == code_v, f"pyproject {m.group(1)} != __version__ {code_v}"

    # newest released CHANGELOG heading (skip an [Unreleased] section)
    headings = re.findall(r'^##\s*\[([^\]]+)\]', _read("CHANGELOG.md"), re.M)
    released = [h for h in headings if h.lower() != "unreleased"]
    assert released, "no released CHANGELOG heading found"
    assert released[0] == code_v, (
        f"newest CHANGELOG [{released[0]}] != __version__ {code_v}")


def test_every_config_key_has_a_description():
    from pmb.config import SCHEMA
    blank = [k for k, s in SCHEMA.items()
             if not (getattr(s, "help", "") or "").strip()]
    assert not blank, f"config keys with no help text (X8): {blank}"


def test_config_keys_are_well_formed():
    # dotted namespace.key, lowercase — the reference + `pmb config` rely on it
    from pmb.config import SCHEMA
    bad = [k for k in SCHEMA if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_.]+", k)]
    assert not bad, f"malformed config keys: {bad}"


def test_distribution_versions_match():
    import yaml

    import pmb

    assert json.loads(_read("npm/package.json"))["version"] == pmb.__version__
    server = json.loads(_read("server.json"))
    assert server["version"] == pmb.__version__
    assert all(package["version"] == pmb.__version__ for package in server["packages"])
    assert yaml.safe_load(_read("CITATION.cff"))["version"] == pmb.__version__
