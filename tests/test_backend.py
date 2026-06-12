"""Backend actions — NoopBackend (default) + Sim2lBackend + selection.

The backend abstracts the loop's *publish* actions (register / persist
/ record). When sim2l isn't active, the no-op backend makes all three
silent no-ops, so ARC runs fully local with no shared persistence. The
sim2l backend routes them to the catalog/results services.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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


def _execution_with_metrics(**metrics):
    return SimpleNamespace(run_id="r1", status="completed",
                           outputs={"bandgap_ev": 1.1}, execution_id="e1",
                           squid_id="sq1", duration_seconds=0.5,
                           metrics=metrics)


def test_sim2l_backend_persist_and_record_report_inline_outcome():
    """persist/record are handled inside Sim2LRuntimeAdapter.run(); the
    backend reports the *actual* inline outcome (from execution.metrics)
    and doesn't double-push."""
    backend = Sim2lBackend(_FakeSim2lAdapter())
    execution = _execution_with_metrics(results_persisted=True,
                                        execution_recorded=True)
    p = asyncio.run(backend.persist_result(_artifact(), execution, {}))
    rec = asyncio.run(backend.record_execution(_artifact(), execution, {}, {}))
    assert p["handled_inline"] is True and p["persisted"] is True
    assert rec["handled_inline"] is True and rec["recorded"] is True


def test_sim2l_backend_reports_inline_push_failure_honestly():
    """An inline push that failed must not be reported as persisted=True."""
    adapter = _FakeSim2lAdapter()
    adapter.last_push_errors = [("results", "results: HTTPError: 401 Unauthorized")]
    backend = Sim2lBackend(adapter)
    execution = _execution_with_metrics(results_persisted=False,
                                        execution_recorded=False)
    p = asyncio.run(backend.persist_result(_artifact(), execution, {}))
    rec = asyncio.run(backend.record_execution(_artifact(), execution, {}, {}))
    assert p["persisted"] is False
    assert "401" in p["error"]
    assert rec["recorded"] is False


def test_sim2l_backend_is_active_when_importable():
    backend = Sim2lBackend(_FakeSim2lAdapter())
    # sim2l is installed in this repo, so is_active should be True.
    assert backend.is_active() == sim2l_importable()


def test_sim2l_backend_standalone_activity_follows_service_probe(monkeypatch):
    """Standalone mode (adapter=None) is gated on service reachability —
    patched here so the test doesn't depend on whether real services
    happen to be listening on localhost."""
    import arc.runtime.backend as backend_mod
    monkeypatch.setattr(backend_mod, "sim2l_services_active", lambda *a, **k: False)
    assert Sim2lBackend(None).is_active() is False
    monkeypatch.setattr(backend_mod, "sim2l_services_active", lambda *a, **k: True)
    assert Sim2lBackend(None).is_active() is True


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


def test_github_register_commits_binary_file_without_raising(monkeypatch, tmp_path):
    """A binary artifact file must not crash register_artifact (the
    contract says it never raises) — bytes are base64-encoded like any
    other file."""
    import requests
    from arc.runtime.backend import GitHubBackend
    from arc.schemas.artifact import ArtifactRecord
    _github_env(monkeypatch)
    fake = _FakeRequests()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "put", fake.put)

    art_dir = tmp_path / "art" / "0.1.0"
    art_dir.mkdir(parents=True)
    (art_dir / "workflow.py").write_text("def simulate():\n    return {}\n")
    # Non-UTF8 bytes — read_text() would have raised UnicodeDecodeError.
    (art_dir / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    rec = ArtifactRecord(artifact_id="a", name="bin model", version="0.1.0",
                         state="REGISTERED", path=str(art_dir), metadata={})

    result = asyncio.run(GitHubBackend().register_artifact(rec))
    assert result["registered"] is True
    assert set(result["files"]) == {"workflow.py", "data.bin"}


def test_github_register_commits_nested_files(monkeypatch, tmp_path):
    import requests
    from arc.runtime.backend import GitHubBackend
    _github_env(monkeypatch)
    fake = _FakeRequests()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "put", fake.put)

    art = _real_artifact(tmp_path)
    art_dir = Path(art.path)
    (art_dir / "tests").mkdir()
    (art_dir / "tests" / "test_workflow.py").write_text("def test_ok(): pass\n")

    result = asyncio.run(GitHubBackend().register_artifact(art))

    assert result["registered"] is True
    assert "tests/test_workflow.py" in result["files"]
    assert any("/tests/test_workflow.py" in url for url, _ in fake.puts)


