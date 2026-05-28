"""Reviewer YAML migration proof (Phase 4).

The reviewer is the first agent migrated to the declarative ``AgentDefinition``
format. This test asserts:

  * The YAML loads cleanly via the strict Pydantic schema.
  * It appears in ``discover_agent_definitions()`` under its canonical name.
  * Its allowed_tools list is empty (the reviewer takes no tools today —
    a future migration could add ``read_artifact`` etc.).

When more agents move to YAML, copy this template and add per-agent tests.
"""

from pathlib import Path

import pytest

from arc.chat.agents.definition import (
    AgentDefinition,
    discover_agent_definitions,
    load_agent_definition,
)


pytestmark = pytest.mark.chat


REVIEWER_YAML = Path(__file__).resolve().parents[1] / "arc" / "agents" / "reviewer.yaml"


def test_reviewer_yaml_exists():
    assert REVIEWER_YAML.is_file()


def test_reviewer_yaml_loads():
    d = load_agent_definition(REVIEWER_YAML)
    assert d is not None
    assert isinstance(d, AgentDefinition)
    assert d.name == "reviewer"


def test_reviewer_has_system_prompt():
    d = load_agent_definition(REVIEWER_YAML)
    assert "reviewer" in d.system_prompt.lower()
    assert "approval" in d.system_prompt.lower()


def test_reviewer_takes_no_tools_by_default():
    d = load_agent_definition(REVIEWER_YAML)
    assert d.allowed_tools == []
    assert d.disallowed_tools == []


def test_reviewer_max_turns_is_single_step():
    """The legacy reviewer is invoked once per execution. The YAML must
    keep that invariant — otherwise the genetic optimizer's tight loop
    becomes much more expensive."""
    d = load_agent_definition(REVIEWER_YAML)
    assert d.max_turns == 1


def test_reviewer_requires_provider():
    """The reviewer needs an LLM to write its summary."""
    d = load_agent_definition(REVIEWER_YAML)
    assert d.requires_provider is True


def test_reviewer_discoverable_as_builtin():
    """When agents are discovered, the reviewer must appear from the
    built-in source so it can't be silently shadowed."""
    discovered = discover_agent_definitions()
    assert "reviewer" in discovered
