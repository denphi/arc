"""One module identity per source file, whichever loader gets there first.

ARC imports package Python from two directions:

* :func:`arc.core.loader._import_class` / ``_import_from_file`` — driven by a
  ``package.yaml`` ``entrypoint`` or ``path`` + ``class`` declaration.
* :func:`arc.core.strategies._load_spec` — driven by a ``StrategySpec``'s
  ``file_path``, for the role/strategy catalogue.

Both loaded from an explicit file path under their own naming scheme, and
neither consulted the other's cache. The same ``ideator.py`` therefore ended up
in ``sys.modules`` twice — once as ``arc.packages.arc_sim2l.agents.ideator``,
once as ``arc_strategies.arc_sim2l.agents.ideator`` — producing two distinct
``IdeatorAgent`` classes from one file::

    manifest-loaded is strategy-loaded   → False
    isinstance(strategy_instance, ManifestClass) → False

Anything comparing class identity silently disagreed with itself, and any
module-level state (caches, precomputed sets, counters) existed in duplicate.

This module keeps a resolved-path → module-name index so both callers converge
on a single module object. The first loader to reach a file decides its name;
every later request for the same file gets that same module back.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

# Absolute source path → the ``sys.modules`` key it was loaded under.
_MODULE_BY_PATH: dict[str, str] = {}

_NON_IDENTIFIER = re.compile(r"[^0-9A-Za-z_]")


def canonical_module_name(path: Path) -> str:
    """A stable, unique, readable ``sys.modules`` key for a source file.

    The trailing digest of the absolute path guarantees two same-named files in
    different packages can't collide; the leading path tokens keep tracebacks
    legible (``arc_pkg_arc_sim2l_agents_ideator_3f2a1c9d8e77``).
    """
    resolved = path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    token = _NON_IDENTIFIER.sub("_", "_".join(resolved.with_suffix("").parts[-3:]))
    return f"arc_pkg_{token.strip('_')}_{digest}"


def register_module_path(module: ModuleType) -> None:
    """Index an already-imported module by its source file.

    Called after a normal ``importlib.import_module`` so a genuinely importable
    module (``arc.packages.arc_coscientist.agents.ideator``) wins the file and a
    later file-path load reuses it rather than making a second copy.
    """
    file = getattr(module, "__file__", None)
    name = getattr(module, "__name__", None)
    if not file or not name:
        return
    try:
        resolved = str(Path(file).resolve())
    except OSError:  # pragma: no cover — unresolvable path, nothing to index
        return
    _MODULE_BY_PATH.setdefault(resolved, name)


def module_for_path(path: Path | str) -> ModuleType | None:
    """Return the module already loaded from ``path``, if there is one."""
    try:
        resolved = str(Path(path).resolve())
    except OSError:  # pragma: no cover
        return None
    name = _MODULE_BY_PATH.get(resolved)
    if name is None:
        return None
    module = sys.modules.get(name)
    if module is None:
        # Someone cleared sys.modules (test teardown); drop the stale index
        # entry so the next call re-executes the file.
        _MODULE_BY_PATH.pop(resolved, None)
    return module


def load_module_from_path(
    path: Path | str,
    preferred_name: str | None = None,
) -> ModuleType:
    """Import ``path`` once, returning the same module object every time.

    ``preferred_name`` is the ``sys.modules`` key to use when this call is the
    one that actually loads the file; it's ignored when the file is already
    loaded under another name. Callers keep their historical names this way
    while still sharing one module per file.
    """
    from importlib.util import module_from_spec, spec_from_file_location

    source = Path(path)
    cached = module_for_path(source)
    if cached is not None:
        return cached

    resolved = source.resolve()
    if not resolved.exists():
        raise ImportError(f"Cannot load module; file does not exist: {source}")

    name = preferred_name or canonical_module_name(resolved)
    existing = sys.modules.get(name)
    if existing is not None:
        register_module_path(existing)
        return existing

    spec = spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {resolved}")
    module = module_from_spec(spec)
    sys.modules[name] = module
    _MODULE_BY_PATH[str(resolved)] = name
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Don't leave a half-initialised module cached — a later load of the
        # same file would return the broken object instead of retrying.
        sys.modules.pop(name, None)
        _MODULE_BY_PATH.pop(str(resolved), None)
        raise
    return module


def reset_module_index() -> None:
    """Forget the path → module mapping (test teardown).

    Does not touch ``sys.modules``; it only stops this module from handing back
    entries a test has since replaced.
    """
    _MODULE_BY_PATH.clear()
