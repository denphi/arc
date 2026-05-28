"""Chat-level tools (Phase 4).

A ``Tool`` is the unit of intent in the v2 router. The LLM emits a tool
call describing which intent the user expressed (start a goal, refine,
ask a question, set a target, …); the chat dispatches to the matching
``Tool.run`` handler.

Tools are pluggable like commands. Each module here declares ``TOOLS``
which the registry aggregates.

Important: tools live BEHIND the ``ARC_CHAT_V2`` feature flag. Nothing
here is wired into the default chat loop yet.
"""

from arc.chat.tools.registry import (
    Tool,
    ToolBudgetExceeded,
    ToolRegistry,
    ToolValidationError,
    build_tool_registry,
)

__all__ = [
    "Tool",
    "ToolBudgetExceeded",
    "ToolRegistry",
    "ToolValidationError",
    "build_tool_registry",
]
