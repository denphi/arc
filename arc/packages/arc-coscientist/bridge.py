"""Lazy bridge to the unmodified cloned Co-Scientist repository."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


class CoScientistUnavailable(RuntimeError):
    """Raised when the upstream clone or one of its dependencies is missing."""


def _repo_candidates(config: dict[str, Any] | None = None) -> list[Path]:
    configured = None
    if config:
        configured = config.get("CO_SCIENTIST_REPO") or config.get("co_scientist_repo")
    configured = configured or os.environ.get("CO_SCIENTIST_REPO")

    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "Co-Scientist")
        candidates.append(parent.parent / "Co-Scientist")
    return candidates


def repo_root(config: dict[str, Any] | None = None) -> Path:
    """Return the local Co-Scientist clone path without modifying it."""

    for candidate in _repo_candidates(config):
        path = candidate if candidate.is_absolute() else Path.cwd() / candidate
        if (path / "co_scientist").is_dir() and (path / "pyproject.toml").is_file():
            return path.resolve()
    raise CoScientistUnavailable(
        "Co-Scientist clone not found. Set CO_SCIENTIST_REPO or clone it at ./Co-Scientist."
    )


def ensure_importable(config: dict[str, Any] | None = None) -> Path:
    """Put the upstream clone on sys.path and return its root.

    This is intentionally opt-in. ARC package loading should not import the
    heavy upstream runtime until a caller explicitly asks for full execution.
    """

    root = repo_root(config)
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    if sys.version_info < (3, 11) and "tomllib" not in sys.modules:
        try:
            import tomli as _tomllib
        except Exception:  # noqa: BLE001
            pass
        else:
            sys.modules["tomllib"] = _tomllib
    if sys.version_info < (3, 11):
        import datetime as _datetime
        if not hasattr(_datetime, "UTC"):
            _datetime.UTC = _datetime.timezone.utc
    return root


def data_dir(config: dict[str, Any] | None = None) -> Path:
    configured = None
    if config:
        configured = config.get("CO_SCIENTIST_DATA_DIR") or config.get("co_scientist_data_dir")
    configured = configured or os.environ.get("CO_SCIENTIST_DATA_DIR") or "workspace/co-scientist"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else Path.cwd() / path
