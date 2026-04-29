"""Tests for package manifest loading."""

import pytest

from arc.core.kernel import Kernel


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
