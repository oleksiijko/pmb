"""Personal Memory Brain - local memory for AI agents.

Importing this package is intentionally lightweight: we only set a few
environment defaults and silence noisy third-party loggers. The heavy
machinery (`Engine`, `Workspace`, `detect_workspace`) is loaded lazily
via PEP 562 `__getattr__` so that:

  - `import pmb`                       does NOT load LanceDB / sentence-transformers / numpy / rank_bm25 / fastmcp
  - `from pmb import Engine`           DOES (transitively, on access)
  - `from pmb.security.redact import redact`   stays lightweight

This keeps short-lived CLI invocations fast and keeps the import graph
in tests predictable.
"""

from __future__ import annotations

__version__ = "1.2.0"

# ---------------------------------------------------------------------------
# Quiet third-party noise BEFORE any heavy module gets a chance to import.
# These environment defaults are read by sentence-transformers, huggingface_hub
# and transformers when they initialise. We must set them at module load time
# so they are in place no matter when (or if) those modules are imported.
# ---------------------------------------------------------------------------
import os as _os
import warnings as _warnings

_os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
_os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# huggingface_hub emits a UserWarning about unauthenticated requests on each
# model download; we don't need a token for the open MiniLM model.
_warnings.filterwarnings(
    "ignore",
    message=".*unauthenticated requests.*",
    category=UserWarning,
)

# Silence the underlying logger too - some versions emit via logging, not warnings.
import logging as _logging

for _name in ("huggingface_hub", "huggingface_hub.utils._http", "transformers"):
    _logging.getLogger(_name).setLevel(_logging.ERROR)
del _logging, _name
del _os, _warnings


# ---------------------------------------------------------------------------
# Public surface (lazy). Anything listed here is reachable via
# `pmb.<name>` or `from pmb import <name>`, but the underlying module
# is only imported the first time the attribute is accessed.
# ---------------------------------------------------------------------------
__all__ = ["Engine", "Workspace", "detect_workspace", "__version__"]


_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    # attribute name -> (module, attr in module)
    "Engine":           ("pmb.core.engine",    "Engine"),
    "Workspace":        ("pmb.core.workspace", "Workspace"),
    "detect_workspace": ("pmb.core.workspace", "detect_workspace"),
}


def __getattr__(name: str):
    """PEP 562 lazy attribute access. Imports heavy modules on demand."""
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module 'pmb' has no attribute {name!r}")
    module_name, attr = target
    import importlib
    module = importlib.import_module(module_name)
    value = getattr(module, attr)
    # Cache on the package so subsequent accesses skip __getattr__.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Make tab-completion / dir() still show the lazy names."""
    return sorted(list(globals().keys()) + list(_LAZY_ATTRS.keys()))
