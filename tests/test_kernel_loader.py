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


def test_registry_resolves_agent_by_package_source():
    class FirstAgent:
        pass

    class SecondAgent:
        pass

    registry = ComponentRegistry()
    registry.register_agent("coder", FirstAgent, package_name="arc-codex")
    registry.register_agent("coder", SecondAgent, package_name="arc-claude-code")

    assert registry.get_agent("coder") is SecondAgent
    assert registry.get_agent("coder", package_name="arc-codex") is FirstAgent
    assert registry.get_agent("arc-codex:coder") is FirstAgent
    assert registry.list_agent_sources("coder") == ["arc-codex", "arc-claude-code"]
