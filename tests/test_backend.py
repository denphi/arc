"""Backend actions — NoopBackend (default) + Sim2lBackend + selection.

The backend abstracts the loop's *publish* actions (register / persist
/ record). When sim2l isn't active, the no-op backend makes all three
silent no-ops, so ARC runs fully local with no shared persistence. The
sim2l backend routes them to the catalog/results services.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.contracts.backend import BackendActions
from arc.runtime.backend import (
    NoopBackend,
    Sim2lBackend,
    resolve_backend,
    sim2l_importable,
)


pytestmark = pytest.mark.chat


def _artifact():
    return SimpleNamespace(artifact_id="a1", name="sim", version="0.1.0",
                           path="/tmp/x", metadata={})


def _execution():
    return SimpleNamespace(run_id="r1", status="completed",
                           outputs={"bandgap_ev": 1.1}, execution_id="e1",
                           squid_id="sq1", duration_seconds=0.5)


# ── NoopBackend ────────────────────────────────────────────────────────


def test_noop_is_not_active():
    assert NoopBackend().is_active() is False


def test_noop_register_is_silent_skip():
    r = asyncio.run(NoopBackend().register_artifact(_artifact()))
    assert r["registered"] is False
    assert r["skipped"] is True


def test_noop_persist_is_silent_skip():
    r = asyncio.run(NoopBackend().persist_result(_artifact(), _execution(), {}))
    assert r["persisted"] is False
    assert r["skipped"] is True


def test_noop_record_is_silent_skip():
    r = asyncio.run(NoopBackend().record_execution(_artifact(), _execution(), {}, {}))
    assert r["recorded"] is False
    assert r["skipped"] is True


def test_noop_satisfies_contract():
    assert isinstance(NoopBackend(), BackendActions)


# ── Sim2lBackend ───────────────────────────────────────────────────────


class _FakeSim2lAdapter:
    """Stand-in for Sim2LRuntimeAdapter exposing register_artifact."""

    def __init__(self, result=None):
        self._result = result or {"registered": True, "sim_name": "sim",
                                   "sim_version": "0.1.0", "catalog_persisted": True}
        self.calls = []

    async def register_artifact(self, artifact):
        self.calls.append(artifact)
        return self._result


def test_sim2l_backend_delegates_register():
    adapter = _FakeSim2lAdapter()
    backend = Sim2lBackend(adapter)
    r = asyncio.run(backend.register_artifact(_artifact()))
    assert r["registered"] is True
    assert len(adapter.calls) == 1


def test_sim2l_backend_register_swallows_exceptions():
    class _Boom:
        async def register_artifact(self, artifact):
            raise RuntimeError("catalog down")

    backend = Sim2lBackend(_Boom())
    r = asyncio.run(backend.register_artifact(_artifact()))
    assert r["registered"] is False
    assert "catalog down" in r["error"]


def test_sim2l_backend_register_handles_sync_return():
    """Adapter.register_artifact may be sync; backend awaits only if needed."""
    class _SyncAdapter:
        def register_artifact(self, artifact):
            return {"registered": True}

    backend = Sim2lBackend(_SyncAdapter())
    r = asyncio.run(backend.register_artifact(_artifact()))
    assert r["registered"] is True


def test_sim2l_backend_persist_and_record_are_inline_noops():
    """persist/record are handled inside Sim2LRuntimeAdapter.run(); the
    backend reports that and doesn't double-push."""
    backend = Sim2lBackend(_FakeSim2lAdapter())
    p = asyncio.run(backend.persist_result(_artifact(), _execution(), {}))
    rec = asyncio.run(backend.record_execution(_artifact(), _execution(), {}, {}))
    assert p["handled_inline"] is True
    assert rec["handled_inline"] is True


def test_sim2l_backend_is_active_when_importable():
    backend = Sim2lBackend(_FakeSim2lAdapter())
    # sim2l is installed in this repo, so is_active should be True.
    assert backend.is_active() == sim2l_importable()


def test_sim2l_backend_inactive_with_none_adapter():
    assert Sim2lBackend(None).is_active() is False


# ── resolve_backend ────────────────────────────────────────────────────


def test_resolve_returns_noop_for_none_adapter():
    assert isinstance(resolve_backend(None), NoopBackend)


def test_resolve_returns_noop_for_local_adapter():
    """A LocalRuntimeAdapter has no register_artifact → no-op backend."""
    class _LocalLike:
        pass  # no register_artifact
    assert isinstance(resolve_backend(_LocalLike()), NoopBackend)


def test_resolve_returns_sim2l_for_capable_adapter():
    """An adapter with register_artifact (+ sim2l importable) → sim2l backend."""
    adapter = _FakeSim2lAdapter()
    backend = resolve_backend(adapter)
    if sim2l_importable():
        assert isinstance(backend, Sim2lBackend)
    else:
        assert isinstance(backend, NoopBackend)


# ── Workflow wiring ────────────────────────────────────────────────────


def test_workflow_has_a_backend():
    """ResearchWorkflow constructs a backend at init."""
    from arc.orchestrator.workflow import ResearchWorkflow
    wf = ResearchWorkflow(session_id="test-backend-wiring")
    assert hasattr(wf, "backend")
    assert isinstance(wf.backend, BackendActions)


def test_workflow_default_backend_is_noop_with_local_adapter(monkeypatch):
    """With the default (local) adapter, the backend is the no-op —
    so a fresh ARC session publishes nothing."""
    import os
    monkeypatch.setenv("ARC_RUNTIME_ADAPTER", "local")
    from arc.orchestrator.workflow import ResearchWorkflow
    wf = ResearchWorkflow(session_id="test-backend-local")
    assert isinstance(wf.backend, NoopBackend)
    assert wf.backend.is_active() is False


# ── Loop integration: register skipped when backend inactive ───────────


def test_register_helper_returns_none_when_backend_inactive():
    """``_register_artifact_with_sim2l`` returns None (silent skip) when
    the workflow's backend is the no-op."""
    import asyncio as _asyncio
    from arc.chat.loop import _register_artifact_with_sim2l
    from arc.chat.plan_mode import set_plan_mode

    set_plan_mode(False)
    workflow = SimpleNamespace(backend=NoopBackend(), session_id="s",
                               _db_path=None)
    result = _asyncio.run(_register_artifact_with_sim2l(workflow, _artifact()))
    assert result is None


def test_register_helper_uses_active_backend():
    """When the backend is active, the helper returns its register result."""
    import asyncio as _asyncio
    from arc.chat.loop import _register_artifact_with_sim2l
    from arc.chat.plan_mode import set_plan_mode

    set_plan_mode(False)
    backend = Sim2lBackend(_FakeSim2lAdapter())
    if not backend.is_active():
        pytest.skip("sim2l not importable in this environment")
    workflow = SimpleNamespace(backend=backend, session_id="s", _db_path=None)
    result = _asyncio.run(_register_artifact_with_sim2l(workflow, _artifact()))
    assert result is not None
    assert result["registered"] is True


def test_register_helper_skips_in_plan_mode():
    import asyncio as _asyncio
    from arc.chat.loop import _register_artifact_with_sim2l
    from arc.chat.plan_mode import set_plan_mode

    set_plan_mode(True)
    try:
        backend = Sim2lBackend(_FakeSim2lAdapter())
        workflow = SimpleNamespace(backend=backend, session_id="s", _db_path=None)
        result = _asyncio.run(_register_artifact_with_sim2l(workflow, _artifact()))
        assert result["registered"] is False
        assert "plan mode" in result["error"]
    finally:
        set_plan_mode(False)
