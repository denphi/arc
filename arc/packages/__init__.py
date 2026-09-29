"""Built-in ARC package helpers.

The bundled package directories use distribution-style names such as
``arc-sim2l``. These helpers provide stable imports for tests and fallback code
without requiring those directory names to be Python identifiers.

For *strategy*-aware lookups (i.e. honouring the ``[strategies]`` block of
``arc.toml``, environment overrides, and per-session ``/strategy`` choices)
use :func:`resolve_role`. The legacy ``load_<role>()`` helpers below always
return the *default* strategy's module — they exist so that callers
needing module-level helpers (``_keys_match`` on the reviewer module, for
example) keep working unchanged.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from pathlib import Path
from typing import Any

# ── Canonical import names for hyphenated package directories ──────────
#
# Bundled packages live in distribution-style directories (``arc-sim2l``)
# that are not valid Python identifiers. This finder makes each of them
# importable under exactly one name, ``arc.packages.<name_with_underscores>``
# (``arc.packages.arc_sim2l.agents.builder``), so the manifest loader, the
# strategy resolver and ordinary ``import`` statements all share a single
# module object per file — no symlinks, no per-caller aliases.


def _pkg_dir() -> Path:
    return Path(__file__).parent


class _HyphenatedPackageFinder(importlib.abc.MetaPathFinder):
    _PREFIX = __name__ + "."

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self._PREFIX):
            return None
        leaf = fullname[len(self._PREFIX):]
        if "." in leaf or "_" not in leaf:
            return None
        directory = _pkg_dir() / leaf.replace("_", "-")
        init = directory / "__init__.py"
        if not init.is_file():
            return None
        return importlib.util.spec_from_file_location(
            fullname, init, submodule_search_locations=[str(directory)],
        )


if not any(isinstance(f, _HyphenatedPackageFinder) for f in sys.meta_path):
    sys.meta_path.append(_HyphenatedPackageFinder())


def canonical_module_name(module_file: Path) -> str | None:
    """Importable dotted name for a file inside a bundled package, else None."""
    try:
        rel = Path(module_file).resolve().relative_to(_pkg_dir().resolve())
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if not parts or parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    parts[0] = parts[0].replace("-", "_")
    if not all(part.isidentifier() for part in parts):
        return None
    return ".".join([__name__, *parts])


# ── Strategy-aware lookup ───────────────────────────────────────────────


def resolve_role(role: str, workflow: Any = None) -> Any:
    """Return the configured class for ``role``.

    Honours, in order: a per-session ``/strategy`` override stored on
    ``workflow._context.memory["strategy_overrides"]``, the
    ``ARC_STRATEGY_<ROLE>`` environment variable, the ``[strategies]``
    block in ``arc.toml``, then the bundled default strategy.

    The legacy ``load_<role>()`` helpers below are unaffected and continue
    to return the default strategy's module — handy for callers that need
    module-level helpers (e.g. ``ReviewerModule._keys_match``).
    """
    from arc.core.strategies import resolve_role as _core_resolve
    overrides: dict[str, str] | None = None
    disabled_packages: set[str] = set()
    loaded_packages: set[str] | None = None
    if workflow is not None:
        try:
            memory = workflow._context.memory
            overrides = memory.get("strategy_overrides") or None
            # The session's ``/package disable`` set becomes a real runtime
            # filter: a strategy from a disabled package is not selectable
            # (design/todo.md item 4).
            disabled_packages = set(
                (memory.get("packages", {}) or {}).get("disabled", []) or []
            )
            registry = getattr(workflow, "registry", None)
            loaded_packages = set(registry.list_packages()) if registry is not None else None
        except AttributeError:
            overrides = None
    try:
        from arc.core.config import load_arc_toml
        _path, config = load_arc_toml()
    except Exception:
        config = {}
    return _core_resolve(
        role, overrides=overrides, config=config, disabled_packages=disabled_packages,
        loaded_packages=loaded_packages,
    )


def load_ideator():
    return importlib.import_module(f"{__name__}.arc_sim2l.agents.ideator")


def load_planner():
    return importlib.import_module(f"{__name__}.arc_sim2l.agents.planner")


def load_builder():
    return importlib.import_module(f"{__name__}.arc_sim2l.agents.builder")


def load_reviewer():
    return importlib.import_module(f"{__name__}.arc_sim2l.agents.reviewer")


def load_reflector():
    return importlib.import_module(f"{__name__}.arc_sim2l.agents.reflector")


def load_optimizer():
    return importlib.import_module(f"{__name__}.arc_sim2l.agents.optimizer")


def load_curator():
    return importlib.import_module(f"{__name__}.arc_sim2l.agents.curator")
