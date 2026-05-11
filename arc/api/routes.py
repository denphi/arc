from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from arc.memory.artifact_registry import ArtifactRegistry
from arc.memory.results_store import ResultsStore
from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.artifact import ArtifactDraft
from arc.schemas.execution import ExecutionRequest, ExecutionResult
from arc.schemas.research import ResearchGoal
from arc.schemas.review import ReviewResult
from arc.session import new_session_id, session_paths, sim2l_home, validate_session_id

router = APIRouter()


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
    return ResearchWorkflow(
        provider_name=llm.provider,
        token=llm.token,
        model=llm.model,
        base_url=llm.base_url,
        session_id=session_id or new_session_id(),
        workflow_name=workflow_name,
    )


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
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        store = ResultsStore(root=str(session_dir / "runs"))
        results.extend(store.list_all())
    return results


def _find_result(run_id: str) -> ExecutionResult:
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
    return _registry(_require_session_id(session_id)).register(draft)


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
    from arc.runtime.sim2l_adapter import Sim2LRuntimeAdapter

    if not request.artifact_id:
        raise HTTPException(status_code=400, detail="artifact_id is required")

    session_id = _require_session_id(session_id)
    reg = _registry(session_id)
    try:
        artifact = reg.get(request.artifact_id, request.version or "0.1.0")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Artifact not found")

    paths = session_paths(session_id)
    adapter = Sim2LRuntimeAdapter(db_path=paths["db"], session_id=session_id)
    result = await adapter.run(artifact, request.inputs)
    _results(session_id).save(result)
    return result


@router.get("/execution/status/{run_id}")
async def get_status(run_id: str):
    from arc.runtime.local import LocalRuntimeAdapter
    adapter = LocalRuntimeAdapter()
    return {"run_id": run_id, "status": await adapter.get_status(run_id)}


# --- Results ---

@router.get("/results/{run_id}")
async def get_result(run_id: str, session_id: str | None = None):
    try:
        return _results(_require_session_id(session_id)).get(run_id) if session_id else _find_result(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Result not found")


@router.get("/results")
async def list_results(session_id: str | None = None):
    return _results(_require_session_id(session_id)).list_all() if session_id else _all_session_results()


# --- Review ---

@router.post("/review/run")
async def run_review(req: ReviewRequest) -> ReviewResult:
    from arc.contracts.agent import AgentContext
    from arc.packages import load_reviewer
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

    ReviewerAgent = load_reviewer().ReviewerAgent
    agent = ReviewerAgent(context=AgentContext(session_id=session_id, memory={"target": target}))
    return await agent.run(req.result)


# --- Provider utilities ---

@router.post("/provider/models")
async def list_models(llm: LLMConfig) -> list[str]:
    """Return available models for the given provider/endpoint."""
    if llm.provider == "openwebui":
        from arc.providers.openwebui.provider import OpenWebUIProvider
        p = OpenWebUIProvider(base_url=llm.base_url, token=llm.token, model=llm.model)
        return p.list_models()
    if llm.provider == "anthropic":
        return ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    if llm.provider == "openai":
        return ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]
    return []
