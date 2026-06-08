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


import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_from_path(module_name: str, file_path: Path):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _pkg_dir() -> Path:
    return Path(__file__).parent


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
    path = _pkg_dir() / "arc-sim2l" / "agents" / "ideator.py"
    return _load_from_path("arc_sim2l.agents.ideator", path)


def load_planner():
    path = _pkg_dir() / "arc-sim2l" / "agents" / "planner.py"
    return _load_from_path("arc_sim2l.agents.planner", path)


def load_builder():
    path = _pkg_dir() / "arc-sim2l" / "agents" / "builder.py"
    return _load_from_path("arc_sim2l.agents.builder", path)


def load_reviewer():
    path = _pkg_dir() / "arc-sim2l" / "agents" / "reviewer.py"
    return _load_from_path("arc_sim2l.agents.reviewer", path)


def load_reflector():
    path = _pkg_dir() / "arc-sim2l" / "agents" / "reflector.py"
    return _load_from_path("arc_sim2l.agents.reflector", path)


def load_optimizer():
    path = _pkg_dir() / "arc-sim2l" / "agents" / "optimizer.py"
    return _load_from_path("arc_sim2l.agents.optimizer", path)


def load_curator():
    path = _pkg_dir() / "arc-sim2l" / "agents" / "curator.py"
    return _load_from_path("arc_sim2l.agents.curator", path)
