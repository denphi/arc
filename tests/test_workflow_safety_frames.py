"""The AST lint rejects frame/traceback introspection attributes."""

from __future__ import annotations

import pytest

from arc.runtime.workflow_safety import WorkflowSafetyError, check_workflow_source


@pytest.mark.parametrize("attr", ["gi_frame", "f_back", "f_globals", "f_builtins", "tb_frame"])
def test_frame_introspection_attributes_are_blocked(attr):
    source = f"def simulate():\n    g = (i for i in [1])\n    x = g.{attr}\n    return {{'y': 1}}\n"
    with pytest.raises(WorkflowSafetyError):
        check_workflow_source(source)


def test_plain_simulate_still_passes():
    check_workflow_source("import math\ndef simulate(x=1.0):\n    return {'y': math.sqrt(x)}\n")
