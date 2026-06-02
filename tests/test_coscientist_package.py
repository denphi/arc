from __future__ import annotations

import asyncio
from pathlib import Path

from arc.packages.arc_coscientist.agents.ideator import CoScientistIdeatorAgent
from arc.packages.arc_coscientist.agents.supervisor import CoScientistSupervisorAgent

from arc.contracts.agent import AgentContext
from arc.core.loader import load_package
from arc.core.registry import ComponentRegistry
from arc.core.strategies import resolve_role
from arc.runtime.audit import assemble_report
from arc.schemas.research import ResearchGoal


def _package_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "arc" / "packages" / "arc-coscientist"


def test_coscientist_package_manifest_loads_without_upstream_imports():
    registry = ComponentRegistry()
    load_package(_package_dir(), registry)

    assert "arc-coscientist" in registry.list_packages()
    assert registry.get_agent("ideator", package_name="arc-coscientist") is CoScientistIdeatorAgent
    assert registry.get_agent("coscientist_supervisor") is CoScientistSupervisorAgent
    assert "coscientist-hypothesis-loop" in registry.list_workflows()
    assert "run-coscientist" in registry.list_skills()
    assert "coscientist_hypotheses" in registry.list_report_sections()


def test_coscientist_ideator_strategy_resolves_from_catalogue():
    registry = ComponentRegistry()
    load_package(_package_dir(), registry)

    cls = resolve_role("ideator", overrides={"ideator": "coscientist"}, config={})

    assert cls.__name__ == "CoScientistIdeatorAgent"


def test_coscientist_ideator_records_hypothesis_pool():
    ctx = AgentContext(session_id="test-coscientist")
    agent = CoScientistIdeatorAgent(ctx)

    proposal = asyncio.run(agent.run(ResearchGoal(
        goal="Identify catalysts for ammonia synthesis",
        target={"yield": 1.0},
    )))

    assert proposal.hypothesis
    pool = ctx.memory["coscientist_hypothesis_pool"]
    assert pool["source"] == "arc-coscientist"
    assert len(pool["candidates"]) >= 3
    assert ctx.memory["ideator_candidates"]


def test_coscientist_prompt_includes_arc_retry_context():
    ctx = AgentContext(
        session_id="test-coscientist",
        memory={
            "run_history": [
                {
                    "inputs": {"input_parameter": 1.0},
                    "outputs": {"result": 2.0},
                    "status": "completed",
                },
            ],
            "required_outputs": ["bandgap_ev"],
            "retry_context": [
                {
                    "reason": "schema_mismatch",
                    "required_outputs": ["bandgap_ev"],
                    "actual_outputs": {"result": 2.0},
                },
            ],
        },
    )
    agent = CoScientistIdeatorAgent(ctx)

    prompt = agent._prompt(ResearchGoal(
        goal="maximize the band gap of silicon",
        target={"bandgap_ev": 1.1},
    ))

    assert "ARC context from prior attempts" in prompt
    assert "required_outputs=['bandgap_ev']" in prompt
    assert "actual_outputs=['result']" in prompt
    assert "Required output keys for the next artifact: ['bandgap_ev']" in prompt


def test_coscientist_report_section_contributes_from_memory():
    registry = ComponentRegistry()
    load_package(_package_dir(), registry)
    ctx = AgentContext(
        session_id="test-coscientist",
        memory={
            "component_registry": registry,
            "coscientist_hypothesis_pool": {
                "source": "arc-coscientist",
                "candidates": [{"hypothesis": "H1"}],
            },
        },
    )

    report = assemble_report(ctx)

    assert "coscientist_hypotheses" in report["sections"]
    section = report["sections"]["coscientist_hypotheses"]
    assert section["hypothesis_pool"]["source"] == "arc-coscientist"


def test_coscientist_supervisor_probe_keeps_clone_read_only():
    ctx = AgentContext(session_id="test-coscientist")
    agent = CoScientistSupervisorAgent(ctx)

    result = asyncio.run(agent.run({"goal": "Test a research direction"}))

    assert result["status"] == "ready"
    assert result["execute"] is False
    assert Path(result["repo"]).name == "Co-Scientist"