def test_github_persist_inactive_never_raises(monkeypatch):
    from arc.runtime.backend import GitHubBackend
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ARC_GITHUB_REPO", raising=False)

    result = asyncio.run(GitHubBackend().persist_result(_artifact(), _execution(), {}))

    assert result["persisted"] is False
    assert result["skipped"] is True


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


def test_safe_backend_action_converts_exceptions_to_error_result():
    from arc.runtime.backend import safe_backend_action

    class _Boom:
        name = "boom"

        async def persist_result(self, *args):
            raise RuntimeError("network down")

    result = asyncio.run(safe_backend_action(_Boom(), "persist_result", _artifact(), _execution(), {}))

    assert result["persisted"] is False
    assert result["backend"] == "boom"
    assert "network down" in result["error"]


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


# ── Standalone sim2l publishing (run locally, publish to sim2l) ─────────


def test_resolve_explicit_sim2l_with_local_adapter_goes_standalone(monkeypatch):
    """ARC_BACKEND=sim2l + a local adapter (no register_artifact) must give
    the standalone sim2l backend when the services are reachable — not a
    silent no-op."""
    import arc.runtime.backend as backend_mod
    monkeypatch.setenv("ARC_BACKEND", "sim2l")
    monkeypatch.setattr(backend_mod, "sim2l_services_active", lambda *a, **k: True)

    class _LocalLike:
        pass

    backend = resolve_backend(_LocalLike())
    assert isinstance(backend, Sim2lBackend)
    assert backend._adapter is None  # standalone mode


def test_resolve_explicit_sim2l_unreachable_services_is_noop(monkeypatch):
    import arc.runtime.backend as backend_mod
    monkeypatch.setenv("ARC_BACKEND", "sim2l")
    monkeypatch.setattr(backend_mod, "sim2l_services_active", lambda *a, **k: False)

    class _LocalLike:
        pass

    assert isinstance(resolve_backend(_LocalLike()), NoopBackend)


def test_standalone_persist_posts_to_results_service(monkeypatch):
    import requests
    posted = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        return _FakeResponse(201)
    monkeypatch.setattr(requests, "post", fake_post)

    backend = Sim2lBackend(None)
    from arc.schemas.execution import ExecutionResult
    execution = ExecutionResult(run_id="r1", status="completed",
                                outputs={"z": 1.0},
                                metrics={"squid_id": "sq1", "duration_seconds": 0.5})
    result = asyncio.run(backend.persist_result(_artifact(), execution, {"x": 1.0}))
    assert result["persisted"] is True
    assert posted["url"].endswith("/register_direct")
    assert posted["json"]["execution_id"] == "r1"
    assert posted["json"]["squid_id"] == "sq1"


def test_standalone_record_requires_catalog_entry(monkeypatch):
    """Recording an execution for a simulation the catalog doesn't know
    returns a clear error instead of raising."""
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(404))

    backend = Sim2lBackend(None)
    from arc.schemas.execution import ExecutionResult
    execution = ExecutionResult(run_id="r1", status="completed", outputs={}, metrics={})
    result = asyncio.run(backend.record_execution(_artifact(), execution, {}, {}))
    assert result["recorded"] is False
    assert "catalog" in result["error"]


# ── Registry-resolved (package-provided) backends ───────────────────────


class _CustomBackend(BackendActions):
    name = "custom"

    def is_active(self):
        return True

    async def register_artifact(self, artifact):
        return {"registered": True, "backend": "custom"}

    async def persist_result(self, artifact, execution, inputs):
        return {"persisted": True, "backend": "custom"}

    async def record_execution(self, artifact, execution, inputs, outputs):
        return {"recorded": True, "backend": "custom"}


