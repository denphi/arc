"""Shared helpers for loading ARC's ``arc.toml`` config file.

Both ``Kernel`` and ``ResearchWorkflow._default_registry`` previously
duplicated nearly the same logic for locating ``arc.toml``, parsing it, and
resolving package paths relative to the config file. This module centralises
that work so the two call sites stay in sync.
"""

from __future__ import annotations

import copy
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
    2. If no path is given and ``./arc.toml`` exists, use it.
    3. Otherwise, fall back to the bundled defaults.
    4. If none exist, return the requested path (caller decides what to do).
    """
    if path is not None:
        candidate = Path(path)
        if candidate.exists():
            return candidate
    else:
        project = Path.cwd() / "arc.toml"
        if project.exists():
            return project

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

    A project-local config overlays the bundled repo-level config so a project
    can add an external package path without copying ARC's whole package
    catalogue. For ``[packages].paths`` the lists are appended and de-duped;
    other local keys override the bundled defaults recursively.

    The returned dict is a fresh ``deepcopy`` of the cache entry — review
    item #A12 — so callers that mutate it can't corrupt the shared cached
    object.
    """
    config_path = resolve_config_path(path)
    if not config_path.exists():
        return config_path, {}
    config = copy.deepcopy(_cached_load_toml(str(config_path), config_path.stat().st_mtime_ns))
    bundled = _arc_toml_search_paths()[0].resolve()
    if config_path.resolve() != bundled and bundled.exists():
        base = copy.deepcopy(_cached_load_toml(str(bundled), bundled.stat().st_mtime_ns))
        _absolutize_package_paths(base, bundled)
        config = _merge_project_config(base, config)
    return config_path, config


@lru_cache(maxsize=8)
def _cached_load_toml(path_str: str, mtime_ns: int) -> dict[str, Any]:
    """Memoize TOML loads by (path, mtime) so re-resolving is cheap.

    Called via a small wrapper so the ``Path`` object isn't part of the cache
    key; the mtime tag invalidates entries automatically when the file changes.
    Callers receive a deepcopy via ``load_arc_toml`` so they're free to mutate
    without leaking changes into other readers (#A12).
    """
    return _load_toml(Path(path_str))


def _merge_project_config(base: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Overlay a project config on bundled defaults."""
    merged = copy.deepcopy(base)
    for key, value in project.items():
        if key == "packages" and isinstance(value, dict):
            packages = dict(merged.get("packages", {}) or {})
            for subkey, subvalue in value.items():
                if subkey == "paths" and isinstance(subvalue, list):
                    packages["paths"] = _dedupe_list((packages.get("paths") or []) + subvalue)
                else:
                    packages[subkey] = copy.deepcopy(subvalue)
            merged["packages"] = packages
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_project_config(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _dedupe_list(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = str(value)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(value)
    return out


def _absolutize_package_paths(config: dict[str, Any], config_path: Path) -> None:
    packages = config.get("packages")
    if not isinstance(packages, dict):
        return
    paths = packages.get("paths")
    if not isinstance(paths, list):
        return
    base = config_path.parent
    packages["paths"] = [
        str((base / path).resolve()) if not Path(path).is_absolute() else str(path)
        for path in paths
    ]


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


@lru_cache(maxsize=512)
def package_name_for_path(package_dir: Path) -> str:
    """Read a package directory's declared ``name`` from its ``package.yaml``.

    Falls back to the directory name when the manifest is missing or
    unreadable. Shared by ``Kernel`` and ``ResearchWorkflow`` so both agree
    on what a path's package name is.
    """
    package_dir = package_dir.resolve()
    manifest = package_dir / "package.yaml"
    if not manifest.exists():
        return package_dir.name
    try:
        import yaml
        data = yaml.safe_load(manifest.read_text()) or {}
        return data.get("name") or package_dir.name
    except Exception:  # noqa: BLE001 — name resolution must never raise
        return package_dir.name


def filter_package_paths(
    package_paths: list[str], package_config: dict[str, Any]
) -> list[str]:
    """Apply ``[packages].enabled`` / ``[packages].disabled`` to resolved paths.

    ``enabled`` (when non-empty) is an allow-list — only those package names
    survive. ``disabled`` is always a deny-list. This is the single source of
    truth for package filtering so the ``Kernel`` startup path and the
    ``ResearchWorkflow`` orchestrator path (CLI/UI/tests) load an identical
    package set for the same ``arc.toml`` (design/todo.md item 3).
    """
    enabled = set(package_config.get("enabled", []) or [])
    disabled = set(package_config.get("disabled", []) or [])
    if not enabled and not disabled:
        return list(package_paths)
    filtered = []
    for path_str in package_paths:
        name = package_name_for_path(Path(path_str))
        if enabled and name not in enabled:
            continue
        if name in disabled:
            continue
        filtered.append(path_str)
    return filtered


def build_context_workflow_specs(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized ``[workflows.build]`` context workflow specs.

    Supported forms:

    ``context = ["name"]``
        The MVP shorthand.

    ``[[workflows.build.context]]``
        Future-friendly table entries with ``name``, ``required``, ``cache``,
        and optional ``inputs``.

    The function is intentionally permissive: malformed entries are skipped so
    a partially edited local ``arc.toml`` does not break unrelated runs.
    """
    workflows = config.get("workflows") or {}
    if not isinstance(workflows, dict):
        return []
    build = workflows.get("build") or {}
    if not isinstance(build, dict):
        return []
    raw = build.get("context") or []
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    specs: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
            if name:
                specs.append({
                    "name": name,
                    "required": True,
                    "cache": "per_iteration",
                    "inputs": {},
                })
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        inputs = item.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        specs.append({
            "name": name,
            "required": bool(item.get("required", True)),
            "cache": str(item.get("cache") or "per_iteration"),
            "inputs": dict(inputs),
        })
    return specs
