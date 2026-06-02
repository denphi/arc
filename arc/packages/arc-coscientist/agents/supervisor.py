"""Lazy ARC wrapper around the cloned Co-Scientist Supervisor."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from typing import Any

from arc.contracts.agent import AgentContract
from arc.schemas.research import ResearchGoal

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "bridge.py"
_BRIDGE_SPEC = importlib.util.spec_from_file_location("arc_coscientist_bridge", _BRIDGE_PATH)
if _BRIDGE_SPEC is None or _BRIDGE_SPEC.loader is None:  # pragma: no cover - impossible in normal installs
    raise ImportError(f"Cannot load Co-Scientist bridge from {_BRIDGE_PATH}")
_BRIDGE = importlib.util.module_from_spec(_BRIDGE_SPEC)
_BRIDGE_SPEC.loader.exec_module(_BRIDGE)

CoScientistUnavailable = _BRIDGE.CoScientistUnavailable
data_dir = _BRIDGE.data_dir
ensure_importable = _BRIDGE.ensure_importable
repo_root = _BRIDGE.repo_root


class CoScientistSupervisorAgent(AgentContract):
    name = "coscientist_supervisor"
    description = "Runs the upstream Co-Scientist durable tournament workflow on demand."

    async def run(self, input_data: Any) -> dict[str, Any]:
        payload = _payload(input_data)
        goal = payload["goal"]
        execute = bool(payload.get("execute", False))
        config = {**(self.context.config or {}), **(payload.get("config") or {})}

        try:
            root = repo_root(config)
        except CoScientistUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}

        if not execute:
            return {
                "status": "ready",
                "execute": False,
                "goal": goal,
                "repo": str(root),
                "message": (
                    "Set execute=true to run the upstream Co-Scientist supervisor. "
                    "The cloned repository is left unmodified."
                ),
            }

        try:
            ensure_importable(config)
            from co_scientist.agents.supervisor import Supervisor
            from co_scientist.config import has_llm_key, load_config, provider_key_env
            from co_scientist.storage import db as db_mod
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": f"Could not import upstream Co-Scientist runtime: {exc}",
                "repo": str(root),
            }

        cfg_path = payload.get("config_file")
        cfg = load_config(Path(cfg_path).expanduser() if cfg_path else None)
        cfg.storage.data_dir = str(data_dir(config))
        if payload.get("budget_usd") is not None:
            cfg.run.budget_usd = float(payload["budget_usd"])
        if payload.get("wall_clock_seconds") is not None:
            cfg.run.wall_clock_seconds = int(payload["wall_clock_seconds"])
        if payload.get("concurrency") is not None:
            cfg.run.concurrency = int(payload["concurrency"])

        if not has_llm_key(cfg):
            return {
                "status": "blocked",
                "reason": f"{provider_key_env(cfg)} is not configured",
                "repo": str(root),
                "data_dir": str(cfg.data_dir),
            }

        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        await db_mod.init_db(cfg)
        supervisor = Supervisor(cfg)
        session_id = await supervisor.run_session(
            goal,
            preferences_text=payload.get("preferences"),
            n_initial=int(payload.get("n_initial", 3)),
            wall_clock_seconds=payload.get("wall_clock_seconds"),
            resume_session_id=payload.get("resume_session_id"),
        )

        overview = cfg.session_artifact_dir(session_id) / "final" / "overview.md"
        record = {
            "status": "completed",
            "session_id": session_id,
            "goal": goal,
            "repo": str(root),
            "data_dir": str(cfg.data_dir),
            "overview_path": str(overview),
            "overview_exists": overview.exists(),
        }
        self.context.memory.setdefault("coscientist_sessions", []).append(record)
        return record


def _payload(input_data: Any) -> dict[str, Any]:
    if isinstance(input_data, ResearchGoal):
        return {"goal": input_data.goal}
    if isinstance(input_data, str):
        return {"goal": input_data}
    if isinstance(input_data, dict):
        if "goal" in input_data and isinstance(input_data["goal"], dict):
            goal = ResearchGoal(**input_data["goal"])
            out = dict(input_data)
            out["goal"] = goal.goal
            return out
        if "goal" in input_data:
            return dict(input_data)
        return {"goal": json.dumps(input_data, default=str)}
    return {"goal": str(input_data)}
