"""Reusable fakes for chat tests.

Kept deliberately minimal — each test imports what it needs and ignores
the rest. New fakes go here when they're used in 2+ tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeProvider:
    """Stand-in for an LLM provider with a scripted reply queue."""

    replies: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        self.calls.append({"prompt": prompt, "system": system, "kwargs": kwargs})
        if not self.replies:
            return ""
        return self.replies.pop(0)


@dataclass
class FakeArtifactRegistry:
    records: list[Any] = field(default_factory=list)

    def list_all(self):
        return list(self.records)

    def get(self, artifact_id, version="0.1.0"):
        for r in self.records:
            if r.artifact_id == artifact_id:
                return r
        raise FileNotFoundError(artifact_id)


@dataclass
class FakeResultsStore:
    runs: list[Any] = field(default_factory=list)

    def save(self, result):
        self.runs.append(result)
        return f"/fake/runs/{getattr(result, 'run_id', 'run')}.json"

    def list_all(self):
        return list(self.runs)


@dataclass
class FakeProvenance:
    entries: list[dict] = field(default_factory=list)

    def record(self, session_id, action, agent, **kwargs):
        self.entries.append({"session_id": session_id, "action": action,
                             "agent": agent, **kwargs})


@dataclass
class FakePackageRegistry:
    packages: list[str] = field(default_factory=list)

    def list_packages(self):
        return list(self.packages)

    def list_agent_sources(self, role):
        return []


def make_context(memory: dict | None = None, iteration: int = 0, session_id: str = "test-session"):
    return SimpleNamespace(
        memory=dict(memory or {}),
        iteration=iteration,
        session_id=session_id,
    )


def make_workflow(
    memory: dict | None = None,
    artifacts: list | None = None,
    results: list | None = None,
    packages: list | None = None,
    provider: Any = None,
    session_id: str = "test-session",
):
    ctx = make_context(memory=memory, session_id=session_id)
    wf = SimpleNamespace(
        _context=ctx,
        artifacts=FakeArtifactRegistry(records=artifacts or []),
        results=FakeResultsStore(runs=results or []),
        registry=FakePackageRegistry(packages=packages or []),
        provider=provider,
        session_id=session_id,
        adapter=None,
        provenance=FakeProvenance(),
        # No package audit actions in unit tests — has_actions() False makes
        # every dispatch a no-op, mirroring a bare ResearchWorkflow.
        audit=SimpleNamespace(has_actions=lambda: False),
    )

    # Mirror ResearchWorkflow.record_run / publish_provenance closely enough
    # for command/loop unit tests: save + run_history, no backend.
    async def _record_run(artifact, result, inputs=None):
        path = wf.results.save(result)
        ctx.memory.setdefault("run_history", []).append({
            "run_id": getattr(result, "run_id", None),
            "inputs": dict(inputs if inputs is not None
                           else getattr(result, "inputs", {}) or {}),
            "outputs": getattr(result, "outputs", {}),
            "metrics": getattr(result, "metrics", {}),
        })
        return {"result_path": path, "backend_persist": {"persisted": False},
                "backend_record": {"recorded": False}}

    async def _publish_provenance():
        return {"published": False, "skipped": True}

    # Mirrors ResearchWorkflow._disabled_packages. Production code reads the
    # session's /package disable set through this method (both
    # _resolve_agent_class and _coder_agent_class), so the double needs it or
    # those paths silently take their exception fallback.
    def _disabled_packages() -> set:
        return set((ctx.memory.get("packages", {}) or {}).get("disabled", []) or [])

    wf.record_run = _record_run
    wf.publish_provenance = _publish_provenance
    wf._disabled_packages = _disabled_packages
    return wf


def make_artifact(artifact_id="abc12345", name="silicon_bandgap", state="REGISTERED",
                  inputs=None, outputs=None):
    return SimpleNamespace(
        artifact_id=artifact_id,
        name=name,
        state=state,
        description=f"Artifact {name}",
        metadata={
            "sim2l_inputs":  inputs  or {"thickness": {}},
            "sim2l_outputs": outputs or {"bandgap_ev": {}},
        },
        path=f"/tmp/{artifact_id}",
        version="0.1.0",
    )


def make_run(run_id="run00001", status="completed", outputs=None):
    return SimpleNamespace(
        run_id=run_id,
        status=status,
        outputs=outputs or {"bandgap_ev": 1.12},
    )
