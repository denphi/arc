"""Session management for ARC chat.

Each session lives under ``$SIM2L_HOME`` (default ``~/.sim2l/code``) as
``<session_id>/`` and persists:

  - artifacts/          ArtifactRegistry root
  - runs/               ResultsStore root
  - memory/             ProvenanceLog root
  - session.json        goal, iteration, current_artifact_id, run_history, target

Sessions are identified by a slug plus 48 random bits, e.g.
'bandgap-3f9a1c2b7e41'. ``session.json`` updates go through
:func:`update_session_meta`, which holds a per-session file lock.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from arc.utils.io import atomic_write_text


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


_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9-]+")


def new_session_id(prefix: str = "") -> str:
    """Return a fresh, unused session id like ``bandgap-3f9a1c2b7e41``.

    The prefix is slugified to ``[a-z0-9-]`` so any user-supplied prefix
    yields a valid id, and the random part is 48 bits; an id whose
    directory already exists is never returned (so a new session can't
    silently adopt an old one).
    """
    slug = _SLUG_UNSAFE_RE.sub("-", (prefix or "").lower())[:20].strip("-")
    stem = slug or "session"
    root = sim2l_home()
    while True:
        candidate = f"{stem}-{uuid.uuid4().hex[:12]}"
        if not (root / candidate).exists():
            return candidate


@contextmanager
def _meta_lock(session_id: str) -> Iterator[None]:
    """Serialise read-modify-write of one session's ``session.json``.

    Uses an advisory ``flock`` on a sidecar lock file, so concurrent writers
    (UI jobs, API requests, a CLI chat) don't lose each other's updates.
    ``atomic_write_text`` already prevents torn files; this prevents lost
    updates. A no-op where ``fcntl`` is unavailable (Windows).
    """
    lock_path = _session_dir(session_id) / ".session.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    with lock_path.open("a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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


_META_DEFAULTS: dict[str, Any] = {
    "goal": "",
    "iteration": 0,
    "current_artifact_id": None,
    "current_artifact_name": None,
    "run_history": [],
    "target": {},
    "next_parameters": {},
    "schema_registry": {},
    "primary_goal": None,
    "refinements": [],
    "packages": {},
    "agent_overrides": {},
    "created": "",
}


def update_session_meta(session_id: str, **fields: Any) -> dict[str, Any]:
    """Merge ``fields`` into ``session.json`` under the session lock.

    Only known fields are accepted; unspecified ones keep their stored
    value. Returns the written metadata.
    """
    unknown = set(fields) - set(_META_DEFAULTS)
    if unknown:
        raise TypeError(f"Unknown session fields: {sorted(unknown)}")
    p = _meta_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _meta_lock(session_id):
        existing = _load_meta(session_id)
        meta: dict[str, Any] = {"session_id": session_id}
        for key, default in _META_DEFAULTS.items():
            meta[key] = existing.get(key, default)
        meta.update(fields)
        atomic_write_text(p, json.dumps(meta, indent=2))
    return meta


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
    packages: dict | None = None,
    agent_overrides: dict | None = None,
) -> None:
    """Write the full session record (``None`` optionals keep stored values).

    Prefer :func:`update_session_meta` for partial updates.
    """
    fields: dict[str, Any] = {
        "iteration": iteration,
        "current_artifact_id": current_artifact_id,
        "current_artifact_name": current_artifact_name,
        "run_history": run_history,
        "target": target,
        "next_parameters": next_parameters,
    }
    if goal:
        fields["goal"] = goal
    if created:
        fields["created"] = created
    optional = {
        "schema_registry": schema_registry,
        "primary_goal": primary_goal,
        "refinements": refinements,
        "packages": packages,
        "agent_overrides": agent_overrides,
    }
    fields.update({k: v for k, v in optional.items() if v is not None})
    update_session_meta(session_id, **fields)


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
