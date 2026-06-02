from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from arc.api.security import require_api_token, validate_provider_base_url
from arc.memory.artifact_registry import ArtifactRegistry
from arc.memory.results_store import ResultsStore, validate_run_id
from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.artifact import ArtifactDraft
from arc.schemas.execution import ExecutionRequest, ExecutionResult
from arc.schemas.research import ResearchGoal
from arc.schemas.review import ReviewResult
from arc.session import new_session_id, session_paths, sim2l_home, validate_session_id

# Review item #T4: every route here runs against caller-controlled inputs;
# applying the bearer-token gate at router level (rather than per-endpoint)
# keeps it impossible to add a new route that forgets the check.
router = APIRouter(dependencies=[Depends(require_api_token)])


class LLMConfig(BaseModel):
    """Optional LLM provider config that can be supplied per-request."""
    provider: str | None = None     # anthropic | openai | openwebui
    token: str | None = None        # API key or bearer token
    model: str | None = None        # model name/id
    base_url: str | None = None     # for openwebui / custom endpoints


class ResearchRequest(BaseModel):
    goal: ResearchGoal
    llm: LLMConfig = LLMConfig()
    session_id: str | None = None
    iterations: int = Field(default=1, ge=1)
    workflow: str = "research-loop"


class ReviewRequest(BaseModel):
    result: ExecutionResult
    target: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


def _optional_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    return _require_session_id(session_id)


def _workflow(
    llm: LLMConfig,
    session_id: str | None = None,
    workflow_name: str = "research-loop",
) -> ResearchWorkflow:
    # Review item #T4: base_url flows into provider HTTP clients. Validate
    # it for openwebui (the only provider that actually honours base_url)
    # so /research/start can't be used as an SSRF primitive either.
    safe_base_url = llm.base_url
    if llm.provider == "openwebui" and llm.base_url:
        safe_base_url = validate_provider_base_url(llm.base_url)
    resolved_session = session_id or new_session_id()
    workflow = ResearchWorkflow(
        provider_name=llm.provider,
        token=llm.token,
        model=llm.model,
        base_url=safe_base_url,
        session_id=resolved_session,
        workflow_name=workflow_name,
    )

    # Splice persisted strategy + recipe state into the workflow's
    # context memory so the resolver picks them up. Without this, a
    # client that called POST /strategies/{role} or /recipes/{n}/apply
    # would see their choice silently ignored when /research/start ran
    # the workflow with default agents. Best-effort: a missing or
    # malformed state file falls through to defaults rather than
    # raising — the API surface should degrade, not 500.
    try:
        from arc.api.session_state import load_state
        from arc.session import load_session_meta

        state = load_state(resolved_session) or {}
        for key in ("strategy_overrides", "active_recipe",
                    "recipe_applied", "recipe_suggested"):
            if key in state and state[key]:
                workflow._context.memory[key] = state[key]
        meta = load_session_meta(resolved_session) or {}
        if meta.get("packages"):
            workflow._context.memory["packages"] = meta["packages"]
            refresh = getattr(workflow, "refresh_disabled_packages", None)
            if callable(refresh):
                refresh()
    except Exception:  # noqa: BLE001 — state lookup is best-effort
        pass
    return workflow


def _require_session_id(session_id: str | None) -> str:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    try:
        return validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _registry(session_id: str) -> ArtifactRegistry:
    paths = session_paths(session_id)
    return ArtifactRegistry(root=paths["artifacts"])


def _results(session_id: str) -> ResultsStore:
    paths = session_paths(session_id)
    return ResultsStore(root=paths["runs"])


def _all_session_results() -> list[ExecutionResult]:
    results: list[ExecutionResult] = []
    root = sim2l_home()
    if not root.exists():
        return results
    try:
        session_dirs = list(root.iterdir())
    except OSError:
        return results
    for session_dir in session_dirs:
        try:
            if not session_dir.is_dir():
                continue
        except OSError:
            continue
        runs_dir = session_dir / "runs"
        if not runs_dir.is_dir():
            continue
        store = ResultsStore(root=str(runs_dir))
        results.extend(store.list_all())
    return results


def _find_result(run_id: str) -> ExecutionResult:
    validate_run_id(run_id)
    for result in _all_session_results():
        if result.run_id == run_id:
            return result
    raise FileNotFoundError(run_id)


# --- Research ---

@router.post("/research/start")
async def start_research(req: ResearchRequest) -> dict[str, Any]:
    session_id = _optional_session_id(req.session_id) or new_session_id()
    workflow = _workflow(req.llm, session_id, req.workflow)
    results = []
    for _ in range(req.iterations):
        result = await workflow.run_once(req.goal)
        results.append(result)
        if result.get("review", {}).get("approved"):
            break
    return results[-1] if len(results) == 1 else {"iterations": results}


# --- Artifacts ---

@router.post("/artifact/create")
async def create_artifact(draft: ArtifactDraft, session_id: str | None = None):
    from arc.runtime.backend import safe_backend_action

    session_id = _require_session_id(session_id)
    artifact = _registry(session_id).register(draft)
    try:
        workflow = _workflow(LLMConfig(), session_id=session_id)
        await safe_backend_action(workflow.backend, "register_artifact", artifact)
    except Exception:  # noqa: BLE001 — backend publication is advisory
        pass
    return artifact


