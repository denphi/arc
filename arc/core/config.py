"""Shared helpers for loading ARC's ``arc.toml`` config file.

Both ``Kernel`` and ``ResearchWorkflow._default_registry`` previously
duplicated nearly the same logic for locating ``arc.toml``, parsing it, and
resolving package paths relative to the config file. This module centralises
that work so the two call sites stay in sync.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any


def _arc_toml_search_paths() -> tuple[Path, ...]:
    """Bundled fallbacks: the repo-root and package-root arc.toml files."""
    return (
        Path(__file__).resolve().parents[2] / "arc.toml",
        Path(__file__).resolve().parents[1] / "arc.toml",
    )


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Find the arc.toml to load.

    1. If ``path`` is given and exists, use it.
    2. Otherwise, fall back to the bundled defaults.
    3. If none exist, return the requested path (caller decides what to do).
    """
    if path is not None:
        candidate = Path(path)
        if candidate.exists():
            return candidate

    for bundled in _arc_toml_search_paths():
        if bundled.exists():
            return bundled

    return Path(path) if path is not None else _arc_toml_search_paths()[0]


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as f:
        return tomllib.load(f)


def load_arc_toml(path: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    """Locate and parse ``arc.toml``.

    Returns ``(config_path, config_dict)``. When no config can be found, the
    dict is empty and ``config_path`` is whatever ``resolve_config_path``
    returned (which may not actually exist on disk).
    """
    config_path = resolve_config_path(path)
    if not config_path.exists():
        return config_path, {}
    return config_path, _cached_load_toml(str(config_path), config_path.stat().st_mtime_ns)


@lru_cache(maxsize=8)
def _cached_load_toml(path_str: str, mtime_ns: int) -> dict[str, Any]:
    """Memoize TOML loads by (path, mtime) so re-resolving is cheap.

    Called via a small wrapper so the ``Path`` object isn't part of the cache
    key; the mtime tag invalidates entries automatically when the file changes.
    """
    return _load_toml(Path(path_str))


def resolve_package_paths(
    config: dict[str, Any], config_path: Path
) -> list[str]:
    """Turn the ``[packages].paths`` entries into absolute paths.

    Relative paths are anchored at the config file's directory (matching the
    historical behaviour of both ``Kernel`` and ``ResearchWorkflow``).
    """
    package_paths = config.get("packages", {}).get("paths", []) or []
    base = config_path.parent if config_path.exists() else Path.cwd()
    return [
        str((base / path).resolve()) if not Path(path).is_absolute() else path
        for path in package_paths
    ]
