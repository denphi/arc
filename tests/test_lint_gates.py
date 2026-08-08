"""Run the correctness-critical lint rules as part of the ordinary test suite.

``pyproject.toml`` selects ``E, F, I, FA``, but most of that is style debt. Two
of those rules catch real defects rather than formatting:

* **F821** (undefined name) — this is what a live ``NameError`` looks like to a
  static checker. ``arc/ui/server.py`` returned an undefined ``prepared`` for
  months; every UI artifact execution ran the artifact, recorded the run, then
  died building the response.
* **FA** (future-annotations) — a PEP 604 annotation without the future import
  raises ``TypeError`` at def time on Python 3.9, the interpreter the
  legacy-FEniCS conda stack ships. ``tests/test_py39_compat.py`` checks the same
  invariant structurally; this covers it via the tool the config names.

Both rules were configured in ``pyproject.toml`` and neither was enforced:
nothing in the repository ran ruff. This project has no CI, by choice, so the
gates live here instead — where they run on every ``pytest`` invocation. Ruff
over this repo takes tens of milliseconds.

Style rules (E501, I001) are deliberately excluded. There is real pre-existing
debt in those, and failing the suite on line length would say nothing about
whether the code works.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT_TARGETS = ("arc", "tests", "examples")

# Rules that indicate a defect, not a preference.
CORRECTNESS_RULES = (
    "F821",  # undefined name — a NameError the checker can see
    "FA",    # PEP 604 without the future import — TypeError on py3.9
)


def _ruff_command() -> list[str]:
    """How to invoke ruff here, preferring the importable module.

    ``shutil.which("ruff")`` alone is not enough: ruff is frequently installed
    into an environment without its console script on ``PATH`` (the case in
    this checkout), and a gate that skips itself into a pass is precisely the
    failure this file exists to prevent.
    """
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    executable = shutil.which("ruff")
    if executable is not None:
        return [executable]
    pytest.skip("ruff is not installed; `pip install -e '.[dev]'` to enable this gate")


@pytest.mark.parametrize("rule", CORRECTNESS_RULES)
def test_correctness_lint_rule_is_clean(rule: str):
    completed = subprocess.run(
        [*_ruff_command(), "check", "--select", rule, "--no-cache", *LINT_TARGETS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"ruff --select {rule} reported problems:\n\n"
        f"{completed.stdout}{completed.stderr}"
    )
