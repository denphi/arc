"""Tests for the curator's capability-summary generation (A).

The curator now writes an LLM-authored (or, with no provider, a deterministic)
capability summary into ``artifact.description`` + ``metadata['capability']``
so the catalog entry is searchable and the reuse scorer has rich text.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arc.contracts.agent import AgentContext  # noqa: E402
from arc.packages.arc_sim2l_agents import curator as curator_mod  # noqa: E402
from arc.schemas.artifact import ArtifactRecord  # noqa: E402

CuratorAgent = curator_mod.CuratorAgent


def _artifact(tmp_path: Path) -> ArtifactRecord:
    (tmp_path / "workflow.py").write_text(
        "def simulate(temperature=300.0):\n"
        "    return {'band_gap_ev': 1.1}\n"
    )
    return ArtifactRecord(
        artifact_id="aid",
        name="bandgap_predictor",
        version="0.1.0",
        state="draft",
        path=str(tmp_path),
        metadata={"sim2l_inputs": {"temperature": {"type": "Number"}}},
    )


@pytest.mark.asyncio
async def test_capability_fallback_without_provider(tmp_path):
    ctx = AgentContext(session_id="t")
    artifact = _artifact(tmp_path)
    result = await CuratorAgent(context=ctx).run(artifact)

    cap = result.metadata.get("capability")
    assert isinstance(cap, dict)
    assert cap["summary"]
    assert isinstance(cap["domain_tags"], list) and cap["domain_tags"]
    # Description is populated from the summary so the catalog can search it.
    assert result.description


@pytest.mark.asyncio
async def test_capability_uses_provider_when_present(tmp_path):
    class FakeProvider:
        async def complete(self, prompt):
            return (
                '{"summary": "Predicts the electronic band gap from temperature.",'
                ' "capabilities": ["band gap prediction"],'
                ' "when_to_use": "When you need a band gap estimate.",'
                ' "domain_tags": ["bandgap", "semiconductor"]}'
            )

    ctx = AgentContext(session_id="t", memory={"provider": FakeProvider()})
    artifact = _artifact(tmp_path)
    result = await CuratorAgent(context=ctx).run(artifact)

    cap = result.metadata["capability"]
    assert "band gap" in cap["summary"].lower()
    assert "bandgap" in cap["domain_tags"]
    assert result.description == cap["summary"]


@pytest.mark.asyncio
async def test_capability_survives_bad_provider_json(tmp_path):
    class BadProvider:
        async def complete(self, prompt):
            return "not json at all"

    ctx = AgentContext(session_id="t", memory={"provider": BadProvider()})
    artifact = _artifact(tmp_path)
    result = await CuratorAgent(context=ctx).run(artifact)
    # Falls back to deterministic capability rather than crashing.
    assert result.metadata["capability"]["summary"]


def test_fallback_capability_shape():
    cap = curator_mod._fallback_capability("my_sim", ["x"], ["y", "z"])
    assert set(cap) == {"summary", "capabilities", "when_to_use", "domain_tags"}
    assert cap["domain_tags"]


def test_coerce_capability_fills_gaps():
    cap = curator_mod._coerce_capability({"summary": ""}, "n", ["a"], ["b"])
    # Empty summary is replaced by the fallback's.
    assert cap["summary"]
    assert cap["capabilities"]
