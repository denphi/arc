"""HTTP routes for the pluggable research-loop surface.

Mirrors the chat-side slash commands one-for-one so a web UI (or any
external client) can drive the same operations the REPL exposes:

  * ``/strategies``        — list / set / clear per-role strategy overrides
  * ``/recipes``           — list / show / apply / save / delete + clear
  * ``/clusters``          — read failure-cluster snapshots
  * ``/skills``            — list / show / delete / export / import

Every mutating route persists through :mod:`arc.api.session_state` so
changes survive an HTTP server restart. Reads are tolerant of missing
state (a brand-new session returns sensible empty payloads).

The router shares the bearer-token gate (``require_api_token``) with
``arc.api.routes`` for parity.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from arc.api.security import require_api_token
from arc.api.session_state import load_state, save_state
from arc.session import session_paths, validate_session_id


router = APIRouter(dependencies=[Depends(require_api_token)])


# ── Helpers ────────────────────────────────────────────────────────────


def _session(session_id: str) -> str:
    """Validate the session id or raise HTTP 400."""
    try:
        return validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _session_skills_dir(session_id: str, *, create: bool = False) -> Path | None:
    """Resolve ``<session>/skills/learned/``, optionally creating it."""
    try:
        paths = session_paths(session_id)
    except Exception:
        return None
    base = Path(paths["artifacts"]).parent
    target = base / "skills" / "learned"
    if create:
        target.mkdir(parents=True, exist_ok=True)
        return target
    if target.exists():
        return target
    return None


def _default_export_dir() -> Path:
    base = Path(os.environ.get("SIM2L_HOME", str(Path.home() / ".sim2l" / "code")))
    return base / "shared" / "skills"


def _resolve_skill_transfer_dir(target: str | None) -> Path:
    """Resolve an import/export directory under the shared skills root.

    The HTTP API accepts caller-controlled JSON, so an arbitrary absolute
    ``target`` would turn skill import/export into a filesystem read/write
    primitive. Treat relative targets as subdirectories of the shared skills
    root and reject anything that resolves outside it.
    """
    base = _default_export_dir().resolve()
    if not target:
        return base
    raw = Path(target)
    candidate = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
    if candidate == base or base in candidate.parents:
        return candidate
    raise HTTPException(
        status_code=400,
        detail=f"skill transfer target must be inside {base}",
    )


# ── /strategies ────────────────────────────────────────────────────────


class StrategyOverrideRequest(BaseModel):
    """Body for ``POST /strategies/{role}``."""
    impl: str = Field(..., min_length=1)


@router.get("/strategies")
async def list_strategies_endpoint(session_id: str) -> dict[str, Any]:
    """Return per-role catalogue + the active strategy for each role.

    The active strategy reflects the four-layer precedence
    (runtime override > env > arc.toml > default), so the response
    is exactly what a client needs to render the same picture
    ``/strategy`` does in the chat.
    """
    session_id = _session(session_id)
    from arc.core.config import load_arc_toml
    from arc.core.strategies import (
        default_strategy,
        known_roles,
        list_strategies,
        resolve_strategy_name,
    )

    state = load_state(session_id)
    overrides = state.get("strategy_overrides") or {}
    try:
        _path, config = load_arc_toml()
    except Exception:
        config = {}

    roles: list[dict[str, Any]] = []
    for role in known_roles():
        active = resolve_strategy_name(role, overrides=overrides, config=config)
        roles.append({
            "role": role,
            "default": default_strategy(role),
            "active": active,
            "session_override": overrides.get(role),
            "options": [
                {"name": s.name, "description": s.description}
                for s in list_strategies(role)
            ],
        })
    return {"session_id": session_id, "roles": roles}


@router.post("/strategies/{role}")
async def set_strategy_endpoint(
    role: str,
    body: StrategyOverrideRequest,
    session_id: str,
) -> dict[str, Any]:
    """Set the session override for ``role`` to ``body.impl``."""
    session_id = _session(session_id)
    from arc.core.strategies import known_roles, list_strategies

    if role not in known_roles():
        raise HTTPException(status_code=404, detail=f"unknown role: {role}")
    available = {s.name for s in list_strategies(role)}
    if body.impl not in available:
        raise HTTPException(
            status_code=400,
            detail=f"unknown strategy {body.impl!r} for {role}; "
                   f"available: {sorted(available)}",
        )

    state = load_state(session_id)
    overrides: dict[str, str] = dict(state.get("strategy_overrides") or {})
    overrides[role] = body.impl
    state["strategy_overrides"] = overrides
    save_state(session_id, state)
    return {"role": role, "impl": body.impl, "strategy_overrides": overrides}


@router.delete("/strategies/{role}")
async def clear_strategy_endpoint(role: str, session_id: str) -> dict[str, Any]:
    """Clear the session override for ``role`` (the resolver falls back
    to env / arc.toml / default)."""
    session_id = _session(session_id)
    from arc.core.strategies import known_roles

    if role not in known_roles():
        raise HTTPException(status_code=404, detail=f"unknown role: {role}")

    state = load_state(session_id)
    overrides: dict[str, str] = dict(state.get("strategy_overrides") or {})
    cleared = overrides.pop(role, None)
    state["strategy_overrides"] = overrides
    save_state(session_id, state)
    return {"role": role, "cleared": cleared, "strategy_overrides": overrides}


# ── /recipes ───────────────────────────────────────────────────────────


class RecipeApplyRequest(BaseModel):
    force: bool = False


class RecipeSaveRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    force: bool = False


def _serialise_recipe(recipe) -> dict[str, Any]:
    return {
        "name": recipe.name,
        "description": recipe.description,
        "source": recipe.source,
        "tags": list(recipe.tags),
        "strategies": dict(recipe.strategies),
    }


@router.get("/recipes")
async def list_recipes_endpoint(session_id: str) -> dict[str, Any]:
    """Discoverable recipes (bundled + user) plus the session's active recipe."""
    session_id = _session(session_id)
    from arc.core.recipes import list_recipes

    state = load_state(session_id)
    return {
        "session_id": session_id,
        "active_recipe": state.get("active_recipe"),
        "recipes": [_serialise_recipe(r) for r in list_recipes()],
    }


