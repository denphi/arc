"""ChatState — the single state object for the chat REPL.

``ChatState`` wraps a reference to ``workflow._context.memory`` and
exposes the chat-relevant fields (primary goal, current artifact,
refinements, target, permission mode, cost budget, etc.) as named
attributes with sensible setters.

Reading and writing through ``ChatState`` is the convention for the
chat layer; the underlying ``memory`` dict is still the durable store
(shared with the agents). Persistence to ``session.json`` is handled
elsewhere by ``arc.chat.loop._save_session`` — the in-memory state and
the on-disk record stay in sync as long as callers use the
``ChatState`` properties for writes.

The class is intentionally NOT a Pydantic model. It wraps a live
workflow object whose memory dict is shared with the agent runtime,
and Pydantic's deep-copy semantics would defeat that aliasing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


PermissionMode = Literal["default", "auto", "plan"]


@dataclass
class ChatState:
    """Live state for one chat session.

    Holds a *reference* to ``workflow._context.memory`` so writes through
    this object are visible to agents that still read ``ctx.memory``
    directly.
    """

    workflow: Any                      # arc.orchestrator.workflow.ResearchWorkflow
    permission_mode: PermissionMode = "default"
    # Counters bumped by the v2 router on every LLM round-trip.
    router_calls: int = 0
    # Maximum v2-router LLM calls per session before refusing.
    router_call_budget: int = 200
    max_iterations: int = 20
    sim2l_status: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Reconcile permission_mode with the global plan-mode flag.
        # Caller intent wins when it's explicitly "plan"; otherwise the
        # global flag (set earlier by the CLI ``--plan`` switch) is
        # respected. This avoids the bug where ``--plan`` is silently
        # disabled when ``chat_loop`` constructs a default ``ChatState``.
        from arc.chat.plan_mode import is_plan_mode, set_plan_mode
        if self.permission_mode == "plan":
            set_plan_mode(True)
        elif is_plan_mode():
            # Global flag is already on (set by --plan); honour it.
            self.permission_mode = "plan"
        else:
            set_plan_mode(False)

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Update permission mode and the global plan-mode flag."""
        from arc.chat.plan_mode import set_plan_mode
        self.permission_mode = mode
        set_plan_mode(mode == "plan")

    # ── Memory accessors ────────────────────────────────────────────────

    @property
    def memory(self) -> dict:
        return self.workflow._context.memory

    @property
    def session_id(self) -> str:
        return self.workflow.session_id

    @property
    def iteration(self) -> int:
        return self.workflow._context.iteration

    # primary_goal -----------------------------------------------------

    @property
    def primary_goal(self) -> Optional[str]:
        return self.memory.get("primary_goal")

    @primary_goal.setter
    def primary_goal(self, value: Optional[str]) -> None:
        if value is None:
            self.memory.pop("primary_goal", None)
        else:
            self.memory["primary_goal"] = value

    # current_artifact -------------------------------------------------

    @property
    def current_artifact(self):
        return self.memory.get("current_artifact")

    @current_artifact.setter
    def current_artifact(self, value) -> None:
        if value is None:
            self.memory.pop("current_artifact", None)
        else:
            self.memory["current_artifact"] = value

    # refinements ------------------------------------------------------

    @property
    def refinements(self) -> list[str]:
        return self.memory.get("refinements", [])

    def add_refinement(self, text: str) -> None:
        refs = self.memory.setdefault("refinements", [])
        refs.append(text)

    def clear_refinements(self) -> None:
        self.memory.pop("refinements", None)

    # target -----------------------------------------------------------

    @property
    def target(self) -> dict:
        return self.memory.get("target", {})

    @target.setter
    def target(self, value: dict) -> None:
        if value:
            self.memory["target"] = value
        else:
            self.memory.pop("target", None)

    # high-level operations -------------------------------------------

    def reset_for_new_goal(self, new_goal: str) -> None:
        """Apply the side effects of the ``/run`` command."""
        m = self.memory
        for key in ("current_artifact", "current_plan", "run_history",
                    "next_parameters", "refinements"):
            m.pop(key, None)
        m["primary_goal"] = new_goal

    def has_active_goal(self) -> bool:
        return self.primary_goal is not None and self.current_artifact is not None

    # persistence ------------------------------------------------------

    def persist(self) -> None:
        """Write the session to disk via the existing ``_save_session``
        helper. Kept as a thin delegate so call sites don't reach back
        into ``arc.chat.loop`` for the private function.
        """
        from arc.chat.loop import _save_session
        _save_session(self.workflow, self.primary_goal)
