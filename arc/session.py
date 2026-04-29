"""Session management for ARC chat.

Each session lives under ~/.sim2l/<session_id>/ and persists:
  - artifacts/          ArtifactRegistry root
  - runs/               ResultsStore root
  - memory/             ProvenanceLog root
  - session.json        goal, iteration, current_artifact_id, run_history, target

Sessions are identified by a short human-readable ID like 'bandgap-abc1'.
"""

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


def sim2l_home() -> Path:
    """Return the ARC session root, honoring SIM2L_HOME at call time."""
    return Path(os.environ.get("SIM2L_HOME", Path.home() / ".sim2l" / "code"))


def validate_session_id(session_id: str) -> str:
    """Return a safe session id or raise ValueError.

    Session ids are path components under SIM2L_HOME, so they must not contain
    separators, traversal markers, or shell/control characters.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if session_id in {".", ".."} or "/" in session_id or "\\" in session_id:
        raise ValueError(f"Unsafe session_id: {session_id}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", session_id):
        raise ValueError(f"Unsafe session_id: {session_id}")
    return session_id


def _session_dir(session_id: str) -> Path:
    root = sim2l_home().resolve()
    path = (root / validate_session_id(session_id)).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"Unsafe session_id: {session_id}")
    return path


def new_session_id(prefix: str = "") -> str:
    short = uuid.uuid4().hex[:6]
    if prefix:
        slug = prefix.lower().replace(" ", "-")[:20].strip("-")
        return f"{slug}-{short}"
    return f"session-{short}"


def list_sessions() -> list[dict[str, Any]]:
    root = sim2l_home()
    if not root.exists():
        return []
    sessions = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        # Only treat directories that contain session.json as ARC sessions.
        if not (d / "session.json").exists():
            continue
        meta = _load_meta(d.name)
        sessions.append({
            "session_id": d.name,
            "goal": meta.get("goal", ""),
            "iteration": meta.get("iteration", 0),
            "created": meta.get("created", ""),
        })
    return sessions


def _meta_path(session_id: str) -> Path:
    return _session_dir(session_id) / "session.json"


def _load_meta(session_id: str) -> dict[str, Any]:
    p = _meta_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def save_session_meta(
    session_id: str,
    goal: str | None,
    iteration: int,
    current_artifact_id: str | None,
    current_artifact_name: str | None,
    run_history: list,
    target: dict,
    next_parameters: dict,
    created: str | None = None,
    schema_registry: dict | None = None,
    primary_goal: str | None = None,
    refinements: list | None = None,
) -> None:
    p = _meta_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_meta(session_id)
    meta = {
        "session_id": session_id,
        "goal": goal or existing.get("goal", ""),
        "iteration": iteration,
        "current_artifact_id": current_artifact_id,
        "current_artifact_name": current_artifact_name,
        "run_history": run_history,
        "target": target,
        "next_parameters": next_parameters,
        "schema_registry": schema_registry if schema_registry is not None else existing.get("schema_registry", {}),
        "primary_goal": primary_goal if primary_goal is not None else existing.get("primary_goal"),
        "refinements": refinements if refinements is not None else existing.get("refinements", []),
        "created": created or existing.get("created", ""),
    }
    p.write_text(json.dumps(meta, indent=2))


def load_session_meta(session_id: str) -> dict[str, Any]:
    return _load_meta(session_id)


def delete_session(session_id: str) -> bool:
    """Delete a session directory and all its contents. Returns True if deleted."""
    import shutil
    d = _session_dir(session_id)
    if not d.exists() or not (d / "session.json").exists():
        return False
    shutil.rmtree(d)
    return True


def delete_all_sessions() -> list[str]:
    """Delete every session. Returns list of deleted session IDs."""
    deleted = []
    for s in list_sessions():
        if delete_session(s["session_id"]):
            deleted.append(s["session_id"])
    return deleted


def session_paths(session_id: str) -> dict[str, str]:
    base = _session_dir(session_id)
    return {
        "artifacts": str(base / "artifacts"),
        "runs": str(base / "runs"),
        "provenance": str(base / "memory" / "provenance.jsonl"),
        "db": str(base / "arc_sim2l.db"),
    }