@router.get("/recipes/{name}")
async def show_recipe_endpoint(name: str, session_id: str) -> dict[str, Any]:
    session_id = _session(session_id)
    from arc.core.recipes import get_recipe

    recipe = get_recipe(name)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"unknown recipe: {name}")
    return _serialise_recipe(recipe)


@router.post("/recipes/{name}/apply")
async def apply_recipe_endpoint(
    name: str,
    body: RecipeApplyRequest,
    session_id: str,
) -> dict[str, Any]:
    """Merge a recipe's strategy mappings into the session overrides."""
    session_id = _session(session_id)
    from arc.core.recipes import apply_recipe, get_recipe, validate_recipe

    recipe = get_recipe(name)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"unknown recipe: {name}")
    errors = validate_recipe(recipe)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    state = load_state(session_id)
    # ``apply_recipe`` mutates the dict in place; that's exactly what
    # we want — load → mutate → persist.
    result = apply_recipe(recipe, state, overwrite_manual=body.force)
    state["active_recipe"] = recipe.name
    save_state(session_id, state)
    return {
        "applied": recipe.name,
        "overrides_set": result.overrides_set,
        "overrides_skipped": result.overrides_skipped,
        "strategy_overrides": state.get("strategy_overrides", {}),
        "active_recipe": state.get("active_recipe"),
    }


@router.post("/recipes")
async def save_recipe_endpoint(
    body: RecipeSaveRequest,
    session_id: str,
) -> dict[str, Any]:
    """Persist the session's current ``strategy_overrides`` as a user recipe."""
    session_id = _session(session_id)
    from arc.core.recipes import RecipeSaveError, save_recipe

    state = load_state(session_id)
    overrides = state.get("strategy_overrides") or {}
    if not overrides:
        raise HTTPException(
            status_code=400,
            detail="no strategy_overrides to save; set at least one via "
                   "POST /strategies/{role} first",
        )
    try:
        path = save_recipe(
            body.name, overrides,
            description=body.description,
            force=body.force,
        )
    except RecipeSaveError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "saved": path.stem,
        "path": str(path),
        "strategies": overrides,
    }


