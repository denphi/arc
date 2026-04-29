"""Built-in ARC package helpers.

The bundled package directories use distribution-style names such as
``arc-sim2l``. These helpers provide stable imports for tests and fallback code
without requiring those directory names to be Python identifiers.
"""

import importlib.util
import sys
from pathlib import Path


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
