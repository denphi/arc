"""Tool + ToolRegistry — Pydantic-validated routing.

Each tool declares:
  * ``name``         — stable identifier used in tool calls
  * ``description``  — what the tool does (shown to the LLM)
  * ``schema``       — Pydantic model defining the arguments
  * ``side_effects`` — True if the tool writes files / talks to a service.
                       Plan-mode rejects ``side_effects=True`` calls.
  * ``run``          — async handler: ``(state, args) -> Any``

Calls go through ``ToolRegistry.dispatch(state, name, args_dict)``:

  1. Look up the tool by name. Unknown name → ``ToolValidationError``.
  2. Validate ``args_dict`` against ``tool.schema``. Bad args →
     ``ToolValidationError``.
  3. Check ``allowed_tools`` on the calling agent if provided.
  4. Check plan-mode gate (refuses side-effect tools when active).
  5. Check per-state cost budget. Burst over budget → ``ToolBudgetExceeded``.
  6. Invoke ``tool.run`` and return its result.

This module is part of the Phase 4 feature-flagged path. Default chat
flow still uses the CommandRegistry from Phase 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from pydantic import BaseModel, ValidationError


logger = logging.getLogger(__name__)


class ToolValidationError(ValueError):
    """Raised when a tool call's arguments don't validate."""


class ToolBudgetExceeded(RuntimeError):
    """Raised when a session's tool-call budget is exhausted."""


# ── Tool record ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    run: Callable[..., Awaitable[Any]]
    side_effects: bool = False


# ── Registry ────────────────────────────────────────────────────────────


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        key = tool.name.lower()
        if key in self._tools:
            raise ValueError(f"Duplicate tool: {key!r}")
        self._tools[key] = tool

    def register_many(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name.lower())

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ── Dispatch ────────────────────────────────────────────────────────

    async def dispatch(
        self,
        state: Any,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        allowed_tools: list[str] | None = None,
    ) -> Any:
        """Validate args and invoke the tool.

        Raises:
            ToolValidationError: bad name, bad args, denied by allowed_tools
            ToolBudgetExceeded: state.cost_budget_usd exceeded
            PlanModeBlocked: tool has side_effects=True and plan mode is on
        """
        tool = self.get(name)
        if tool is None:
            raise ToolValidationError(f"unknown tool {name!r}")

        if allowed_tools is not None and tool.name not in allowed_tools:
            raise ToolValidationError(
                f"tool {tool.name!r} is not in allowed_tools {allowed_tools!r}"
            )

        try:
            validated = tool.schema(**(args or {}))
        except ValidationError as exc:
            raise ToolValidationError(
                f"invalid args for tool {tool.name!r}: {exc}"
            ) from exc

        # Budget check FIRST — refuse before any side effects or counters.
        # Otherwise an exhausted budget would still tick the counter and
        # could mask why the next call was blocked.
        budget = getattr(state, "cost_budget_usd", None)
        if budget is not None and budget <= 0:
            raise ToolBudgetExceeded(
                f"tool budget exhausted before dispatching {tool.name!r}"
            )

        # Plan-mode gate for side-effect tools.
        if tool.side_effects:
            from arc.chat.plan_mode import check_plan_gate
            check_plan_gate(f"tool {tool.name!r}",
                            hint="set --plan to dry-run, or omit to execute")

        # Counter ticks only when we're actually about to invoke the tool.
        if hasattr(state, "router_calls"):
            state.router_calls += 1

        return await tool.run(state, validated)


def build_tool_registry() -> ToolRegistry:
    """Build the canonical chat tool registry.

    Aggregates the tool modules under ``arc.chat.tools``. New tool
    modules just need to import here and contribute via their
    ``TOOLS`` attribute.
    """
    from arc.chat.tools import routing as _routing
    reg = ToolRegistry()
    reg.register_many(_routing.TOOLS)
    return reg