@router.delete("/recipes/{name}")
async def delete_recipe_endpoint(name: str, session_id: str) -> dict[str, Any]:
    """Remove a user recipe. Bundled recipes are read-only."""
    session_id = _session(session_id)
    from arc.core.recipes import RecipeDeleteError, clear_recipe, delete_recipe

    try:
        path = delete_recipe(name)
    except RecipeDeleteError as exc:
        msg = str(exc).lower()
        if "no user recipe" in msg or "could not be normalised" in msg \
                or "non-empty" in msg:
            raise HTTPException(status_code=404, detail=str(exc))
        # "Refusing to delete bundled recipe" — 403 Forbidden.
        raise HTTPException(status_code=403, detail=str(exc))

    state = load_state(session_id)
    cleared: dict[str, str] = {}
    if state.get("active_recipe") == path.stem:
        cleared = clear_recipe(state)
        state.pop("active_recipe", None)
    save_state(session_id, state)
    return {
        "deleted": path.stem,
        "path": str(path),
        "cleared_active": bool(cleared),
        "cleared_overrides": cleared,
    }


@router.post("/recipes/clear")
async def clear_active_recipe_endpoint(session_id: str) -> dict[str, Any]:
    """Drop the keys the last applied recipe set."""
    session_id = _session(session_id)
    from arc.core.recipes import clear_recipe

    state = load_state(session_id)
    cleared = clear_recipe(state)
    state.pop("active_recipe", None)
    save_state(session_id, state)
    return {"cleared": cleared}


# ── /clusters ──────────────────────────────────────────────────────────


@router.get("/clusters")
async def list_clusters_endpoint(session_id: str) -> dict[str, Any]:
    """Return the failure clusters the reflector last stamped.

    These come from the per-session ``run_history`` flow rather than
    from the API state file — they're computed at the end of every
    iteration by the failure-clustering reflector. The chat layer
    saves them on the workflow's memory dict via the existing chat
    session persistence; here we read from ``session.json`` to keep
    parity with what the chat sees.
    """
    session_id = _session(session_id)
    from arc.session import load_session_meta

    meta = load_session_meta(session_id) or {}
    raw = meta.get("failure_clusters")
    clusters = raw if isinstance(raw, list) else []
    return {"session_id": session_id, "clusters": clusters}


@router.get("/clusters/{signature}")
async def show_cluster_endpoint(signature: str, session_id: str) -> dict[str, Any]:
    session_id = _session(session_id)
    from arc.session import load_session_meta

    meta = load_session_meta(session_id) or {}
    clusters = meta.get("failure_clusters")
    if not isinstance(clusters, list):
        raise HTTPException(status_code=404, detail="no clusters recorded")
    for cluster in clusters:
        if isinstance(cluster, dict) and cluster.get("signature") == signature:
            return cluster
    # Prefix match — same UX as the slash command.
    prefix_matches = [
        c for c in clusters
        if isinstance(c, dict) and str(c.get("signature", "")).startswith(signature)
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"{signature!r} matches multiple signatures: "
                   f"{[c.get('signature') for c in prefix_matches]}",
        )
    raise HTTPException(
        status_code=404, detail=f"no cluster with signature {signature!r}",
    )


# ── /skills ────────────────────────────────────────────────────────────


