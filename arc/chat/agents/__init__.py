"""Declarative agent definitions.

OpenHarness-style: an agent is a Pydantic record describing system
prompt, allowed tools, model, max_turns. The chat layer dispatches to
agents by name; the existing Python agent classes in
``arc.packages.*`` remain as the fallback when a YAML definition is
absent or the LLM is unavailable.

Public surface:
  * ``AgentDefinition`` — Pydantic model
  * ``load_agent_definition(path)`` — read + validate a YAML file
  * ``discover_agent_definitions()`` — build a name→def map

This module is intentionally inert by default — nothing here is wired
into the chat path until ``ARC_CHAT_V2=1`` is set in Phase 4.
"""

from arc.chat.agents.definition import (
    AgentDefinition,
    AgentRequirementError,
    discover_agent_definitions,
    load_agent_definition,
    resolve_agent,
)

__all__ = [
    "AgentDefinition",
    "AgentRequirementError",
    "discover_agent_definitions",
    "load_agent_definition",
    "resolve_agent",
]
