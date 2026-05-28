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


# ── GitHubBackend (TODO item 15) ────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeRequests:
    """Records PUTs; GET returns 404 (file doesn't exist yet) by default."""

    def __init__(self):
        self.puts: list[tuple[str, dict]] = []
        self.get_status = 404

    def get(self, url, headers=None, params=None, timeout=None):
        return _FakeResponse(self.get_status, {"sha": "deadbeef"})

    def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append((url, json))
        return _FakeResponse(201, {"content": {"path": url}})


def _github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("ARC_GITHUB_REPO", "owner/repo")
    monkeypatch.delenv("ARC_GITHUB_BRANCH", raising=False)
    monkeypatch.delenv("ARC_GITHUB_PREFIX", raising=False)


def _real_artifact(tmp_path):
    from arc.schemas.artifact import ArtifactRecord
    art_dir = tmp_path / "art" / "0.1.0"
    art_dir.mkdir(parents=True)
    (art_dir / "workflow.py").write_text("def simulate(x=1.0):\n    return {'z': x}\n")
    (art_dir / "sim2l.yaml").write_text("inputs:\n  x: {type: Number}\n")
    (art_dir / "arc_record.json").write_text("{}")
    return ArtifactRecord(
        artifact_id="a1", name="my model v1", description="d",
        version="0.1.0", state="REGISTERED", path=str(art_dir), metadata={},
    )


def test_github_config_requires_token_and_repo(monkeypatch):
    from arc.runtime.backend import github_config
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ARC_GITHUB_REPO", raising=False)
    assert github_config() is None
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert github_config() is None  # repo still missing
    monkeypatch.setenv("ARC_GITHUB_REPO", "owner/repo")
    cfg = github_config()
    assert cfg["repo"] == "owner/repo" and cfg["prefix"] == "artifacts"


def test_github_backend_inactive_without_config(monkeypatch):
    from arc.runtime.backend import GitHubBackend
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ARC_GITHUB_REPO", raising=False)
    assert GitHubBackend().is_active() is False


def test_github_register_commits_every_file(monkeypatch, tmp_path):
    import requests
    from arc.runtime.backend import GitHubBackend
    _github_env(monkeypatch)
    fake = _FakeRequests()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "put", fake.put)

    backend = GitHubBackend()
    result = asyncio.run(backend.register_artifact(_real_artifact(tmp_path)))
    assert result["registered"] is True
    assert set(result["files"]) == {"workflow.py", "sim2l.yaml", "arc_record.json"}
    # name was sanitised into the repo path (no spaces, escaped).
    assert all("artifacts/my_model_v1/0.1.0/" in url for url, _ in fake.puts)


def test_github_register_reports_failure(monkeypatch, tmp_path):
    import requests
    from arc.runtime.backend import GitHubBackend
    _github_env(monkeypatch)

    def failing_put(url, headers=None, json=None, timeout=None):
        return _FakeResponse(422, text="unprocessable")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(404))
    monkeypatch.setattr(requests, "put", failing_put)

    backend = GitHubBackend()
    result = asyncio.run(backend.register_artifact(_real_artifact(tmp_path)))
    assert result["registered"] is False
    assert "error" in result


def test_github_register_missing_path_is_handled(monkeypatch):
    from arc.runtime.backend import GitHubBackend
    from arc.schemas.artifact import ArtifactRecord
    _github_env(monkeypatch)
    rec = ArtifactRecord(artifact_id="a", name="n", version="0.1.0",
                         state="REGISTERED", path="/nonexistent/path", metadata={})
    result = asyncio.run(GitHubBackend().register_artifact(rec))
    assert result["registered"] is False
    assert "missing" in result["error"]


def test_github_persist_result_writes_run_record(monkeypatch, tmp_path):
    import requests
    from arc.runtime.backend import GitHubBackend
    _github_env(monkeypatch)
    fake = _FakeRequests()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "put", fake.put)

    from arc.schemas.execution import ExecutionResult
    execution = ExecutionResult(run_id="r1", status="completed",
                                outputs={"z": 1.0}, metrics={})
    backend = GitHubBackend()
    result = asyncio.run(backend.persist_result(_real_artifact(tmp_path), execution, {"x": 1.0}))
    assert result["persisted"] is True
    assert any("/runs/r1.json" in url for url, _ in fake.puts)


def test_github_never_raises_on_network_error(monkeypatch, tmp_path):
    import requests
    from arc.runtime.backend import GitHubBackend
    _github_env(monkeypatch)

    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "put", boom)

    backend = GitHubBackend()
    # Must not raise — best-effort contract.
    result = asyncio.run(backend.register_artifact(_real_artifact(tmp_path)))
    assert result["registered"] is False


def test_resolve_backend_explicit_github(monkeypatch):
    from arc.runtime.backend import GitHubBackend
    _github_env(monkeypatch)
    monkeypatch.setenv("ARC_BACKEND", "github")
    assert isinstance(resolve_backend(), GitHubBackend)


def test_resolve_backend_explicit_github_without_token_is_noop(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ARC_GITHUB_REPO", raising=False)
    monkeypatch.setenv("ARC_BACKEND", "github")
    assert isinstance(resolve_backend(), NoopBackend)


def test_resolve_backend_explicit_noop_overrides_inference(monkeypatch):
    """ARC_BACKEND=noop forces the no-op even with a sim2l-capable adapter."""
    monkeypatch.setenv("ARC_BACKEND", "noop")
    assert isinstance(resolve_backend(_FakeSim2lAdapter()), NoopBackend)


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