def _read_skill_files(target: Path) -> list[dict[str, Any]]:
    """Helper: enumerate every ``*.md`` in ``target`` with its H1."""
    import re
    h1 = re.compile(r"^#\s+([^\n]+)", re.MULTILINE)
    items: list[dict[str, Any]] = []
    for path in sorted(target.glob("*.md")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = h1.search(body)
        items.append({
            "name": path.stem,
            "filename": path.name,
            "h1": match.group(1).strip() if match else "",
        })
    return items


@router.get("/skills")
async def list_skills_endpoint(session_id: str) -> dict[str, Any]:
    session_id = _session(session_id)
    target = _session_skills_dir(session_id)
    if target is None:
        return {"session_id": session_id, "dir": None, "skills": []}
    return {
        "session_id": session_id,
        "dir": str(target),
        "skills": _read_skill_files(target),
    }


@router.get("/skills/{name}")
async def show_skill_endpoint(name: str, session_id: str) -> dict[str, Any]:
    session_id = _session(session_id)
    target = _session_skills_dir(session_id)
    if target is None:
        raise HTTPException(status_code=404, detail="no skills dir for session")
    candidates = sorted(target.glob("*.md"))
    exact = next((p for p in candidates if p.stem == name), None)
    if exact is None:
        prefix = [p for p in candidates if p.stem.startswith(name)]
        if len(prefix) == 1:
            exact = prefix[0]
        elif len(prefix) > 1:
            raise HTTPException(
                status_code=409,
                detail=f"{name!r} matches multiple skills: "
                       f"{[p.stem for p in prefix]}",
            )
        else:
            raise HTTPException(status_code=404, detail=f"no skill: {name}")
    body = exact.read_text(encoding="utf-8")
    return {"name": exact.stem, "filename": exact.name, "body": body}


@router.delete("/skills/{name}")
async def delete_skill_endpoint(name: str, session_id: str) -> dict[str, Any]:
    session_id = _session(session_id)
    target = _session_skills_dir(session_id)
    if target is None:
        raise HTTPException(status_code=404, detail="no skills dir for session")
    candidates = sorted(target.glob("*.md"))
    match = next((p for p in candidates if p.stem == name), None)
    if match is None:
        prefix = [p for p in candidates if p.stem.startswith(name)]
        if len(prefix) == 1:
            match = prefix[0]
        elif len(prefix) > 1:
            raise HTTPException(
                status_code=409,
                detail=f"{name!r} matches multiple skills: "
                       f"{[p.stem for p in prefix]}",
            )
        else:
            raise HTTPException(status_code=404, detail=f"no skill: {name}")
    try:
        match.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"deleted": match.stem, "filename": match.name}


class SkillTransferRequest(BaseModel):
    target: str | None = None      # explicit dir; None → default
    force: bool = False


def _transfer_skills(
    *,
    src: Path,
    dst: Path,
    force: bool,
) -> dict[str, Any]:
    """Shared logic for export + import: src → dst with conflict handling."""
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    skipped_same: list[str] = []
    skipped_conflict: list[str] = []
    overwritten: list[str] = []
    for src_path in sorted(src.glob("*.md")):
        try:
            body = src_path.read_text(encoding="utf-8")
        except OSError:
            continue
        dst_path = dst / src_path.name
        if dst_path.exists():
            try:
                existing = dst_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if existing == body:
                skipped_same.append(src_path.stem)
                continue
            if not force:
                skipped_conflict.append(src_path.stem)
                continue
            overwritten.append(src_path.stem)
        try:
            dst_path.write_text(body, encoding="utf-8")
        except OSError:
            continue
        if src_path.stem not in overwritten:
            copied.append(src_path.stem)
    return {
        "copied": copied,
        "overwritten": overwritten,
        "skipped_same": skipped_same,
        "skipped_conflict": skipped_conflict,
    }


@router.post("/skills/export")
async def export_skills_endpoint(
    body: SkillTransferRequest,
    session_id: str,
) -> dict[str, Any]:
    session_id = _session(session_id)
    src = _session_skills_dir(session_id)
    if src is None:
        raise HTTPException(status_code=404, detail="no skills dir for session")
    if not list(src.glob("*.md")):
        return {"src": str(src), "dst": None, "copied": [],
                "overwritten": [], "skipped_same": [], "skipped_conflict": []}
    dst = _resolve_skill_transfer_dir(body.target)
    report = _transfer_skills(src=src, dst=dst, force=body.force)
    return {"src": str(src), "dst": str(dst), **report}


@router.post("/skills/import")
async def import_skills_endpoint(
    body: SkillTransferRequest,
    session_id: str,
) -> dict[str, Any]:
    session_id = _session(session_id)
    src = _resolve_skill_transfer_dir(body.target)
    if not src.exists():
        raise HTTPException(
            status_code=404, detail=f"source does not exist: {src}",
        )
    if not list(src.glob("*.md")):
        return {"src": str(src), "dst": None, "copied": [],
                "overwritten": [], "skipped_same": [], "skipped_conflict": []}
    dst = _session_skills_dir(session_id, create=True)
    if dst is None:
        raise HTTPException(
            status_code=500, detail="could not resolve session skills dir",
        )
    report = _transfer_skills(src=src, dst=dst, force=body.force)
    return {"src": str(src), "dst": str(dst), **report}
