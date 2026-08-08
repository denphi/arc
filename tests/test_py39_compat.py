"""Guard the Python 3.9 floor that the legacy-FEniCS conda stack depends on.

ARC supports 3.9 for exactly one reason: it has to run inside the
FEniCS/DOLFIN 2019.1.x conda environment, which ships py39-only builds (see
``arc-fenics``). On 3.9 a ``X | None`` annotation is *evaluated at def time*
and raises ``TypeError`` unless the module carries
``from __future__ import annotations``.

``pyproject.toml`` configures ruff's FA102 rule for exactly this, but nothing
ran it — this project has no CI. ``arc/runtime/local.py`` picked up a bare
``ExecutionResult | None`` in that gap, and because ``arc/runtime/__init__.py``
imports it eagerly, *every* ``import arc.runtime.*`` raised on 3.9 — taking the
executor, the orchestrator and the chat loop with it.

So the invariant is checked here, structurally, over every module in the tree:
a rule that only exists in configuration is not a rule. ``test_lint_gates.py``
covers the same ground through ruff itself; this file needs no external tool
and names the offending annotation directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("arc", "examples", "tests")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _has_future_annotations(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _annotation_nodes(tree: ast.Module):
    """Every node that Python evaluates as an annotation at definition time."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.annotation is not None:
            yield node.annotation
        elif isinstance(node, ast.arg) and node.annotation is not None:
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            yield node.returns


def _uses_pep604(annotation: ast.AST) -> bool:
    return any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        for node in ast.walk(annotation)
    )


@pytest.mark.parametrize(
    "path", _python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_pep604_annotations_carry_the_future_import(path: Path):
    """A module using ``X | Y`` in an annotation must import future annotations."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
        pytest.fail(f"{path.relative_to(REPO_ROOT)} does not parse: {exc}")

    if _has_future_annotations(tree):
        return

    offenders = [
        f"line {annotation.lineno}: {ast.unparse(annotation)}"
        for annotation in _annotation_nodes(tree)
        if _uses_pep604(annotation)
    ]
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} uses PEP 604 unions without "
        f"`from __future__ import annotations`, so importing it raises "
        f"TypeError on Python 3.9:\n  " + "\n  ".join(offenders)
    )


def test_arc_runtime_package_imports_are_reachable():
    """``import arc.runtime`` pulls in local.py eagerly — keep that path clean.

    This is the specific chain that broke: ``arc/runtime/__init__.py`` imports
    ``LocalRuntimeAdapter``, so a def-time annotation error anywhere in
    ``local.py`` takes down the executor, the orchestrator and the chat loop.
    """
    import arc.chat.loop  # noqa: F401
    import arc.orchestrator.workflow  # noqa: F401
    import arc.runtime  # noqa: F401
    import arc.runtime.executor  # noqa: F401

    assert arc.runtime.LocalRuntimeAdapter is not None