def test_resolve_backend_from_registry(monkeypatch):
    """A package-registered backend is selectable via ARC_BACKEND=<name>."""
    from arc.core.registry import ComponentRegistry
    registry = ComponentRegistry()
    registry.register_backend("custom", _CustomBackend, package_name="my-pkg")
    monkeypatch.setenv("ARC_BACKEND", "custom")
    backend = resolve_backend(None, registry=registry)
    assert isinstance(backend, _CustomBackend)


def test_resolve_backend_registry_honours_package_disable(monkeypatch):
    from arc.core.registry import ComponentRegistry
    registry = ComponentRegistry()
    registry.register_backend("custom", _CustomBackend, package_name="my-pkg")
    monkeypatch.setenv("ARC_BACKEND", "custom")
    backend = resolve_backend(None, registry=registry, disabled_packages={"my-pkg"})
    assert isinstance(backend, NoopBackend)


def test_resolve_backend_unknown_name_is_noop_not_error(monkeypatch):
    from arc.core.registry import ComponentRegistry
    monkeypatch.setenv("ARC_BACKEND", "does-not-exist")
    backend = resolve_backend(None, registry=ComponentRegistry())
    assert isinstance(backend, NoopBackend)


# ── publish_provenance ───────────────────────────────────────────────────


def test_publish_provenance_default_is_skip():
    """Backends that don't override publish_provenance skip cleanly —
    third-party BackendActions keep working unchanged."""
    r = asyncio.run(NoopBackend().publish_provenance("s1", [{"action": "a"}]))
    assert r["published"] is False
    assert r["skipped"] is True


def test_sim2l_publish_provenance_posts_batch(monkeypatch):
    import requests
    posted = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        return _FakeResponse(201)
    monkeypatch.setattr(requests, "post", fake_post)

    backend = Sim2lBackend(None)
    entries = [{"action": "start", "agent": "orchestrator"}]
    r = asyncio.run(backend.publish_provenance("sess-1", entries))
    assert r["published"] is True and r["count"] == 1
    assert posted["url"].endswith("/provenance")
    assert posted["json"]["session_id"] == "sess-1"
    assert posted["json"]["entries"] == entries


def test_sim2l_publish_provenance_404_is_skip_not_retry(monkeypatch):
    """An older results service without /provenance must not cause endless
    requeue-retry loops."""
    import requests
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: _FakeResponse(404, text="not found"),
    )
    r = asyncio.run(Sim2lBackend(None).publish_provenance("s", [{"a": 1}]))
    assert r["published"] is False
    assert r["skipped"] is True


def test_github_publish_provenance_commits_jsonl_batch(monkeypatch):
    import requests
    from arc.runtime.backend import GitHubBackend
    _github_env(monkeypatch)
    fake = _FakeRequests()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "put", fake.put)

    r = asyncio.run(GitHubBackend().publish_provenance(
        "sess-1", [{"action": "start"}, {"action": "build"}],
    ))
    assert r["published"] is True and r["count"] == 2
    assert any("/provenance/sess-1/" in url and url.endswith(".jsonl")
               for url, _ in fake.puts)


def test_workflow_publish_provenance_drains_and_requeues_on_failure():
    """The workflow helper drains the log; a real failure requeues, a skip
    drops (the local JSONL still has everything)."""
    from arc.memory.provenance import ProvenanceLog
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = ProvenanceLog(log_path=f"{tmp}/p.jsonl")
        log.record("s1", "start", "orchestrator")

        class _FailingBackend(NoopBackend):
            async def publish_provenance(self, session_id, entries):
                return {"published": False, "backend": "x", "error": "down"}

        workflow = SimpleNamespace(provenance=log, session_id="s1",
                                   backend=_FailingBackend())
        from arc.orchestrator.workflow import ResearchWorkflow
        result = asyncio.run(ResearchWorkflow.publish_provenance(workflow))
        assert result["published"] is False
        # Real failure → requeued for the next attempt.
        assert len(log.drain_unpublished()) == 1

        # A skip result must NOT requeue (backend doesn't store provenance).
        log.record("s1", "plan", "planner")
        workflow.backend = NoopBackend()
        asyncio.run(ResearchWorkflow.publish_provenance(workflow))
        assert log.drain_unpublished() == []


