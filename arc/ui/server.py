"""Standalone FastAPI server for the ARC browser UI.

This module deliberately does not import ``arc.chat``. The browser UI is
a separate surface over the core ARC primitives: sessions, memory stores,
runtime adapters, and ``ResearchWorkflow``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from arc.api import research_loop_routes as loop_actions
from arc.api.research_loop_routes import router as research_loop_router
from arc.api.routes import router as core_api_router
from arc.api.session_state import load_state, save_state
from arc.api.security import (
    load_security_config,
    require_api_token,
    validate_provider_base_url,
)
from arc.memory.artifact_registry import ArtifactRegistry
from arc.memory.results_store import ResultsStore
from arc.orchestrator.workflow import ResearchWorkflow
from arc.runtime.backend import safe_backend_action
from arc.schemas.research import ResearchGoal
from arc.ui import jobs
from arc.session import (
    delete_session as delete_session_dir,
    list_sessions,
    load_session_meta,
    new_session_id,
    save_session_meta,
    session_paths,
    validate_session_id,
)
from arc.version import __version__


STATIC_DIR = Path(__file__).parent / "static"
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
CONFIG_KEYS: tuple[str, ...] = (
    "ARC_PROVIDER",
    "ARC_MODEL",
    "ARC_API_TOKEN",
    "ARC_PROVIDER_ALLOWLIST",
    "ARC_ALLOW_PRIVATE_PROVIDER_HOSTS",
    "OPENWEBUI_URL",
    "OPENWEBUI_KEY",
    "OPENWEBUI_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
)
STATE_KEYS = (
    "strategy_overrides",
    "active_recipe",
    "recipe_applied",
    "recipe_suggested",
)

COMMANDS: list[dict[str, Any]] = [
    {
        "name": "help",
        "usage": "/help",
        "group": "General",
        "summary": "Show available UI commands and actions.",
        "aliases": [],
    },
    {
        "name": "run",
        "usage": "/run [goal]",
        "group": "Research",
        "summary": "Start or restart a research goal in the selected session.",
        "aliases": [],
    },
    {
        "name": "continue",
        "usage": "/continue",
        "group": "Research",
        "summary": "Run one more iteration for the saved goal.",
        "aliases": ["resume"],
    },
    {
        "name": "iterate",
        "usage": "/iterate [N]",
        "group": "Research",
        "summary": "Run several more iterations for the saved goal.",
        "aliases": [],
    },
    {
        "name": "target",
        "usage": "/target [key=value | update key=value | clear]",
        "group": "Research",
        "summary": "Show, set, update, or clear the active target.",
        "aliases": [],
    },
    {
        "name": "artifacts",
        "usage": "/artifacts",
        "group": "Session",
        "summary": "List registered artifacts for this session.",
        "aliases": [],
    },
    {
        "name": "results",
        "usage": "/results",
        "group": "Session",
        "summary": "List saved execution results for this session.",
        "aliases": [],
    },
    {
        "name": "sessions",
        "usage": "/sessions",
        "group": "Session",
        "summary": "List available sessions.",
        "aliases": [],
    },
    {
        "name": "exec",
        "usage": "/exec <artifact_id_or_name> [key=value ...]",
        "group": "Runtime",
        "summary": "Run an existing artifact with inputs.",
        "aliases": [],
    },
    {
        "name": "sweep",
        "usage": "/sweep [artifact_id_or_name]",
        "group": "Runtime",
        "summary": "Run the current parameter sweep when one is available.",
        "aliases": [],
    },
    {
        "name": "optimize",
        "usage": "/optimize [generations] [pop_size]",
        "group": "Runtime",
        "summary": "Run the optimizer for the current artifact.",
        "aliases": [],
    },
    {
        "name": "services",
        "usage": "/services [status|start|stop|restart|logs] [name]",
        "group": "Services",
        "summary": "Inspect or manage local sim2l services.",
        "aliases": [],
    },
    {
        "name": "strategy",
        "usage": "/strategy [role [impl|reset]]",
        "group": "Configuration",
        "summary": "Inspect or override role strategies.",
        "aliases": ["strategies"],
    },
    {
        "name": "recipe",
        "usage": "/recipe [list|show <n>|apply <n>|save <n>|delete <n>|clear]",
        "group": "Configuration",
        "summary": "List, inspect, apply, save, or delete recipes.",
        "aliases": ["recipes"],
    },
    {
        "name": "clusters",
        "usage": "/clusters [signature]",
        "group": "Knowledge",
        "summary": "Show failure clusters accumulated by the reflector.",
        "aliases": [],
    },
    {
        "name": "skills",
        "usage": "/skills [list|show <n>|delete <n>|export|import]",
        "group": "Knowledge",
        "summary": "List, view, delete, export, or import learned skills.",
        "aliases": ["skill"],
    },
    {
        "name": "packages",
        "usage": "/packages",
        "group": "Configuration",
        "summary": "List loaded ARC packages.",
        "aliases": [],
    },
    {
        "name": "package",
        "usage": "/package enable|disable <name>",
        "group": "Configuration",
        "summary": "Record a session package preference.",
        "aliases": [],
    },
    {
        "name": "coder",
        "usage": "/coder [builder|codex|claude]",
        "group": "Configuration",
        "summary": "Show or select the builder/coder strategy.",
        "aliases": [],
    },
]


class SessionCreateRequest(BaseModel):
    prefix: str | None = Field(default=None, max_length=20)
    goal: str | None = None


class ResearchRunRequest(BaseModel):
    session_id: str | None = None
    goal: str
    domain: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    iterations: int = Field(default=1, ge=1, le=20)
    workflow_name: str = "research-loop"
    provider: str | None = None
    token: str | None = None
    model: str | None = None
    base_url: str | None = None


class UIExecutionRequest(BaseModel):
    session_id: str
    artifact_id: str
    version: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    workflow_name: str = "research-loop"


class MessageRequest(BaseModel):
    session_id: str | None = None
    content: str = Field(..., min_length=1)
    domain: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    iterations: int = Field(default=1, ge=1, le=20)
    workflow_name: str = "research-loop"
    provider: str | None = None
    token: str | None = None
    model: str | None = None
    base_url: str | None = None


class CommandRunRequest(BaseModel):
    session_id: str | None = None
    command: str = Field(..., min_length=1)
    domain: str | None = None
    target: dict[str, Any] = Field(default_factory=dict)
    iterations: int = Field(default=1, ge=1, le=20)
    workflow_name: str = "research-loop"
    provider: str | None = None
    token: str | None = None
    model: str | None = None
    base_url: str | None = None


class ConfigSaveRequest(BaseModel):
    values: dict[str, str | None] = Field(default_factory=dict)


class TargetPatchRequest(BaseModel):
    target: dict[str, Any] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)
    replace: bool = False   # replace the whole target vs. merge


class StrategySetRequest(BaseModel):
    impl: str | None = None   # None/"reset"/"clear" → clear the override


class RecipeApplyBody(BaseModel):
    force: bool = False


class ArtifactCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: str = "0.1.0"


class WorkflowValidateRequest(BaseModel):
    source: str = Field(..., min_length=1)


def create_app() -> FastAPI:
    from arc.core.env import load_env
    load_env()  # populate os.environ from .env before any package/provider use
    app = FastAPI(
        title="ARC UI",
        description="Standalone browser UI for ARC sessions, artifacts, and runs",
        version=__version__,
    )
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    app.include_router(core_api_router, prefix="/api/core")
    app.include_router(research_loop_router, prefix="/api/actions")

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        # Cache-bust the JS/CSS with the arc version so a browser never serves
        # a stale app.js/app.css after an upgrade. The index itself is served
        # no-cache (it's tiny and carries the version stamp), so a plain reload
        # always picks up new assets — no hard-refresh needed.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("/assets/app.css", f"/assets/app.css?v={__version__}")
        html = html.replace("/assets/app.js", f"/assets/app.js?v={__version__}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "ui": "arc.ui"}

    # The data + execution endpoints carry the same bearer-token gate as
    # ``arc.api.routes`` (default-open; locked when ARC_API_TOKEN is set).
    # ``/``, ``/assets`` and ``/api/health`` stay open so the page can load
    # and probe health before a token is supplied. This matters because the
    # UI can be exposed beyond localhost (``python -m arc.ui --host 0.0.0.0``)
    # and ``/api/research/run`` + ``/api/executions/run`` spawn LLM calls and
    # execute workflow code.
    auth = [Depends(require_api_token)]

    @app.get("/api/config", dependencies=auth)
    def read_config() -> dict[str, Any]:
        return _config_snapshot()

    @app.put("/api/config", dependencies=auth)
    def save_config(body: ConfigSaveRequest) -> dict[str, Any]:
        values = _clean_env_values(body.values)
        _write_env_values(_config_env_path(), values)
        for key, value in values.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        _refresh_security_config()
        return _config_snapshot()

    @app.get("/api/sessions", dependencies=auth)
    def sessions() -> dict[str, Any]:
        return {"sessions": list_sessions()}

    @app.post("/api/sessions", dependencies=auth)
    def create_session(body: SessionCreateRequest) -> dict[str, Any]:
        session_id = new_session_id(body.prefix or "ui")
        save_session_meta(
            session_id=session_id,
            goal=body.goal or "",
            iteration=0,
            current_artifact_id=None,
            current_artifact_name=None,
            run_history=[],
            target={},
            next_parameters={},
        )
        return _session_snapshot(session_id)

    @app.get("/api/sessions/{session_id}", dependencies=auth)
    def session_detail(session_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        return _session_snapshot(session_id)

    @app.delete("/api/sessions/{session_id}", dependencies=auth)
    def delete_session(session_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        if not delete_session_dir(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"deleted": True, "session_id": session_id, "sessions": list_sessions()}

    @app.get("/api/commands", dependencies=auth)
    def command_manifest() -> dict[str, Any]:
        return {
            "commands": COMMANDS,
            "actions": [
                {
                    "name": "Core API",
                    "base_path": "/api/core",
                    "summary": "Research, artifact, execution, review, and provider-model endpoints.",
                },
                {
                    "name": "Research-loop API",
                    "base_path": "/api/actions",
                    "summary": "Strategies, recipes, clusters, and learned-skill endpoints.",
                },
            ],
        }

    @app.post("/api/commands/run", dependencies=auth)
    async def run_command(body: CommandRunRequest) -> dict[str, Any]:
        return await _dispatch_ui_command(body)

    @app.post("/api/messages", dependencies=auth)
    async def send_message(body: MessageRequest) -> dict[str, Any]:
        session_id = _ensure_session(body.session_id, goal=body.content, target=body.target)
        _append_thread(session_id, "user", body.content, title="You")
        try:
            result = await _run_research_iterations(
                session_id=session_id,
                goal_text=body.content,
                domain=body.domain,
                target=body.target,
                iterations=body.iterations,
                workflow_name=body.workflow_name,
                provider=body.provider,
                token=body.token,
                model=body.model,
                base_url=body.base_url,
            )
        except Exception as exc:
            _append_thread(session_id, "system", str(exc), title="Error")
            raise
        _append_thread(
            session_id,
            "assistant",
            _assistant_summary(result["result"]),
            title="ARC",
            payload=result["result"],
            suggestions=_suggestions_for(result["result"]),
        )
        return {
            "session_id": session_id,
            "message": result["result"],
            "session": _session_snapshot(session_id),
        }

    @app.post("/api/research/run", dependencies=auth)
    async def run_research(body: ResearchRunRequest) -> dict[str, Any]:
        session_id = _safe_session_id(body.session_id) if body.session_id else new_session_id("ui")
        _append_thread(session_id, "user", body.goal, title="Research")
        result = await _run_research_iterations(
            session_id=session_id,
            goal_text=body.goal,
            domain=body.domain,
            target=body.target,
            iterations=body.iterations,
            workflow_name=body.workflow_name,
            provider=body.provider,
            token=body.token,
            model=body.model,
            base_url=body.base_url,
        )
        _append_thread(
            session_id,
            "assistant",
            _assistant_summary(result["result"]),
            title="ARC",
            payload=result["result"],
            suggestions=_suggestions_for(result["result"]),
        )
        return {
            "session_id": session_id,
            "result": result["result"],
            "iterations": result["iterations"],
            "session": _session_snapshot(session_id),
        }

    @app.post("/api/executions/run", dependencies=auth)
    async def run_execution(body: UIExecutionRequest) -> dict[str, Any]:
        session_id = _safe_session_id(body.session_id)
        paths = session_paths(session_id)
        registry = ArtifactRegistry(root=paths["artifacts"])
        try:
            artifact = registry.get(body.artifact_id, body.version or "0.1.0")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Artifact not found")

        workflow = ResearchWorkflow(session_id=session_id, workflow_name=body.workflow_name)
        inputs = await workflow.adapter.prepare_inputs(artifact, body.inputs)
        result = await workflow.adapter.run(artifact, inputs)
        workflow.results.save(result)
        await safe_backend_action(workflow.backend, "register_artifact", artifact)
        await safe_backend_action(workflow.backend, "persist_result", artifact, result, inputs)
        await safe_backend_action(
            workflow.backend,
            "record_execution",
            artifact,
            result,
            inputs,
            result.outputs,
        )
        _append_thread(
            session_id,
            "tool",
            f"Executed {artifact.name}. Status: {result.status}.",
            title="Execution",
            payload=result.model_dump(),
        )
        return {
            "session_id": session_id,
            "artifact": artifact.model_dump(),
            "execution": result.model_dump(),
            "session": _session_snapshot(session_id),
        }

    # ── Phase 2 read routes ──────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/artifacts", dependencies=auth)
    def list_session_artifacts(session_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        paths = session_paths(session_id)
        return {"session_id": session_id,
                "artifacts": _list_artifacts_readonly(Path(paths["artifacts"]))}

    @app.get("/api/sessions/{session_id}/artifacts/{artifact_id}", dependencies=auth)
    def get_session_artifact(
        session_id: str, artifact_id: str, version: str | None = None,
    ) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        registry = ArtifactRegistry(root=session_paths(session_id)["artifacts"])
        artifact = _resolve_artifact(registry, artifact_id, version or "0.1.0")
        return {
            "session_id": session_id,
            "artifact": artifact.model_dump(),
            "files": _list_artifact_files(Path(artifact.path)),
        }

    @app.get("/api/sessions/{session_id}/artifacts/{artifact_id}/files/{file_path:path}",
             dependencies=auth)
    def get_artifact_file(
        session_id: str, artifact_id: str, file_path: str, version: str | None = None,
    ) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        registry = ArtifactRegistry(root=session_paths(session_id)["artifacts"])
        artifact = _resolve_artifact(registry, artifact_id, version or "0.1.0")
        content = _read_artifact_file(Path(artifact.path), file_path)
        return {"session_id": session_id, "artifact_id": artifact.artifact_id,
                "path": file_path, "content": content}

    @app.get("/api/sessions/{session_id}/results", dependencies=auth)
    def list_session_results(session_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        paths = session_paths(session_id)
        return {"session_id": session_id,
                "results": _list_results_readonly(Path(paths["runs"]))}

    @app.get("/api/sessions/{session_id}/results/{run_id}", dependencies=auth)
    def get_session_result(session_id: str, run_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        store = ResultsStore(root=session_paths(session_id)["runs"])
        try:
            result = store.get(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Result not found")
        return {"session_id": session_id, "result": result.model_dump(),
                "review": _review_for_result(session_id, run_id)}

    @app.get("/api/sessions/{session_id}/state", dependencies=auth)
    def get_session_state(session_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        return {"session_id": session_id, "state": load_state(session_id),
                "meta": load_session_meta(session_id)}

    @app.patch("/api/sessions/{session_id}/target", dependencies=auth)
    def patch_session_target(session_id: str, body: TargetPatchRequest) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        meta = load_session_meta(session_id) or {}
        current = dict(meta.get("target") or {})
        if body.replace:
            target = dict(body.target)
        else:
            target = {**current, **body.target}
            for key in body.remove or []:
                target.pop(key, None)
        _save_session_fields(session_id, target=target)
        return {"session_id": session_id, "target": target,
                "session": _session_snapshot(session_id)}

    @app.get("/api/strategies", dependencies=auth)
    def list_strategies(session_id: str | None = None) -> dict[str, Any]:
        return loop_actions.list_strategies_endpoint(
            session_id=_safe_session_id(session_id) if session_id else "ui-default")

    @app.put("/api/sessions/{session_id}/strategies/{role}", dependencies=auth)
    def set_strategy(session_id: str, role: str, body: StrategySetRequest) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        if body.impl in (None, "", "reset", "clear"):
            return loop_actions.clear_strategy_endpoint(role=role, session_id=session_id)
        return loop_actions.set_strategy_endpoint(
            role=role,
            body=loop_actions.StrategyOverrideRequest(impl=body.impl),
            session_id=session_id,
        )

    @app.get("/api/recipes", dependencies=auth)
    def list_recipes(session_id: str | None = None) -> dict[str, Any]:
        return loop_actions.list_recipes_endpoint(
            session_id=_safe_session_id(session_id) if session_id else "ui-default")

    @app.post("/api/sessions/{session_id}/recipes/{name}/apply", dependencies=auth)
    def apply_recipe(session_id: str, name: str, body: RecipeApplyBody) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        return loop_actions.apply_recipe_endpoint(
            name,
            loop_actions.RecipeApplyRequest(force=body.force),
            session_id=session_id,
        )

    # ── Phase 4: artifact authoring ──────────────────────────────────────

    @app.post("/api/workflow/validate", dependencies=auth)
    def validate_workflow(body: WorkflowValidateRequest) -> dict[str, Any]:
        """Dry-run the workflow safety check on draft source — no file write."""
        from arc.runtime.workflow_safety import check_workflow_source_safe
        ok, message = check_workflow_source_safe(body.source)
        return {"valid": ok, "message": message}

    @app.post("/api/sessions/{session_id}/artifacts", dependencies=auth)
    def create_artifact(session_id: str, body: ArtifactCreateRequest) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        # Safety-check any workflow.py before registering it.
        source = body.files.get("workflow.py")
        if source:
            from arc.runtime.workflow_safety import check_workflow_source_safe
            ok, message = check_workflow_source_safe(source)
            if not ok:
                raise HTTPException(status_code=400, detail=f"Unsafe workflow: {message}")
        registry = ArtifactRegistry(root=session_paths(session_id)["artifacts"])
        from arc.schemas.artifact import ArtifactDraft
        try:
            record = registry.register(
                ArtifactDraft(
                    name=body.name, description=body.description,
                    files=body.files, metadata=body.metadata,
                ),
                version=body.version,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _append_thread(session_id, "tool", f"Created artifact {record.name}.",
                       title="Artifact", payload=record.model_dump())
        return {"session_id": session_id, "artifact": record.model_dump(),
                "files": _list_artifact_files(Path(record.path)),
                "session": _session_snapshot(session_id)}

    @app.get("/api/sessions/{session_id}/artifacts/{artifact_id}/diff", dependencies=auth)
    def diff_artifact_versions(
        session_id: str, artifact_id: str, left: str, right: str,
    ) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        registry = ArtifactRegistry(root=session_paths(session_id)["artifacts"])
        left_rec = _resolve_artifact(registry, artifact_id, left)
        right_rec = _resolve_artifact(registry, artifact_id, right)
        return {
            "session_id": session_id, "artifact_id": left_rec.artifact_id,
            "left": left, "right": right,
            "diff": _diff_artifact_files(Path(left_rec.path), Path(right_rec.path)),
        }

    # ── Phase 3: async jobs + SSE ────────────────────────────────────────

    @app.post("/api/jobs/research", dependencies=auth)
    async def start_research_job(body: MessageRequest) -> dict[str, Any]:
        session_id = _ensure_session(body.session_id, goal=body.content, target=body.target)

        async def _body(job) -> dict[str, Any]:
            return await _run_research_job(job, session_id, body)

        job = jobs.registry.start("research", session_id, _body)
        return {"job_id": job.job_id, "session_id": session_id, "status": job.status}

    @app.get("/api/jobs", dependencies=auth)
    def list_jobs() -> dict[str, Any]:
        return {"jobs": jobs.registry.list()}

    @app.get("/api/jobs/{job_id}", dependencies=auth)
    def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        payload = job.to_dict()
        payload["events"] = job.events.buffered()
        return payload

    @app.post("/api/jobs/{job_id}/cancel", dependencies=auth)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        if jobs.registry.get(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        cancelled = await jobs.registry.cancel(job_id)
        return {"job_id": job_id, "cancelled": cancelled,
                "status": jobs.registry.get(job_id).status}

    # ── sim2l services: status + on-demand start (mirrors the CLI prompt) ──

    @app.get("/api/services", dependencies=auth)
    def services_status() -> dict[str, Any]:
        """Status of the local sim2l services + whether the UI should offer to
        start them. ``prompt_start`` is True when sim2l is installed but no
        services are running — the same condition the CLI chat uses to ask
        'Start sim2l services now?'."""
        from arc.services import sim2l_available, status_all
        services = status_all()
        any_running = any(
            (s.get("running") if isinstance(s, dict) else bool(s))
            for s in services.values()
        )
        available = sim2l_available()
        return {
            "available": available,
            "any_running": any_running,
            "prompt_start": available and not any_running,
            "services": services,
        }

    @app.post("/api/services/start", dependencies=auth)
    async def services_start() -> dict[str, Any]:
        """Start the (non-optional) sim2l services, like '/services start'."""
        from arc.services import sim2l_available, start_all, status_all
        if not sim2l_available():
            raise HTTPException(status_code=400, detail="sim2l package is not installed")
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, start_all)
        reports = [{"service": n, "ok": ok, "message": m} for (n, ok, m) in results]
        # Give freshly-spawned Flask services a moment to answer /health.
        for _ in range(10):
            services = await loop.run_in_executor(None, status_all)
            if all(
                (s.get("running") if isinstance(s, dict) else bool(s))
                for s in services.values()
            ):
                break
            await asyncio.sleep(0.5)
        return {"reports": reports, "services": status_all()}

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(_require_token_header_or_query)])
    async def job_events(job_id: str, request: Request, token: str | None = None) -> StreamingResponse:
        job = jobs.registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return StreamingResponse(
            _sse_event_generator(job, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _require_token_header_or_query(
    authorization: str | None = Header(default=None),
    token: str | None = None,
) -> None:
    """Auth for SSE: a browser ``EventSource`` can't set an Authorization
    header, so this dependency accepts the bearer token from either the
    header *or* a ``?token=`` query param, validated against the same
    configured ``ARC_API_TOKEN``. Default-open when no token is configured
    (mirrors ``require_api_token``)."""
    cfg = load_security_config()
    if not cfg.token:
        return
    supplied = None
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            supplied = parts[1]
    supplied = supplied or token
    if supplied != cfg.token:
        raise HTTPException(status_code=401, detail="Invalid credentials")


def _safe_session_id(session_id: str | None) -> str:
    try:
        return validate_session_id(session_id or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _config_env_path() -> Path:
    configured = os.environ.get("ARC_UI_ENV_PATH")
    if configured:
        return Path(configured).expanduser()
    cwd = Path.cwd()
    for candidate in (cwd / ".env", cwd.parent / ".env"):
        if candidate.exists():
            return candidate
    return cwd / ".env"


def _config_snapshot() -> dict[str, Any]:
    path = _config_env_path()
    file_values = _read_env_values(path)
    values = {
        key: file_values.get(key, os.environ.get(key, ""))
        for key in CONFIG_KEYS
    }
    extra = {
        key: value
        for key, value in file_values.items()
        if key not in CONFIG_KEYS
    }
    return {
        "path": str(path),
        "exists": path.exists(),
        "keys": list(CONFIG_KEYS),
        "values": values,
        "extra": extra,
    }


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed:
            key, value = parsed
            values[key] = value
    return values


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not ENV_KEY_RE.fullmatch(key):
        return None
    return key, _decode_env_value(raw_value.strip())


def _decode_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return parsed if isinstance(parsed, str) else str(parsed)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _clean_env_values(values: dict[str, str | None]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key).strip()
        if not ENV_KEY_RE.fullmatch(key):
            raise HTTPException(status_code=400, detail=f"Invalid environment key: {raw_key}")
        value = "" if raw_value is None else str(raw_value).strip()
        if "\n" in value or "\r" in value:
            raise HTTPException(status_code=400, detail=f"Invalid multiline value for {key}")
        cleaned[key] = value
    return cleaned


def _write_env_values(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated: list[str] = []
    seen: set[str] = set()

    for line in lines:
        parsed = _parse_env_line(line)
        key = parsed[0] if parsed else None
        if key in values:
            seen.add(key)
            if values[key]:
                updated.append(f"{key}={_format_env_value(values[key])}")
            continue
        updated.append(line)

    additions = [
        key
        for key in values
        if key not in seen and values[key]
    ]
    if additions:
        if updated and updated[-1].strip():
            updated.append("")
        for key in additions:
            updated.append(f"{key}={_format_env_value(values[key])}")

    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(updated).rstrip()
    path.write_text(f"{content}\n" if content else "", encoding="utf-8")


def _format_env_value(value: str) -> str:
    if not value:
        return ""
    if any(char.isspace() for char in value) or "#" in value or '"' in value:
        return json.dumps(value)
    return value


def _refresh_security_config() -> None:
    from arc.api import security as api_security

    api_security.load_security_config.cache_clear()


def _ensure_session(
    session_id: str | None,
    *,
    goal: str = "",
    target: dict[str, Any] | None = None,
) -> str:
    safe_id = _safe_session_id(session_id) if session_id else new_session_id("ui")
    if not load_session_meta(safe_id):
        save_session_meta(
            session_id=safe_id,
            goal=goal,
            iteration=0,
            current_artifact_id=None,
            current_artifact_name=None,
            run_history=[],
            target=target or {},
            next_parameters={},
        )
    return safe_id


def _session_snapshot(session_id: str) -> dict[str, Any]:
    session_id = _safe_session_id(session_id)
    paths = session_paths(session_id)
    artifacts = _list_artifacts_readonly(Path(paths["artifacts"]))
    results = _list_results_readonly(Path(paths["runs"]))
    return {
        "session_id": session_id,
        "meta": load_session_meta(session_id),
        "state": load_state(session_id),
        "artifacts": artifacts,
        "results": results,
        "thread": _thread_for_session(session_id, results),
    }


def _list_artifacts_readonly(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    return [artifact.model_dump() for artifact in ArtifactRegistry(root=str(root)).list_all()]


def _list_results_readonly(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    return [result.model_dump() for result in ResultsStore(root=str(root)).list_all()]


# Artifact files we expose to the browser viewer. A fixed allowlist of names
# plus any *.py/*.yaml/*.yml/*.md/*.json/*.txt under the artifact dir, so we
# never serve, e.g., a stray secret a workflow wrote. Internal record files are
# excluded (they're returned via the artifact record endpoint already).
_VIEWABLE_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".json", ".txt", ".cfg", ".toml"}
_HIDDEN_ARTIFACT_FILES = {"arc_record.json", "arc_metadata.json"}
_MAX_VIEW_BYTES = 512 * 1024  # 512 KiB cap so the browser isn't handed a huge blob


def _list_artifact_files(artifact_dir: Path) -> list[dict[str, Any]]:
    """List viewable files inside an artifact directory (non-recursive +
    one level deep), with size, relative to the artifact root."""
    if not artifact_dir.is_dir():
        return []
    root = artifact_dir.resolve()
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in _HIDDEN_ARTIFACT_FILES:
            continue
        if path.suffix.lower() not in _VIEWABLE_SUFFIXES:
            continue
        try:
            rel = path.resolve().relative_to(root)
        except ValueError:
            continue  # symlink escaping the dir — skip
        files.append({"path": str(rel), "size": path.stat().st_size})
    return files


def _read_artifact_file(artifact_dir: Path, rel_path: str) -> str:
    """Read one file from inside the artifact dir, containing the path.

    Resolves the requested path and verifies it stays within the artifact
    directory (defeats ``../`` traversal and absolute paths). Refuses files
    outside the viewable allowlist or above the size cap.
    """
    if not rel_path or rel_path.startswith(("/", "\\")) or "\x00" in rel_path:
        raise HTTPException(status_code=400, detail="Invalid file path")
    root = artifact_dir.resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes the artifact directory")
    if target.name in _HIDDEN_ARTIFACT_FILES or target.suffix.lower() not in _VIEWABLE_SUFFIXES:
        raise HTTPException(status_code=403, detail="File type is not viewable")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target.stat().st_size > _MAX_VIEW_BYTES:
        raise HTTPException(status_code=413, detail="File too large to view")
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read file: {exc}")


def _diff_artifact_files(left_dir: Path, right_dir: Path) -> list[dict[str, Any]]:
    """Unified diff of each viewable file between two artifact versions.

    Files present in either version are compared; a missing side reads as
    empty so adds/removes show. Returns one entry per file with a unified
    diff string (empty when unchanged)."""
    import difflib

    left_files = {f["path"] for f in _list_artifact_files(left_dir)}
    right_files = {f["path"] for f in _list_artifact_files(right_dir)}
    out: list[dict[str, Any]] = []
    for rel in sorted(left_files | right_files):
        try:
            left_text = _read_artifact_file(left_dir, rel) if rel in left_files else ""
        except HTTPException:
            left_text = ""
        try:
            right_text = _read_artifact_file(right_dir, rel) if rel in right_files else ""
        except HTTPException:
            right_text = ""
        diff = "".join(difflib.unified_diff(
            left_text.splitlines(keepends=True),
            right_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        ))
        out.append({"path": rel, "changed": left_text != right_text, "diff": diff})
    return out


def _review_for_result(session_id: str, run_id: str) -> dict[str, Any]:
    """Best-effort: find the review mapped to a run in this session's
    run_history (the loop records review alongside each iteration)."""
    meta = load_session_meta(session_id) or {}
    for entry in meta.get("run_history") or []:
        if not isinstance(entry, dict):
            continue
        entry_run = (
            entry.get("run_id")
            or _nested_value(entry, "execution", "run_id")
            or _nested_value(entry, "result", "run_id")
        )
        if entry_run == run_id and isinstance(entry.get("review"), dict):
            return entry["review"]
    return {}


def _save_workflow_meta(workflow: ResearchWorkflow, goal: str, target: dict[str, Any]) -> None:
    memory = workflow._context.memory
    artifact = memory.get("current_artifact")
    save_session_meta(
        session_id=workflow.session_id,
        goal=goal,
        iteration=workflow._context.iteration,
        current_artifact_id=getattr(artifact, "artifact_id", None),
        current_artifact_name=getattr(artifact, "name", None),
        run_history=memory.get("run_history", []),
        target=target or memory.get("target", {}),
        next_parameters=memory.get("next_parameters", {}),
        schema_registry=memory.get("schema_registry", {}),
        primary_goal=memory.get("primary_goal", goal),
        refinements=memory.get("refinements", []),
        packages=memory.get("packages", {}),
        agent_overrides=memory.get("agent_overrides", {}),
    )
    save_state(workflow.session_id, {key: memory.get(key) for key in STATE_KEYS})


def _hydrate_workflow_state(workflow: ResearchWorkflow) -> None:
    """Restore the pieces chat and HTTP actions persist between turns."""
    memory = workflow._context.memory
    state = load_state(workflow.session_id) or {}
    for key in STATE_KEYS:
        if state.get(key):
            memory[key] = state[key]

    meta = load_session_meta(workflow.session_id) or {}
    if not meta:
        return

    workflow._context.iteration = meta.get("iteration", 0)
    for key in (
        "run_history",
        "target",
        "next_parameters",
        "schema_registry",
        "primary_goal",
        "refinements",
        "packages",
        "agent_overrides",
    ):
        if key in meta:
            memory[key] = meta[key]

    artifact_id = meta.get("current_artifact_id")
    if artifact_id:
        try:
            memory["current_artifact"] = workflow.artifacts.get(artifact_id)
        except Exception:
            pass


def _save_session_fields(session_id: str, **updates: Any) -> None:
    meta = load_session_meta(session_id) or {}
    save_session_meta(
        session_id=session_id,
        goal=updates.get("goal", meta.get("goal", "")),
        iteration=updates.get("iteration", meta.get("iteration", 0)),
        current_artifact_id=updates.get("current_artifact_id", meta.get("current_artifact_id")),
        current_artifact_name=updates.get("current_artifact_name", meta.get("current_artifact_name")),
        run_history=updates.get("run_history", meta.get("run_history", [])),
        target=updates.get("target", meta.get("target", {})),
        next_parameters=updates.get("next_parameters", meta.get("next_parameters", {})),
        created=updates.get("created", meta.get("created")),
        schema_registry=updates.get("schema_registry", meta.get("schema_registry", {})),
        primary_goal=updates.get("primary_goal", meta.get("primary_goal")),
        refinements=updates.get("refinements", meta.get("refinements", [])),
        packages=updates.get("packages", meta.get("packages", {})),
        agent_overrides=updates.get("agent_overrides", meta.get("agent_overrides", {})),
    )


async def _execute_artifact(
    *,
    session_id: str,
    artifact_ref: str,
    version: str | None,
    inputs: dict[str, Any],
    workflow_name: str,
) -> dict[str, Any]:
    workflow = ResearchWorkflow(session_id=session_id, workflow_name=workflow_name)
    _hydrate_workflow_state(workflow)
    artifact = _resolve_artifact(workflow.artifacts, artifact_ref, version or "0.1.0")
    prepared = await workflow.adapter.prepare_inputs(artifact, inputs)
    result = await workflow.adapter.run(artifact, prepared)
    workflow.results.save(result)
    await safe_backend_action(workflow.backend, "register_artifact", artifact)
    await safe_backend_action(workflow.backend, "persist_result", artifact, result, prepared)
    await safe_backend_action(
        workflow.backend,
        "record_execution",
        artifact,
        result,
        prepared,
        result.outputs,
    )
    return {
        "session_id": session_id,
        "artifact": artifact.model_dump(),
        "inputs": prepared,
        "execution": result.model_dump(),
        "session": _session_snapshot(session_id),
    }


def _resolve_artifact(
    registry: ArtifactRegistry,
    artifact_ref: str | None,
    version: str = "0.1.0",
):
    if not artifact_ref:
        raise HTTPException(status_code=400, detail="artifact id or name is required")
    try:
        return registry.get(artifact_ref, version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        records = registry.list_all()
        matches = [
            item for item in records
            if item.artifact_id.startswith(artifact_ref) or item.name == artifact_ref
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail=f"{artifact_ref!r} matches multiple artifacts",
            )
        raise HTTPException(status_code=404, detail="Artifact not found")


def _canonical_command(name: str) -> str | None:
    lowered = name.lower().lstrip("/")
    for command in COMMANDS:
        if lowered == command["name"] or lowered in command.get("aliases", []):
            return command["name"]
    return None


def _command_response(
    command: str,
    session_id: str | None,
    text: str,
    payload: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "command": command,
        "status": "completed",
        "text": text,
        "payload": payload,
    }
    if session_id:
        response["session_id"] = session_id
        response["session"] = _session_snapshot(session_id)
    return response


def _help_text() -> str:
    lines = ["Available commands:"]
    for command in COMMANDS:
        lines.append(f"{command['usage']} - {command['summary']}")
    return "\n".join(lines)


def _list_text(title: str, items: list[dict[str, Any]], key: str) -> str:
    if not items:
        return f"No {title.lower()}."
    preview = ", ".join(str(item.get(key, "?")) for item in items[:6])
    suffix = "" if len(items) <= 6 else f", +{len(items) - 6} more"
    return f"{title}: {preview}{suffix}."


def _positive_int(value: str | int | None, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_key_values(argv: list[str]) -> dict[str, Any]:
    if not argv:
        return {}
    raw = " ".join(argv).strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc.msg}")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        return parsed

    values: dict[str, Any] = {}
    idx = 0
    while idx < len(argv):
        item = argv[idx]
        if "=" in item:
            key, value = item.split("=", 1)
            idx += 1
        elif idx + 1 < len(argv):
            key, value = item, argv[idx + 1]
            idx += 2
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Expected key=value pair, got {item!r}",
            )
        key = key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="Empty key in key=value pair")
        values[key] = _parse_scalar(value)
    return values


def _parse_scalar(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


async def _run_research_iterations(
    *,
    session_id: str,
    goal_text: str,
    domain: str | None,
    target: dict[str, Any],
    iterations: int,
    workflow_name: str,
    provider: str | None,
    token: str | None,
    model: str | None,
    base_url: str | None,
) -> dict[str, Any]:
    # Validate any caller-supplied provider base_url against the SSRF
    # allowlist/private-host policy before it reaches the provider.
    safe_base_url = validate_provider_base_url(base_url) if base_url else None
    workflow = ResearchWorkflow(
        provider_name=provider,
        token=token,
        model=model,
        base_url=safe_base_url,
        session_id=session_id,
        workflow_name=workflow_name,
    )
    _hydrate_workflow_state(workflow)
    goal = ResearchGoal(goal=goal_text, domain=domain, target=target)
    results: list[dict[str, Any]] = []
    for _ in range(iterations):
        result = await workflow.run_once(goal)
        results.append(result)
        if (result.get("review") or {}).get("approved"):
            break
    _save_workflow_meta(workflow, goal_text, target)
    return {"result": results[-1] if results else None, "iterations": results}


async def _run_research_job(job, session_id: str, body: "MessageRequest") -> dict[str, Any]:
    """Job body: run research iterations, emitting structured UI events per
    iteration so the browser timeline updates live over SSE. Persists the
    thread + summary the same way the synchronous route does."""
    _append_thread(session_id, "user", body.content, title="You")
    safe_base_url = validate_provider_base_url(body.base_url) if body.base_url else None
    workflow = ResearchWorkflow(
        provider_name=body.provider, token=body.token, model=body.model,
        base_url=safe_base_url, session_id=session_id, workflow_name=body.workflow_name,
    )
    _hydrate_workflow_state(workflow)
    goal = ResearchGoal(goal=body.content, domain=body.domain, target=body.target)
    total = max(1, body.iterations)
    results: list[dict[str, Any]] = []
    for index in range(total):
        job.events.emit("phase_start", f"Iteration {index + 1}/{total}",
                        phase="iteration", index=index + 1)
        result = await workflow.run_once(goal)
        results.append(result)
        job.progress = (index + 1) / total
        execution = (result or {}).get("execution") or {}
        review = (result or {}).get("review") or {}
        job.events.emit("result", _assistant_summary(result), phase="iteration",
                        index=index + 1, status=execution.get("status"))
        if review.get("summary"):
            job.events.emit("review", str(review["summary"]), index=index + 1)
        job.events.emit("phase_end", f"Iteration {index + 1} done", phase="iteration",
                        index=index + 1)
        if review.get("approved"):
            job.events.emit("status", "Goal approved — stopping early.")
            break
    _save_workflow_meta(workflow, body.content, body.target)
    final = results[-1] if results else None
    _append_thread(session_id, "assistant", _assistant_summary(final), title="ARC",
                   payload=final, suggestions=_suggestions_for(final))
    return {"result": final, "iterations": results, "session_id": session_id}


async def _sse_event_generator(job, request: "Request"):
    """Yield SSE frames for a job: buffered history first, then live events
    until the stream closes or the client disconnects."""
    queue, buffered = job.events.subscribe()
    try:
        for event in buffered:
            yield _sse_frame(event.to_dict())
        # If the job already finished, the buffer is the whole story.
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"   # comment frame to hold the connection
                continue
            if event is None:           # stream closed sentinel
                yield _sse_frame({"kind": "done", "text": "", "meta": {}, "ts": 0})
                break
            yield _sse_frame(event.to_dict())
    finally:
        job.events.unsubscribe(queue)


def _sse_frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


async def _dispatch_ui_command(body: CommandRunRequest) -> dict[str, Any]:
    raw = body.command.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="command is required")
    if raw.lower() in {"continue", "resume"}:
        raw = "/" + raw.lower()
    if raw.startswith("\\"):
        raw = "/" + raw[1:]

    if not raw.startswith("/"):
        message = MessageRequest(
            session_id=body.session_id,
            content=raw,
            domain=body.domain,
            target=body.target,
            iterations=body.iterations,
            workflow_name=body.workflow_name,
            provider=body.provider,
            token=body.token,
            model=body.model,
            base_url=body.base_url,
        )
        return await _dispatch_free_text(message)

    try:
        tokens = shlex.split(raw[1:], posix=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"could not parse command: {exc}")
    if not tokens:
        raise HTTPException(status_code=400, detail="bare slash command")

    command = _canonical_command(tokens[0])
    argv = tokens[1:]
    if not command:
        raise HTTPException(status_code=400, detail=f"unknown command: /{tokens[0]}")

    session_id = (
        _ensure_session(body.session_id)
        if body.session_id or command not in {"help", "sessions", "services"}
        else None
    )
    if session_id:
        _append_thread(session_id, "user", raw, title="Command")

    try:
        text, payload = await _execute_command(command, argv, body, session_id)
    except HTTPException as exc:
        if session_id:
            _append_thread(session_id, "system", str(exc.detail), title="Command Error")
        raise
    except Exception as exc:
        if session_id:
            _append_thread(session_id, "system", str(exc), title="Command Error")
        raise HTTPException(status_code=500, detail=str(exc))

    if session_id:
        # Research commands (run/continue/iterate) return {"result": …};
        # surface next-step chips off that result. Other commands get none.
        run_result = payload.get("result") if isinstance(payload, dict) else None
        _append_thread(
            session_id,
            "assistant",
            text,
            title=f"/{command}",
            payload=payload if isinstance(payload, dict) else {"value": payload},
            suggestions=_suggestions_for(run_result) if command in
            {"run", "continue", "iterate"} else [],
        )
    return _command_response(command, session_id, text, payload)


async def _dispatch_free_text(body: MessageRequest) -> dict[str, Any]:
    session_id = _ensure_session(body.session_id, goal=body.content, target=body.target)
    _append_thread(session_id, "user", body.content, title="You")
    result = await _run_research_iterations(
        session_id=session_id,
        goal_text=body.content,
        domain=body.domain,
        target=body.target,
        iterations=body.iterations,
        workflow_name=body.workflow_name,
        provider=body.provider,
        token=body.token,
        model=body.model,
        base_url=body.base_url,
    )
    text = _assistant_summary(result["result"])
    _append_thread(
        session_id,
        "assistant",
        text,
        title="ARC",
        payload=result["result"],
        suggestions=_suggestions_for(result["result"]),
    )
    return _command_response("message", session_id, text, result)


async def _execute_command(
    command: str,
    argv: list[str],
    body: CommandRunRequest,
    session_id: str | None,
) -> tuple[str, Any]:
    if command == "help":
        return _help_text(), {"commands": COMMANDS}
    if command == "sessions":
        sessions = list_sessions()
        return _list_text("Sessions", sessions, "session_id"), {"sessions": sessions}
    if command == "artifacts":
        assert session_id is not None
        artifacts = _session_snapshot(session_id)["artifacts"]
        return _list_text("Artifacts", artifacts, "artifact_id"), {"artifacts": artifacts}
    if command == "results":
        assert session_id is not None
        results = _session_snapshot(session_id)["results"]
        return _list_text("Results", results, "run_id"), {"results": results}
    if command in {"run", "continue", "iterate"}:
        assert session_id is not None
        return await _handle_research_command(command, argv, body, session_id)
    if command == "target":
        assert session_id is not None
        return _handle_target_command(session_id, argv)
    if command == "exec":
        assert session_id is not None
        return await _handle_exec_command(session_id, argv, body.workflow_name)
    if command == "sweep":
        assert session_id is not None
        return await _handle_sweep_command(session_id, argv, body.workflow_name)
    if command == "optimize":
        assert session_id is not None
        return await _handle_optimize_command(session_id, argv, body)
    if command == "services":
        return await _handle_services_command(argv)
    if command == "strategy":
        assert session_id is not None
        return _handle_strategy_command(session_id, argv)
    if command == "recipe":
        assert session_id is not None
        return _handle_recipe_command(session_id, argv)
    if command == "clusters":
        assert session_id is not None
        return _handle_clusters_command(session_id, argv)
    if command == "skills":
        assert session_id is not None
        return _handle_skills_command(session_id, argv)
    if command == "packages":
        assert session_id is not None
        return _handle_packages_command(session_id)
    if command == "package":
        assert session_id is not None
        return _handle_package_toggle_command(session_id, argv)
    if command == "coder":
        assert session_id is not None
        return _handle_coder_command(session_id, argv)
    raise HTTPException(status_code=400, detail=f"unsupported command: /{command}")


async def _handle_research_command(
    command: str,
    argv: list[str],
    body: CommandRunRequest,
    session_id: str,
) -> tuple[str, Any]:
    meta = load_session_meta(session_id) or {}
    target = body.target or meta.get("target", {}) or {}
    if command == "run":
        goal_text = " ".join(argv).strip() or meta.get("goal") or meta.get("primary_goal")
        iterations = body.iterations
        if not goal_text:
            raise HTTPException(status_code=400, detail="No goal. Use /run <goal>.")
        _save_session_fields(
            session_id,
            goal=goal_text,
            iteration=0,
            current_artifact_id=None,
            current_artifact_name=None,
            run_history=[],
            target=target,
            next_parameters={},
            primary_goal=goal_text,
            refinements=[],
        )
    else:
        goal_text = meta.get("primary_goal") or meta.get("goal")
        if not goal_text:
            raise HTTPException(status_code=400, detail="No saved goal. Use /run <goal>.")
        iterations = 1 if command == "continue" else _positive_int(argv[0], default=body.iterations or 3) if argv else 3

    result = await _run_research_iterations(
        session_id=session_id,
        goal_text=goal_text,
        domain=body.domain,
        target=target,
        iterations=iterations,
        workflow_name=body.workflow_name,
        provider=body.provider,
        token=body.token,
        model=body.model,
        base_url=body.base_url,
    )
    return _assistant_summary(result["result"]), result


def _handle_target_command(session_id: str, argv: list[str]) -> tuple[str, Any]:
    meta = load_session_meta(session_id) or {}
    current = dict(meta.get("target") or {})
    if not argv:
        text = f"Target: {json.dumps(current, sort_keys=True)}" if current else "No target set."
        return text, {"target": current}

    action = argv[0].lower()
    if action in {"clear", "reset", "none"}:
        target: dict[str, Any] = {}
    elif action == "update":
        target = {**current, **_parse_key_values(argv[1:])}
    else:
        target = _parse_key_values(argv)

    _save_session_fields(session_id, target=target)
    text = "Target cleared." if not target else f"Target set to {json.dumps(target, sort_keys=True)}."
    return text, {"target": target}


async def _handle_exec_command(
    session_id: str,
    argv: list[str],
    workflow_name: str,
) -> tuple[str, Any]:
    if not argv:
        raise HTTPException(status_code=400, detail="Usage: /exec <artifact_id_or_name> [key=value ...]")
    artifact_ref = argv[0]
    inputs = _parse_key_values(argv[1:]) if len(argv) > 1 else {}
    payload = await _execute_artifact(
        session_id=session_id,
        artifact_ref=artifact_ref,
        version=None,
        inputs=inputs,
        workflow_name=workflow_name,
    )
    execution = payload["execution"]
    artifact = payload["artifact"]
    return f"Executed {artifact.get('name') or artifact.get('artifact_id')}. Status: {execution.get('status')}.", payload


async def _handle_sweep_command(
    session_id: str,
    argv: list[str],
    workflow_name: str,
) -> tuple[str, Any]:
    workflow = ResearchWorkflow(session_id=session_id, workflow_name=workflow_name)
    _hydrate_workflow_state(workflow)
    artifact_ref = argv[0] if argv else None
    artifact = (
        _resolve_artifact(workflow.artifacts, artifact_ref)
        if artifact_ref else workflow._context.memory.get("current_artifact")
    )
    if artifact is None:
        raise HTTPException(status_code=400, detail="No artifact. Use /sweep <artifact_id_or_name>.")
    plan = workflow._context.memory.get("current_plan")
    sweep = getattr(plan, "parameter_sweep", {}) if plan else {}
    if not sweep:
        raise HTTPException(status_code=400, detail="No parameter sweep is available for this session.")

    rows: list[dict[str, Any]] = []
    for parameter, values in sweep.items():
        for value in values:
            inputs = {parameter: value}
            result = await workflow.adapter.run(artifact, inputs)
            workflow.results.save(result)
            await safe_backend_action(workflow.backend, "persist_result", artifact, result, inputs)
            await safe_backend_action(
                workflow.backend,
                "record_execution",
                artifact,
                result,
                inputs,
                result.outputs,
            )
            rows.append({"inputs": inputs, "run": result.model_dump()})
    _save_workflow_meta(workflow, load_session_meta(session_id).get("goal", ""), workflow._context.memory.get("target", {}))
    return f"Sweep completed with {len(rows)} runs.", {"artifact": artifact.model_dump(), "runs": rows}


async def _handle_optimize_command(
    session_id: str,
    argv: list[str],
    body: CommandRunRequest,
) -> tuple[str, Any]:
    workflow = ResearchWorkflow(
        provider_name=body.provider,
        token=body.token,
        model=body.model,
        base_url=validate_provider_base_url(body.base_url) if body.base_url else None,
        session_id=session_id,
        workflow_name=body.workflow_name,
    )
    _hydrate_workflow_state(workflow)
    artifact = workflow._context.memory.get("current_artifact")
    if artifact is None:
        artifacts = workflow.artifacts.list_all()
        artifact = artifacts[-1] if artifacts else None
    if artifact is None:
        raise HTTPException(status_code=400, detail="No artifact. Run a goal before /optimize.")

    target = body.target or workflow._context.memory.get("target", {}) or {}
    generations = _positive_int(argv[0], default=10) if argv else 10
    population = _positive_int(argv[1], default=8) if len(argv) > 1 else 8

    from arc.packages import resolve_role

    workflow._context.memory["adapter"] = workflow.adapter
    OptimizerAgent = resolve_role("optimizer", workflow)
    result = await OptimizerAgent(context=workflow._context).run(
        artifact,
        target=target,
        max_generations=generations,
        pop_size=population,
    )
    _save_workflow_meta(
        workflow,
        load_session_meta(session_id).get("goal", ""),
        target,
    )
    text = f"Optimization completed: {result.get('generations_run', generations)} generations."
    if result.get("best_inputs"):
        text += f" Best inputs: {json.dumps(result['best_inputs'], sort_keys=True, default=str)}."
    return text, {"artifact": artifact.model_dump(), "optimization": result}


async def _handle_services_command(argv: list[str]) -> tuple[str, Any]:
    from arc.services import _SERVICES, _log_path, sim2l_available, start, status_all, stop

    sub = argv[0].lower() if argv else "status"
    target = argv[1].lower() if len(argv) > 1 else None
    if sub not in {"status", "start", "stop", "restart", "logs"}:
        raise HTTPException(status_code=400, detail="Usage: /services [status|start|stop|restart|logs] [name]")

    if sub == "status":
        payload = {"available": sim2l_available(), "services": status_all()}
        label = "installed" if payload["available"] else "not installed"
        return f"sim2l is {label}. Service status loaded.", payload

    if sub == "logs":
        log_target = target or "catalog"
        if log_target not in _SERVICES:
            raise HTTPException(status_code=400, detail=f"Unknown service: {log_target}")
        log = _log_path(log_target)
        lines = log.read_text(encoding="utf-8").splitlines()[-80:] if log.exists() else []
        return f"Loaded {len(lines)} log lines for {log_target}.", {
            "service": log_target,
            "path": str(log),
            "lines": lines,
        }

    if not sim2l_available():
        raise HTTPException(status_code=400, detail="sim2l package is not installed")

    names = [target] if target else [
        name for name, cfg in _SERVICES.items() if not cfg.get("optional")
    ]
    for name in names:
        if name not in _SERVICES:
            raise HTTPException(status_code=400, detail=f"Unknown service: {name}")

    loop = asyncio.get_running_loop()
    reports: list[dict[str, Any]] = []
    for name in names:
        if sub == "start":
            ok, message = await loop.run_in_executor(None, start, name)
        elif sub == "stop":
            ok, message = await loop.run_in_executor(None, stop, name)
        else:
            await loop.run_in_executor(None, stop, name)
            ok, message = await loop.run_in_executor(None, start, name)
        reports.append({"service": name, "ok": ok, "message": message})
    return f"{sub.title()} requested for {len(reports)} service(s).", {"reports": reports, "services": status_all()}


def _handle_strategy_command(session_id: str, argv: list[str]) -> tuple[str, Any]:
    if not argv:
        payload = loop_actions.list_strategies_endpoint(session_id=session_id)
        return "Loaded research-loop strategies.", payload

    role = argv[0].lower()
    if len(argv) == 1:
        payload = loop_actions.list_strategies_endpoint(session_id=session_id)
        matches = [item for item in payload["roles"] if item["role"] == role]
        if not matches:
            raise HTTPException(status_code=404, detail=f"unknown role: {role}")
        return f"Loaded strategies for {role}.", {"session_id": session_id, "role": matches[0]}

    impl = " ".join(argv[1:]).lower()
    if impl in {"reset", "clear"}:
        payload = loop_actions.clear_strategy_endpoint(role=role, session_id=session_id)
        return f"Cleared strategy override for {role}.", payload

    payload = loop_actions.set_strategy_endpoint(
        role=role,
        body=loop_actions.StrategyOverrideRequest(impl=impl),
        session_id=session_id,
    )
    return f"Set {role} strategy to {impl}.", payload


def _handle_recipe_command(session_id: str, argv: list[str]) -> tuple[str, Any]:
    if not argv or argv[0].lower() == "list":
        payload = loop_actions.list_recipes_endpoint(session_id=session_id)
        return "Loaded recipes.", payload

    sub = argv[0].lower()
    rest = argv[1:]
    if sub == "show":
        if not rest:
            raise HTTPException(status_code=400, detail="Usage: /recipe show <name>")
        payload = loop_actions.show_recipe_endpoint(rest[0], session_id=session_id)
        return f"Loaded recipe {payload['name']}.", payload
    if sub == "apply":
        if not rest:
            raise HTTPException(status_code=400, detail="Usage: /recipe apply <name> [--force]")
        force = "--force" in rest
        name = next((item for item in rest if not item.startswith("--")), None)
        if not name:
            raise HTTPException(status_code=400, detail="Usage: /recipe apply <name> [--force]")
        payload = loop_actions.apply_recipe_endpoint(
            name,
            loop_actions.RecipeApplyRequest(force=force),
            session_id=session_id,
        )
        return f"Applied recipe {payload['applied']}.", payload
    if sub == "save":
        if not rest:
            raise HTTPException(status_code=400, detail="Usage: /recipe save <name> [description] [--force]")
        force = "--force" in rest
        parts = [item for item in rest if not item.startswith("--")]
        payload = loop_actions.save_recipe_endpoint(
            loop_actions.RecipeSaveRequest(
                name=parts[0],
                description=" ".join(parts[1:]),
                force=force,
            ),
            session_id=session_id,
        )
        return f"Saved recipe {payload['saved']}.", payload
    if sub in {"delete", "remove", "rm"}:
        if not rest:
            raise HTTPException(status_code=400, detail="Usage: /recipe delete <name>")
        payload = loop_actions.delete_recipe_endpoint(rest[0], session_id=session_id)
        return f"Deleted recipe {payload['deleted']}.", payload
    if sub == "clear":
        payload = loop_actions.clear_active_recipe_endpoint(session_id=session_id)
        return "Cleared active recipe.", payload

    payload = loop_actions.apply_recipe_endpoint(
        argv[0],
        loop_actions.RecipeApplyRequest(force="--force" in argv),
        session_id=session_id,
    )
    return f"Applied recipe {payload['applied']}.", payload


def _handle_clusters_command(session_id: str, argv: list[str]) -> tuple[str, Any]:
    if not argv:
        payload = loop_actions.list_clusters_endpoint(session_id=session_id)
        return f"Loaded {len(payload['clusters'])} failure cluster(s).", payload
    payload = loop_actions.show_cluster_endpoint(argv[0], session_id=session_id)
    return f"Loaded cluster {payload.get('signature', argv[0])}.", payload


def _handle_skills_command(session_id: str, argv: list[str]) -> tuple[str, Any]:
    if not argv or argv[0].lower() == "list":
        payload = loop_actions.list_skills_endpoint(session_id=session_id)
        return f"Loaded {len(payload['skills'])} learned skill(s).", payload

    sub = argv[0].lower()
    rest = argv[1:]
    if sub == "show":
        if not rest:
            raise HTTPException(status_code=400, detail="Usage: /skills show <name>")
        payload = loop_actions.show_skill_endpoint(rest[0], session_id=session_id)
        return f"Loaded skill {payload['name']}.", payload
    if sub in {"delete", "remove", "rm"}:
        if not rest:
            raise HTTPException(status_code=400, detail="Usage: /skills delete <name>")
        payload = loop_actions.delete_skill_endpoint(rest[0], session_id=session_id)
        return f"Deleted skill {payload['deleted']}.", payload
    if sub == "export":
        force = "--force" in rest
        target = next((item for item in rest if not item.startswith("--")), None)
        payload = loop_actions.export_skills_endpoint(
            loop_actions.SkillTransferRequest(target=target, force=force),
            session_id=session_id,
        )
        return "Exported learned skills.", payload
    if sub == "import":
        force = "--force" in rest
        target = next((item for item in rest if not item.startswith("--")), None)
        payload = loop_actions.import_skills_endpoint(
            loop_actions.SkillTransferRequest(target=target, force=force),
            session_id=session_id,
        )
        return "Imported learned skills.", payload

    payload = loop_actions.show_skill_endpoint(argv[0], session_id=session_id)
    return f"Loaded skill {payload['name']}.", payload


def _handle_packages_command(session_id: str) -> tuple[str, Any]:
    workflow = ResearchWorkflow(session_id=session_id)
    _hydrate_workflow_state(workflow)
    packages = workflow.registry.list_packages()
    meta = load_session_meta(session_id) or {}
    payload = {
        "session_id": session_id,
        "packages": packages,
        "state": meta.get("packages", {}),
    }
    return f"Loaded {len(packages)} package(s).", payload


def _handle_package_toggle_command(session_id: str, argv: list[str]) -> tuple[str, Any]:
    if len(argv) != 2 or argv[0].lower() not in {"enable", "disable"}:
        raise HTTPException(status_code=400, detail="Usage: /package enable|disable <name>")
    action = argv[0].lower()
    package_name = argv[1]
    workflow = ResearchWorkflow(session_id=session_id)
    package_names = set(workflow.registry.list_packages())
    if package_name not in package_names:
        raise HTTPException(status_code=404, detail=f"Package '{package_name}' is not loaded.")

    meta = load_session_meta(session_id) or {}
    package_state = dict(meta.get("packages") or {})
    enabled = set(package_state.get("enabled") or [])
    disabled = set(package_state.get("disabled") or [])
    if action == "enable":
        enabled.add(package_name)
        disabled.discard(package_name)
    else:
        disabled.add(package_name)
        enabled.discard(package_name)
    package_state["enabled"] = sorted(enabled)
    package_state["disabled"] = sorted(disabled)
    _save_session_fields(session_id, packages=package_state)
    return f"{action.title()}d package {package_name} for this session.", {"packages": package_state}


def _handle_coder_command(session_id: str, argv: list[str]) -> tuple[str, Any]:
    aliases = {
        "builder": "default",
        "builtin": "default",
        "built-in": "default",
        "sim2l": "default",
        "codex": "codex",
        "arc-codex": "codex",
        "claude": "claude_code",
        "claude-code": "claude_code",
        "claude_code": "claude_code",
    }
    if not argv:
        return _handle_strategy_command(session_id, ["builder"])
    impl = aliases.get(argv[0].lower(), argv[0].lower())
    return _handle_strategy_command(session_id, ["builder", impl])


def _assistant_summary(result: dict[str, Any] | None) -> str:
    if not result:
        return "No result was produced."
    execution = result.get("execution") or {}
    review = result.get("review") or {}
    artifact = result.get("artifact") or {}
    parts = [
        f"Run completed with status {result.get('status', 'unknown')}.",
    ]
    if artifact.get("name"):
        parts.append(f"Artifact: {artifact['name']}.")
    if execution.get("status"):
        parts.append(f"Execution: {execution['status']}.")
    if review.get("summary"):
        parts.append(str(review["summary"]))
    return " ".join(parts)


def _thread_path(session_id: str) -> Path:
    paths = session_paths(session_id)
    return Path(paths["db"]).parent / "ui_thread.json"


def _load_thread(session_id: str) -> list[dict[str, Any]]:
    path = _thread_path(session_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


def _save_thread(session_id: str, messages: list[dict[str, Any]]) -> None:
    path = _thread_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: serialize to a temp file in the same dir, then os.replace
    # (atomic on POSIX + Windows). A concurrent reader (e.g. a second browser
    # tab polling the session) never observes a half-written/truncated file.
    # NB: this prevents *corruption*, not a last-writer-wins race on the whole
    # list — acceptable for the single-user dev UI; see ui_todo.md.
    payload = json.dumps(messages[-200:], indent=2, default=str)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _append_thread(
    session_id: str,
    role: str,
    content: str,
    *,
    title: str | None = None,
    payload: dict[str, Any] | None = None,
    suggestions: list[dict[str, Any]] | None = None,
) -> None:
    messages = _load_thread(session_id)
    messages.append({
        "id": f"msg-{int(time.time() * 1000)}-{len(messages)}",
        "role": role,
        "title": title or role.title(),
        "content": content,
        "payload": payload or {},
        # Contextual next-step chips the UI renders under this message — set
        # only when the loop signals it wants user input (e.g. a run that
        # wasn't approved). Empty for a fresh/goal-first turn.
        "suggestions": suggestions or [],
        "ts": time.time(),
    })
    _save_thread(session_id, messages)


def _suggestions_for(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Derive next-step suggestion chips from a run result.

    The loop tells us what it needs: an *unapproved* review means the goal
    isn't met yet, so offer to keep going; suggested ``next_parameters`` mean
    the loop has a concrete next experiment to try. An approved/empty result
    yields no chips (nothing for the user to decide). Each chip is
    ``{label, command, prompt_steps?}`` — ``command`` is the slash command the
    UI runs; ``prompt_steps`` asks the UI to collect an iteration count first.
    """
    if not result:
        return []
    review = result.get("review") or {}
    # An explicit approval (or a final/approved status) means we're done —
    # don't nudge the user to keep iterating.
    if review.get("approved") is True or result.get("status") in ("approved", "final"):
        return []
    chips: list[dict[str, Any]] = [
        {"label": "Continue (1 more)", "command": "/continue"},
        {"label": "Iterate…", "command": "/iterate", "prompt_steps": True},
    ]
    next_params = result.get("next_parameters") or review.get("next_parameters")
    if isinstance(next_params, dict) and next_params:
        chips.insert(0, {
            "label": "Run suggested next parameters",
            "command": "/continue",
            "note": ", ".join(f"{k}={v}" for k, v in list(next_params.items())[:4]),
        })
    return chips


