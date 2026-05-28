"""Persistent chat-session I/O.

Two functions wrap ``arc.session``'s on-disk session_meta API with the
specific set of fields the chat REPL persists.

  * :func:`save_session` — write ``ctx.memory`` + iteration into the
    session.json file. Idempotent; skips for the ``"default"`` session.
  * :func:`restore_session` — load the file (if present) and rehydrate
    ``ctx.memory`` + the current artifact. Returns the saved goal text
    so the caller can re-display it.

Previously these lived in ``arc/chat/loop.py`` as ``_save_session`` /
``_restore_session``. The commands package and the research-pipeline
hooks both import them; hosting them here lets those modules avoid
importing from the giant ``loop.py``.

Two on-disk files
-----------------

Session state is split across two JSON files in the session directory:

  * ``session.json`` — the legacy schema (goal, iteration, run_history,
    target, …). Written by ``arc.session.save_session_meta``.
  * ``session_state.json`` — strategy / recipe choices the user made
    during the session: ``strategy_overrides``, ``active_recipe``,
    ``recipe_applied``, ``recipe_suggested``. Written via
    ``arc.api.session_state.save_state`` so the same file format
    backs the HTTP API.

The split keeps the legacy schema stable while giving the new
configuration keys a home that survives a chat restart. A future
cleanup can fold them together; for now the two functions here are the
single authoritative way to round-trip a chat session.
"""

from __future__ import annotations

from typing import Any

from arc.api.session_state import load_state, save_state
from arc.session import load_session_meta, save_session_meta


# Memory keys that live in ``session_state.json``. The chat reads the
# whole subset on restore and writes it back on save; the API does the
# same. Keeping the list in one place avoids drift between the two
# layers.
_STATE_KEYS: tuple[str, ...] = (
    "strategy_overrides",
    "active_recipe",
    "recipe_applied",
    "recipe_suggested",
)


def save_session(workflow: Any, current_goal: str | None) -> None:
    """Persist session state to ``~/.sim2l/<session_id>/``.

    Writes the legacy fields to ``session.json`` and the strategy /
    recipe configuration to ``session_state.json``. No-op when
    ``workflow.session_id == "default"`` (the sentinel id used by
    stub-mode tests).
    """
    if workflow.session_id == "default":
        return
    ctx = workflow._context
    artifact = ctx.memory.get("current_artifact")
    save_session_meta(
        session_id=workflow.session_id,
        goal=current_goal,
        iteration=ctx.iteration,
        current_artifact_id=artifact.artifact_id if artifact else None,
        current_artifact_name=artifact.name if artifact else None,
        run_history=ctx.memory.get("run_history", []),
        target=ctx.memory.get("target", {}),
        next_parameters=ctx.memory.get("next_parameters", {}),
        schema_registry=ctx.memory.get("schema_registry", {}),
        primary_goal=ctx.memory.get("primary_goal"),
        refinements=ctx.memory.get("refinements", []),
        packages=ctx.memory.get("packages", {}),
        agent_overrides=ctx.memory.get("agent_overrides", {}),
    )

    # Strategy + recipe configuration lands in the sidecar file shared
    # with the HTTP API. Pass the whole subset every time and let
    # save_state drop empty values, so the file stays clean.
    save_state(workflow.session_id, {k: ctx.memory.get(k) for k in _STATE_KEYS})


def restore_session(workflow: Any) -> str | None:
    """Reload session state from disk into ``workflow._context``.

    Returns the saved ``goal`` (or ``None`` when no session.json
    exists). The caller is responsible for re-displaying the resumed
    goal to the user; ``chat_loop`` does this via the resume banner.

    Artifact lookup is best-effort: if the artifact registry doesn't
    have the saved id (e.g. it was deleted out-of-band), the rest of
    the session still loads.

    Strategy / recipe state restores from ``session_state.json``
    independently of ``session.json``; either file missing is fine.
    """
    if workflow.session_id == "default":
        return None
    meta = load_session_meta(workflow.session_id)
    ctx = workflow._context

    # Rehydrate strategy/recipe state first so it's available no matter
    # whether the legacy session.json existed. (A session that *only*
    # touched /strategy without ever running an iteration still has a
    # session_state.json to restore.)
    state = load_state(workflow.session_id) or {}
    for key in _STATE_KEYS:
        if key in state and state[key]:
            ctx.memory[key] = state[key]

    if not meta:
        return None

    ctx.iteration = meta.get("iteration", 0)
    ctx.memory["run_history"] = meta.get("run_history", [])
    ctx.memory["target"] = meta.get("target", {})
    ctx.memory["next_parameters"] = meta.get("next_parameters", {})
    ctx.memory["schema_registry"] = meta.get("schema_registry", {})
    ctx.memory["primary_goal"] = meta.get("primary_goal")
    ctx.memory["refinements"] = meta.get("refinements", [])
    ctx.memory["packages"] = meta.get("packages", {})
    ctx.memory["agent_overrides"] = meta.get("agent_overrides", {})

    artifact_id = meta.get("current_artifact_id")
    if artifact_id:
        try:
            ctx.memory["current_artifact"] = workflow.artifacts.get(artifact_id)
        except Exception:
            # Artifact was deleted or registry is unhappy — best effort.
            pass

    return meta.get("goal")