# ── Session-id lifecycle (constructed-before-login) ─────────────────────


def test_sim2l_backend_session_ids_resolve_lazily(monkeypatch):
    """The backend is constructed before the chat signs in; ids attached
    to the adapter afterwards must be visible to the backend (capturing
    them in __init__ froze them at None — the publish/provenance 401 bug)."""
    monkeypatch.delenv("SIM2L_CATALOG_SESSION_ID", raising=False)
    monkeypatch.delenv("SIM2L_RESULTS_SESSION_ID", raising=False)
    adapter = _FakeSim2lAdapter()
    backend = Sim2lBackend(adapter)
    assert backend._catalog_session_id is None

    # Chat login attaches ids to the adapter *after* backend construction.
    adapter._catalog_session_id = "cat-1"
    adapter._results_session_id = "res-1"
    assert backend._catalog_session_id == "cat-1"
    assert backend._results_session_id == "res-1"


def test_sim2l_backend_set_session_ids_for_standalone(monkeypatch):
    """Standalone mode has no adapter to inherit from — the login flow
    attaches ids via set_session_ids."""
    monkeypatch.delenv("SIM2L_CATALOG_SESSION_ID", raising=False)
    monkeypatch.delenv("SIM2L_RESULTS_SESSION_ID", raising=False)
    backend = Sim2lBackend(None)
    assert backend._results_session_id is None
    backend.set_session_ids(catalog_session_id="c-1", results_session_id="r-1")
    assert backend._catalog_session_id == "c-1"
    assert backend._results_session_id == "r-1"


def test_standalone_provenance_post_carries_session_header(monkeypatch):
    import requests
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["headers"] = headers or {}
        return _FakeResponse(201)
    monkeypatch.setattr(requests, "post", fake_post)

    backend = Sim2lBackend(None)
    backend.set_session_ids(results_session_id="res-9")
    r = asyncio.run(backend.publish_provenance("s", [{"action": "a"}]))
    assert r["published"] is True
    assert seen["headers"].get("X-Session-ID") == "res-9"


def test_inline_outcome_flags_assumed_persistence():
    """A cache hit's persisted=True is an inference, not a verified push —
    the backend surfaces that."""
    backend = Sim2lBackend(_FakeSim2lAdapter())
    execution = _execution_with_metrics(
        results_persisted=True, results_persistence_assumed=True,
        execution_recorded=True,
    )
    p = asyncio.run(backend.persist_result(_artifact(), execution, {}))
    assert p["persisted"] is True
    assert p["assumed"] is True


# ── execute_recorded (HTTP surfaces' bookkeeping, review pass 3) ─────────


def test_execute_recorded_full_bookkeeping(tmp_path, monkeypatch):
    """The HTTP-surface entry point saves the run, appends run_history,
    and writes a provenance entry — same trail as chat/YAML runs."""
    import uuid as _uuid
    monkeypatch.setenv("ARC_RUNTIME_ADAPTER", "local")
    from arc.orchestrator.workflow import ResearchWorkflow
    from arc.schemas.artifact import ArtifactRecord

    session_id = f"test-exec-rec-{_uuid.uuid4().hex[:8]}"
    wf = ResearchWorkflow(session_id=session_id)

    art_dir = tmp_path / "art"
    art_dir.mkdir()
    (art_dir / "workflow.py").write_text(
        "def simulate(**inputs):\n"
        "    return {'result': inputs.get('x', 1.0) * 2}\n"
    )
    artifact = ArtifactRecord(
        artifact_id="exec-rec-art", name="exec-rec", version="0.1.0",
        state="REGISTERED", path=str(art_dir), metadata={},
    )

    result = asyncio.run(
        wf.execute_recorded(artifact, {"x": 3.0}, action="api_run"),
    )
    assert result.status == "completed"
    # Saved to the session results store…
    assert wf.results.get(result.run_id).run_id == result.run_id
    # …in run_history…
    history = wf._context.memory["run_history"]
    assert history[-1]["run_id"] == result.run_id
    # …and in the provenance trail with the caller's action label.
    entries = wf.provenance.read_session(session_id)
    assert any(e["action"] == "api_run" and e["run_id"] == result.run_id
               for e in entries)