def _thread_for_session(session_id: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages = _load_thread(session_id)
    if messages:
        return messages
    meta = load_session_meta(session_id) or {}
    derived: list[dict[str, Any]] = []
    if meta.get("goal"):
        derived.append({
            "id": "derived-goal",
            "role": "user",
            "title": "Goal",
            "content": meta["goal"],
            "payload": {},
            "ts": 0,
        })

    run_history = meta.get("run_history") or []
    if isinstance(run_history, list) and run_history:
        recent_history = run_history[-50:]
        offset = len(run_history) - len(recent_history)
        for idx, entry in enumerate(recent_history):
            if not isinstance(entry, dict):
                continue
            iteration = offset + idx + 1
            derived.append({
                "id": f"derived-run-history-{iteration}",
                "role": "assistant",
                "title": f"Iteration {iteration}",
                "content": _run_history_message(entry, iteration),
                "payload": entry,
                "ts": iteration,
            })
        if derived:
            return derived

    for idx, result in enumerate(results[-5:]):
        derived.append({
            "id": f"derived-result-{idx}",
            "role": "assistant",
            "title": "Result",
            "content": f"Run {result.get('run_id', idx)} finished with status {result.get('status', 'unknown')}.",
            "payload": result,
            "ts": idx + 1,
        })
    return derived


def _run_history_message(entry: dict[str, Any], index: int) -> str:
    status = (
        entry.get("status")
        or _nested_value(entry, "execution", "status")
        or _nested_value(entry, "result", "status")
        or "recorded"
    )
    artifact_name = (
        _nested_value(entry, "artifact", "name")
        or entry.get("artifact_name")
        or entry.get("artifact_id")
    )
    review = entry.get("review") or {}

    lines = [f"Iteration {index} finished with status {status}."]
    if artifact_name:
        lines.append(f"Artifact: {artifact_name}.")
    if isinstance(review, dict) and review.get("summary"):
        lines.append(str(review["summary"]))
    return "\n".join(lines)


def _nested_value(source: dict[str, Any], parent: str, key: str) -> Any:
    value = source.get(parent)
    if isinstance(value, dict):
        return value.get(key)
    return None


app = create_app()
