"""Tests for API route validation and request plumbing."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import arc.api.routes as routes
from arc.api.routes import (
    ResearchRequest,
    ReviewRequest,
    create_artifact,
    get_status,
    list_artifacts,
    list_results,
    run_execution,
    run_review,
    start_research,
)
from arc.schemas.artifact import ArtifactDraft
from arc.schemas.execution import ExecutionRequest, ExecutionResult
from arc.schemas.research import ResearchGoal


@pytest.mark.asyncio
async def test_start_research_rejects_invalid_session_id():
    request = ResearchRequest(
        goal=ResearchGoal(goal="bad session"),
        session_id="../escape",
    )
    with pytest.raises(Exception) as exc:
        await start_research(request)
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_list_artifacts_rejects_invalid_session_id():
    with pytest.raises(Exception) as exc:
        await list_artifacts(session_id="../escape")
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_list_results_rejects_invalid_session_id():
    with pytest.raises(Exception) as exc:
        await list_results(session_id="../escape")
    assert getattr(exc.value, "status_code", None) == 400


def test_research_request_rejects_non_positive_iterations():
    with pytest.raises(ValidationError):
        ResearchRequest(goal=ResearchGoal(goal="bad iterations"), iterations=0)


@pytest.mark.asyncio
async def test_run_review_requires_target_context():
    request = ReviewRequest(
        result=ExecutionResult(
            run_id="run-1",
            status="completed",
            outputs={"result": 1.0},
        )
    )
    with pytest.raises(Exception) as exc:
        await run_review(request)
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_run_execution_uses_local_runtime_by_default(monkeypatch):
    monkeypatch.setenv("ARC_RUNTIME_ADAPTER", "local")
    session_id = "api-exec-local"
    artifact = await create_artifact(
        ArtifactDraft(
            name="local-exec",
            description="local execution artifact",
            files={
                "workflow.py": (
                    "def simulate(**inputs):\n"
                    "    return {'result': inputs.get('x', 1.0) * 3}\n"
                ),
                "sim2l.yaml": (
                    "inputs:\n"
                    "  x: {type: Number, default: 1.0}\n"
                    "outputs:\n"
                    "  result: {type: Number}\n"
                ),
            },
        ),
        session_id=session_id,
    )

    result = await run_execution(
        ExecutionRequest(artifact_id=artifact.artifact_id, inputs={"x": 2.0}),
        session_id=session_id,
    )

    assert result.status == "completed"
    assert result.outputs["result"] == 6.0


@pytest.mark.asyncio
async def test_create_artifact_registers_with_backend(monkeypatch):
    registered = []

    class _Backend:
        name = "spy"

        async def register_artifact(self, artifact):
            registered.append(artifact)
            return {"registered": True, "backend": "spy"}

    monkeypatch.setattr(
        routes,
        "_workflow",
        lambda *args, **kwargs: SimpleNamespace(backend=_Backend()),
    )

    artifact = await create_artifact(
        ArtifactDraft(
            name="published-api-artifact",
            description="published from API",
            files={
                "workflow.py": "def simulate(**inputs):\n    return {'result': 1}\n",
                "sim2l.yaml": "outputs:\n  result: {type: Number}\n",
            },
        ),
        session_id="api-create-publish",
    )

    assert registered == [artifact]


@pytest.mark.asyncio
async def test_create_artifact_survives_backend_setup_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("backend setup failed")

    monkeypatch.setattr(routes, "_workflow", _boom)

    artifact = await create_artifact(
        ArtifactDraft(
            name="local-only-api-artifact",
            description="still saved locally",
            files={
                "workflow.py": "def simulate(**inputs):\n    return {'result': 1}\n",
                "sim2l.yaml": "outputs:\n  result: {type: Number}\n",
            },
        ),
        session_id="api-create-backend-fails",
    )

    assert artifact.name == "local-only-api-artifact"


@pytest.mark.asyncio
async def test_get_status_reads_saved_result_not_adapter_default():
    session_id = "api-status"
    from arc.memory.results_store import ResultsStore
    from arc.session import session_paths

    store = ResultsStore(root=session_paths(session_id)["runs"])
    store.save(ExecutionResult(run_id="run-error", status="error"))

    assert await get_status("run-error", session_id=session_id) == {
        "run_id": "run-error",
        "status": "error",
    }
    with pytest.raises(Exception) as exc:
        await get_status("missing-run", session_id=session_id)
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_sessionless_result_lookup_rejects_unsafe_run_id():
    for fn in (get_status, routes.get_result):
        with pytest.raises(Exception) as exc:
            await fn("../escape")
        assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_sessionless_result_scan_is_read_only(monkeypatch):
    from arc.session import sim2l_home

    stray = sim2l_home() / "shared"
    stray.mkdir(parents=True)

    with pytest.raises(Exception) as exc:
        await get_status("missing-run")

    assert getattr(exc.value, "status_code", None) == 404
    assert not (stray / "runs").exists()


# ── Strategy state plumbing (fix for the half-shipped API gap) ────────


@pytest.mark.asyncio
async def test_workflow_helper_hydrates_strategy_overrides():
    """``_workflow`` reads session_state.json and writes the overrides
    onto ``workflow._context.memory`` so ``resolve_role`` picks them up.

    Without this, a client who calls POST /strategies/{role} sees their
    pick silently ignored on the next /research/start call.
    """
    from arc.api.routes import LLMConfig, _workflow
    from arc.api.session_state import save_state

    save_state("api-hydrate-test", {
        "strategy_overrides": {"planner": "mars_planner",
                               "optimizer": "bayesopt"},
        "active_recipe": "bayesian-materials",
    })
    workflow = _workflow(LLMConfig(), session_id="api-hydrate-test")
    assert workflow._context.memory["strategy_overrides"] == {
        "planner": "mars_planner", "optimizer": "bayesopt",
    }
    assert workflow._context.memory["active_recipe"] == "bayesian-materials"


@pytest.mark.asyncio
async def test_workflow_helper_falls_through_when_no_state_file():
    """Sessions without a state file (the common case) construct
    cleanly with no override keys on memory."""
    from arc.api.routes import LLMConfig, _workflow

    workflow = _workflow(LLMConfig(), session_id="api-no-state")
    assert "strategy_overrides" not in workflow._context.memory


@pytest.mark.asyncio
async def test_workflow_helper_ignores_empty_state_keys():
    """Empty values shouldn't pollute memory with falsy keys."""
    from arc.api.routes import LLMConfig, _workflow
    from arc.api.session_state import save_state

    # ``save_state`` itself drops falsy entries, but verify the read
    # path doesn't accidentally re-add them.
    save_state("api-empty-state", {"strategy_overrides": {}})
    workflow = _workflow(LLMConfig(), session_id="api-empty-state")
    assert "strategy_overrides" not in workflow._context.memory


