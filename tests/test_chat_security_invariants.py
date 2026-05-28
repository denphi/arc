"""Static security invariants for the chat package.

These tests assert patterns that would be a real bug if they crept in.
They're a poor-man's bandit while we don't have CI bandit yet.

If you legitimately need to introduce one of these, add an inline
``# noqa: security-invariant <reason>`` and update the test to ignore
that line.
"""

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.chat


CHAT_ROOT = Path(__file__).resolve().parent.parent / "arc" / "chat"


def _all_py_files() -> list[Path]:
    return [p for p in CHAT_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _grep(pattern: str) -> list[tuple[Path, int, str]]:
    rx = re.compile(pattern)
    hits = []
    for f in _all_py_files():
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "noqa: security-invariant" in line:
                continue
            if rx.search(line):
                # Strip docstring-only mentions (lines that are inside a string)
                if line.lstrip().startswith(('"', "'", "#")):
                    continue
                hits.append((f, i, line.strip()))
    return hits


def test_no_eval_or_exec_on_user_input():
    hits = _grep(r"\beval\s*\(|\bexec\s*\(")
    assert not hits, f"eval/exec call sites: {hits}"


def test_no_unsafe_yaml_load():
    hits = _grep(r"yaml\.load\(|yaml\.full_load\(|yaml\.unsafe_load\(")
    # yaml.safe_load is allowed
    assert not hits, f"unsafe yaml.* call sites: {hits}"


def test_no_shell_true_subprocess():
    hits = _grep(r"shell\s*=\s*True")
    assert not hits, f"shell=True call sites: {hits}"


def test_subprocess_not_imported_in_chat_package():
    """Subprocess belongs in ``arc.services`` (daemon manager), not in
    the chat package itself. The chat layer talks to services via the
    public ``start/stop/status_all`` API.
    """
    hits = _grep(r"^\s*import subprocess|^\s*from subprocess")
    assert not hits, (
        f"subprocess should only live in arc.services; found: {hits}"
    )


def test_slash_command_argv_uses_shlex():
    """The registry MUST tokenize with shlex.split so quoted args don't
    get silently corrupted. This is a regression flag for anyone who
    "simplifies" registry.lookup back to ``raw.split()``."""
    text = (CHAT_ROOT / "registry.py").read_text()
    assert "shlex.split" in text, (
        "arc/chat/registry.py must tokenize via shlex.split (current code "
        "uses raw.split() which corrupts quoted arguments)."
    )
