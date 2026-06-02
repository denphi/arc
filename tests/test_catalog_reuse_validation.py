"""Catalog-reuse validation semantics (recommendations.md finding D).

A reused catalog artifact is intentionally NOT re-validated (it was
validated when published, and has no local files to re-check), but it MUST
still execute. This pins the contract the comment + code now agree on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from arc.chat.research.phases import ExecutionPhase, ValidationPhase
from arc.chat.research.pipeline import PipelineState
from tests.fakes import make_artifact, make_workflow

pytestmark = pytest.mark.chat


@dataclass
class _StubAdapter:
    validated: list = field(default_factory=list)
    ran: list = field(default_factory=list)

    async def validate_artifact(self, artifact):
        self.validated.append(artifact)
        return SimpleNamespace(valid=True, errors=[], warnings=[])

    async def prepare_inputs(self, artifact, params):
        return dict(params)

    async def run(self, artifact, inputs):
        self.ran.append((artifact, dict(inputs)))
        return SimpleNamespace(run_id="r1", status="completed",
                               outputs={"result": 2.0}, logs=["a"], metrics={})


class _StubResults:
    def save(self, ex):  # noqa: D401
        pass


def _state(is_new: bool):
    wf = make_workflow()
    wf.adapter = _StubAdapter()
    wf.results = _StubResults()
    wf.backend = None
    st = PipelineState(workflow=wf, goal_text="g")
    st.artifact = make_artifact()
    st.is_new_artifact = is_new
    return st, wf.adapter


def test_validation_skipped_for_reused_catalog_artifact():
    # is_new_artifact=False is exactly what the catalog-reuse path sets.
    st, adapter = _state(is_new=False)
    assert ValidationPhase().should_run(st) is False
    # Execution still runs for the reused artifact.
    assert ExecutionPhase().should_run(st) is True
    asyncio.run(ExecutionPhase().run(st))
    assert adapter.ran, "reused catalog artifact must still execute"
    assert not adapter.validated, "reused catalog artifact must not be re-validated"


def test_validation_runs_for_newly_built_artifact():
    st, adapter = _state(is_new=True)
    assert ValidationPhase().should_run(st) is True
    asyncio.run(ValidationPhase().run(st))
    assert adapter.validated, "a freshly built artifact must be validated"