@router.get("/artifact/{artifact_id}")
async def get_artifact(artifact_id: str, version: str = "0.1.0", session_id: str | None = None):
    try:
        return _registry(_require_session_id(session_id)).get(artifact_id, version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Artifact not found")


@router.get("/artifact")
async def list_artifacts(session_id: str | None = None):
    if session_id:
        return _registry(_require_session_id(session_id)).list_all()
    artifacts = []
    root = sim2l_home()
    if root.exists():
        for session_dir in root.iterdir():
            if session_dir.is_dir():
                artifacts.extend(ArtifactRegistry(root=str(session_dir / "artifacts")).list_all())
    return artifacts


# --- Execution ---

@router.post("/execution/run")
async def run_execution(request: ExecutionRequest, session_id: str | None = None):
    from arc.runtime.backend import safe_backend_action

    if not request.artifact_id:
        raise HTTPException(status_code=400, detail="artifact_id is required")

    session_id = _require_session_id(session_id)
    reg = _registry(session_id)
    try:
        artifact = reg.get(request.artifact_id, request.version or "0.1.0")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Artifact not found")

    workflow = _workflow(LLMConfig(), session_id=session_id)
    inputs = await workflow.adapter.prepare_inputs(artifact, request.inputs)
    result = await workflow.adapter.run(artifact, inputs)
    workflow.results.save(result)
    await safe_backend_action(workflow.backend, "persist_result", artifact, result, inputs)
    await safe_backend_action(
        workflow.backend, "record_execution", artifact, result, inputs, result.outputs,
    )
    return result


@router.get("/execution/status/{run_id}")
async def get_status(run_id: str, session_id: str | None = None):
    try:
        result = (
            _results(_require_session_id(session_id)).get(run_id)
            if session_id else
            _find_result(run_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Result not found")
    return {"run_id": run_id, "status": result.status}


# --- Results ---

@router.get("/results/{run_id}")
async def get_result(run_id: str, session_id: str | None = None):
    try:
        if session_id:
            return _results(_require_session_id(session_id)).get(run_id)
        return _find_result(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Result not found")


@router.get("/results")
async def list_results(session_id: str | None = None):
    if session_id:
        return _results(_require_session_id(session_id)).list_all()
    return _all_session_results()


# --- Review ---

@router.post("/review/run")
async def run_review(req: ReviewRequest) -> ReviewResult:
    from arc.api.session_state import load_state
    from arc.contracts.agent import AgentContext
    from arc.core.config import load_arc_toml
    from arc.core.strategies import resolve_role as resolve_strategy_role
    from arc.session import load_session_meta

    target = dict(req.target)
    session_id = _optional_session_id(req.session_id) or "api"
    if req.session_id and not target:
        meta = load_session_meta(session_id)
        target = meta.get("target", {}) if meta else {}
    if not target:
        raise HTTPException(
            status_code=400,
            detail="review target is required unless session_id has a saved target",
        )

    # Pick the reviewer through the strategy resolver so a client that
    # selected ``reflective`` via POST /strategies/reviewer (or via a
    # /recipes/.../apply call) actually gets that reviewer, not the
    # bundled default. Falls through to default when no override is set.
    overrides: dict | None = None
    if req.session_id:
        state = load_state(session_id)
        overrides = state.get("strategy_overrides") or None
    try:
        _path, config = load_arc_toml()
    except Exception:
        config = {}
    ReviewerAgent = resolve_strategy_role(
        "reviewer", overrides=overrides, config=config,
    )

    # Hand the reviewer the same memory shape the chat layer gives it —
    # target plus the persisted state so reflective reviewers can read
    # run_history / failure_clusters when available.
    memory: dict = {"target": target}
    if req.session_id:
        meta = load_session_meta(session_id) or {}
        for key in ("run_history", "failure_clusters", "schema_registry"):
            if meta.get(key):
                memory[key] = meta[key]
    agent = ReviewerAgent(context=AgentContext(session_id=session_id, memory=memory))
    return await agent.run(req.result)


# --- Provider utilities ---

@router.post("/provider/models")
async def list_models(llm: LLMConfig) -> list[str]:
    """Return available models for the given provider/endpoint.

    Review item #T4: the openwebui branch dispatches an HTTP client at
    ``llm.base_url``. We validate it against the configured allowlist before
    creating the provider so a caller can't pivot ``/provider/models`` into
    an SSRF probe of internal services.
    """
    # Validate base_url (openwebui is the only provider that honours it)
    # before constructing, then resolve through the package-aware factory
    # and ask the provider itself for its models — no per-provider ladder.
    safe_url = validate_provider_base_url(llm.base_url) if llm.base_url else None
    from arc.orchestrator.workflow import _default_registry
    from arc.providers import build_provider
    provider = build_provider(
        llm.provider, token=llm.token, model=llm.model, base_url=safe_url,
        registry=_default_registry(),
    )
    lister = getattr(provider, "list_models", None) if provider else None
    if callable(lister):
        try:
            return list(lister())
        except Exception:  # noqa: BLE001 — listing is best-effort
            return []
    return []
