"""Planner/optimizer provenance surfaced in /sweep and /optimize
(design/todo.md item 8).

``/sweep`` shows which planner designed its sweep; ``/optimize`` shows the
active optimizer plus the planner that designed the search space, and stamps
optimizer provenance onto the result.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from arc.chat.commands.sweep import run as sweep_run
from arc.chat.state import ChatState
from tests.fakes import make_artifact, make_workflow

pytestmark = pytest.mark.chat

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI colour codes so substring asserts see the literal text."""
    return _ANSI.sub("", text)


@dataclass
class _StubAdapter:
    calls: list = field(default_factory=list)

    async def run(self, artifact, inputs):
        self.calls.append((artifact, dict(inputs)))
        return SimpleNamespace(run_id="r1", status="completed",
                               outputs={"bandgap_ev": 1.1}, logs=["a", "b"])

    async def prepare_inputs(self, artifact, params):
        return dict(params)


class _StubResults:
    def __init__(self):
        self.saved = []

    def save(self, ex):
        self.saved.append(ex)

    def list_all(self):
        return list(self.saved)


def test_sweep_shows_planner_provenance(capsys):
    art = make_artifact()
    plan = SimpleNamespace(parameter_sweep={"thickness": [1, 2]})
    wf = make_workflow(memory={
        "current_plan": plan,
        "planner_provenance": {
            "planner": "doe_lhs",
            "experimental_design": ["Latin hypercube sweep"],
        },
    }, artifacts=[art])
    wf.adapter = _StubAdapter()
    wf.results = _StubResults()
    state = ChatState(workflow=wf)
    asyncio.run(sweep_run(state, [art.artifact_id]))
    out = _plain(capsys.readouterr().out)
    assert "Sweep source" in out
    assert "doe_lhs" in out
    assert "Latin hypercube sweep" in out


def test_sweep_provenance_defaults_to_unknown(capsys):
    art = make_artifact()
    plan = SimpleNamespace(parameter_sweep={"thickness": [1]})
    wf = make_workflow(memory={"current_plan": plan}, artifacts=[art])
    wf.adapter = _StubAdapter()
    wf.results = _StubResults()
    state = ChatState(workflow=wf)
    asyncio.run(sweep_run(state, [art.artifact_id]))
    out = _plain(capsys.readouterr().out)
    assert "planner=unknown" in out


def test_optimize_stamps_and_shows_provenance(capsys, monkeypatch):
    art = make_artifact()
    wf = make_workflow(memory={
        "target": {"bandgap_ev": 1.1},
        "planner_provenance": {"planner": "doe_lhs"},
    }, artifacts=[art])
    wf.adapter = _StubAdapter()

    # Stub the optimizer role so we don't run a real GA.
    class _StubOptimizer:
        def __init__(self, context=None):
            self.context = context

        async def run(self, artifact, target, max_generations=10, pop_size=8, on_generation=None):
            return {
                "best_inputs": {"thickness": 2.0},
                "best_outputs": {"bandgap_ev": 1.1},
                "best_fitness": 0.99,
                "generations_run": max_generations,
                "converged": True,
            }

    monkeypatch.setattr("arc.packages.resolve_role", lambda role, wf=None: _StubOptimizer)
    # current_artifact is read via state.current_artifact → workflow memory.
    wf._context.memory["current_artifact"] = art
    monkeypatch.setattr("arc.chat.session_io.save_session", lambda *a, **k: None)

    from arc.chat.commands.optimize import run as optimize_run
    state = ChatState(workflow=wf)
    asyncio.run(optimize_run(state, ["3", "4"]))
    out = _plain(capsys.readouterr().out)
    assert "Optimizer" in out
    assert "planner=doe_lhs" in out
    # Provenance recorded on session memory.
    prov = wf._context.memory.get("optimizer_provenance")
    assert prov and prov["generations"] == 3 and prov["population"] == 4
    assert prov["search_space_from_planner"] == "doe_lhs"
