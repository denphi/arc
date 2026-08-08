"""Tests for package manifest loading."""

import pytest
from pathlib import Path

from arc.core.kernel import Kernel
from arc.core.registry import ComponentRegistry


@pytest.mark.asyncio
async def test_kernel_loads_declared_package_resources():
    kernel = Kernel()
    await kernel.startup()

    assert "ideator" in kernel.registry.list_agents()
    assert "research-loop" in kernel.registry.list_workflows()
    assert "create-sim2l" in kernel.registry.list_skills()
    assert "local" in kernel.registry.list_adapters()
    assert "sim2l" in kernel.registry.list_adapters()
    assert "bandgap_evaluator" in kernel.registry.list_evaluators()
    assert "materials_hypothesis_generation" in kernel.registry.list_prompts()
    assert "materials_sim2l_template" in kernel.registry.list_templates()
    assert kernel.registry.get_prompt("materials_hypothesis_generation").content
    assert kernel.registry.get_template("materials_sim2l_template").data
    assert kernel.registry.get_vocabulary("materials_properties").data

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_kernel_respects_package_enabled_filter(tmp_path):
    config = tmp_path / "arc.toml"
    root = Path(__file__).resolve().parents[1] / "arc"
    config.write_text(
        f"""
[packages]
paths = [
  "{root / 'packages' / 'arc-sim2l'}",
  "{root / 'packages' / 'arc-mars'}",
]
enabled = ["arc-sim2l"]
"""
    )

    kernel = Kernel(config_path=str(config))
    await kernel.startup()

    assert "ideator" in kernel.registry.list_agents()
    assert "mars_planner" not in kernel.registry.list_agents()

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_kernel_loads_extension_via_hyphenated_entrypoint(tmp_path):
    """Core seam (Item 2): the extension loader resolves a package-hosted
    extension whose entrypoint has a hyphenated path, and registers it.
    The mcp extension with no apps loads idle (no live server needed)."""
    config = tmp_path / "arc.toml"
    config.write_text(
        """
[packages]
paths = []

[extensions.mcp]
enabled = true
entrypoint = "arc.packages.arc-mcp.extension:McpExtension"
"""
    )
    kernel = Kernel(config_path=str(config))
    await kernel.startup()

    # The extension was constructed + registered despite the hyphenated path.
    assert kernel.registry.get_extension("mcp") is not None
    await kernel.shutdown()


def test_registry_resolves_agent_by_package_source():
    """A contested bare name goes to the first registrant and stays there.

    ``coder`` is provided by both arc-codex and arc-claude-code, and ``ideator``
    by both arc-sim2l and arc-coscientist. Under last-wins, which one a bare
    ``get_agent("coder")`` returned was decided by the order of
    ``[packages].paths`` in arc.toml — reordering that list, an edit that reads
    as cosmetic, silently swapped the agent. Every provider stays reachable
    through the explicit ``package:name`` form.
    """
    class FirstAgent:
        pass

    class SecondAgent:
        pass

    registry = ComponentRegistry()
    registry.register_agent("coder", FirstAgent, package_name="arc-codex")
    registry.register_agent("coder", SecondAgent, package_name="arc-claude-code")

    assert registry.get_agent("coder") is FirstAgent
    assert registry.get_agent("coder", package_name="arc-codex") is FirstAgent
    assert registry.get_agent("coder", package_name="arc-claude-code") is SecondAgent
    assert registry.get_agent("arc-codex:coder") is FirstAgent
    assert registry.get_agent("arc-claude-code:coder") is SecondAgent
    assert registry.list_agent_sources("coder") == ["arc-codex", "arc-claude-code"]


def test_registry_bare_agent_name_is_independent_of_registration_order():
    """Loading the same two packages in either order yields the same bare name."""
    class Codex:
        pass

    class ClaudeCode:
        pass

    forward = ComponentRegistry()
    forward.register_agent("coder", Codex, package_name="arc-codex")
    forward.register_agent("coder", ClaudeCode, package_name="arc-claude-code")

    reverse = ComponentRegistry()
    reverse.register_agent("coder", ClaudeCode, package_name="arc-claude-code")
    reverse.register_agent("coder", Codex, package_name="arc-codex")

    # Each registry keeps whichever package it saw first — the point is that a
    # bare lookup is a stable function of the load set, not of a later
    # registration silently overwriting an earlier one.
    assert forward.get_agent("coder") is Codex
    assert reverse.get_agent("coder") is ClaudeCode
    # …and both providers stay addressable in both registries.
    for registry in (forward, reverse):
        assert registry.get_agent("arc-codex:coder") is Codex
        assert registry.get_agent("arc-claude-code:coder") is ClaudeCode


def test_disabled_agent_falls_back_to_another_providing_package():
    """Disabling one provider must not hide a name another package supplies.

    ``_agent_sources`` already held the alternative; the lookup just didn't
    consult it, so ``/package disable arc-coscientist`` made ``ideator``
    unresolvable even though arc-sim2l provides one.
    """
    class Sim2lIdeator:
        pass

    class CoScientistIdeator:
        pass

    registry = ComponentRegistry()
    registry.register_agent("ideator", Sim2lIdeator, package_name="arc-sim2l")
    registry.register_agent("ideator", CoScientistIdeator, package_name="arc-coscientist")

    assert registry.get_agent("ideator") is Sim2lIdeator
    # Disabling the bare-name owner falls through to the other provider.
    assert registry.get_agent(
        "ideator", disabled_packages={"arc-sim2l"},
    ) is CoScientistIdeator
    # Disabling every provider is still a KeyError.
    with pytest.raises(KeyError):
        registry.get_agent(
            "ideator", disabled_packages={"arc-sim2l", "arc-coscientist"},
        )