@pytest.mark.asyncio
async def test_run_review_uses_reflective_when_overridden(monkeypatch):
    """``/review/run`` reads the saved override and instantiates the
    matching reviewer class instead of always loading the default."""
    from arc.api import routes as routes_mod
    from arc.api.session_state import save_state

    save_state("api-review-state", {
        "strategy_overrides": {"reviewer": "reflective"},
    })

    captured: list = []

    # Replace the resolver so we can confirm the route called it with
    # the right override, regardless of whether the reflective module
    # can be loaded in this environment.
    sentinel_called: list = []

    class _SentinelReviewer:
        def __init__(self, context):
            self.context = context
            sentinel_called.append(("init", context.memory))

        async def run(self, result):
            from arc.schemas.review import ReviewResult
            sentinel_called.append(("run", result))
            return ReviewResult(approved=False, summary="stub")

    def fake_resolve(role, *, overrides=None, config=None):
        captured.append({"role": role, "overrides": overrides})
        return _SentinelReviewer

    monkeypatch.setattr(routes_mod, "resolve_strategy_role", fake_resolve,
                        raising=False)
    # Above sets fail because resolve_strategy_role is imported inside
    # run_review. Patch the source instead so the local import picks it up.
    monkeypatch.setattr(
        "arc.core.strategies.resolve_role", fake_resolve,
    )

    review = await run_review(ReviewRequest(
        result=ExecutionResult(run_id="r", status="completed", outputs={"x": 1}),
        target={"x": 1},
        session_id="api-review-state",
    ))

    assert captured == [{
        "role": "reviewer",
        "overrides": {"reviewer": "reflective"},
    }]
    assert any(step[0] == "init" for step in sentinel_called)
    assert review.summary == "stub"


@pytest.mark.asyncio
async def test_run_review_uses_default_when_no_state(monkeypatch):
    """No persisted overrides → resolver is called with ``overrides=None``,
    which falls through to the default reviewer."""
    captured: list = []

    class _SentinelReviewer:
        def __init__(self, context):
            self.context = context

        async def run(self, result):
            from arc.schemas.review import ReviewResult
            return ReviewResult(approved=False, summary="default")

    def fake_resolve(role, *, overrides=None, config=None):
        captured.append({"role": role, "overrides": overrides})
        return _SentinelReviewer

    monkeypatch.setattr(
        "arc.core.strategies.resolve_role", fake_resolve,
    )

    await run_review(ReviewRequest(
        result=ExecutionResult(run_id="r", status="completed", outputs={"x": 1}),
        target={"x": 1},
        session_id="api-review-no-state",
    ))
    assert captured[0]["overrides"] is None
