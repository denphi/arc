import importlib.util
from types import SimpleNamespace
from pathlib import Path

import pytest


CHAT_PATH = Path(__file__).resolve().parents[1] / "examples" / "chat.py"
spec = importlib.util.spec_from_file_location("arc_chat_helpers", CHAT_PATH)
chat = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(chat)


def test_parse_target_infers_bandgap_from_target_ev_shorthand():
    target = chat._parse_target("optimize silicon geometry to target 1.1 eV")

    assert target == {"bandgap_ev": 1.1}


def test_parse_target_keeps_explicit_key_value_priority():
    target = chat._parse_target("target bandgap_ev=1.12")

    assert target == {"bandgap_ev": 1.12}


def test_parse_target_ignores_bare_zero_diagnostic_output_name():
    target = chat._parse_target(
        "target_bandgap_compliance is always 0, I want to address that"
    )

    assert target == {}


def test_parse_refinement_target_preserves_target_for_diagnostic_refinement():
    target = chat._parse_refinement_target(
        "target_bandgap_compliance is always 0, I want to address that"
    )

    assert target == {}


def test_parse_refinement_target_accepts_explicit_target_change():
    target = chat._parse_refinement_target("change the target to 1.05 eV")

    assert target == {"bandgap_ev": 1.05}


def test_refinement_needs_artifact_rebuild_for_broken_metric():
    assert chat._refinement_needs_artifact_rebuild(
        "target_bandgap_compliance is always 0, I want to address that"
    )


def test_refinement_does_not_rebuild_for_parameter_request():
    assert not chat._refinement_needs_artifact_rebuild(
        "try a smaller quantum well thickness"
    )


def test_normalize_chat_command_accepts_backslash_help():
    assert chat._normalize_chat_command("\\help") == "/help"


def test_related_refinement_matches_artifact_output_key():
    artifact = SimpleNamespace(
        name="bandgap_model",
        description="Silicon bandgap model",
        metadata={
            "sim2l_inputs": {"quantum_well_thickness": {}},
            "sim2l_outputs": {"target_bandgap_compliance": {}, "bandgap_ev": {}},
        },
    )
    ctx = SimpleNamespace(memory={"target": {"bandgap_ev": 1.1}, "schema_registry": {}})

    assert chat._is_related_refinement(
        "target_bandgap_compliance is always 0, I want to address that",
        "optimize silicon bandgap",
        artifact,
        ctx,
    )


def test_unrelated_help_question_is_not_a_refinement():
    artifact = SimpleNamespace(
        name="bandgap_model",
        description="Silicon bandgap model",
        metadata={
            "sim2l_inputs": {"quantum_well_thickness": {}},
            "sim2l_outputs": {"bandgap_ev": {}},
        },
    )
    ctx = SimpleNamespace(memory={"target": {"bandgap_ev": 1.1}, "schema_registry": {}})

    assert not chat._is_related_refinement(
        "help me write an email",
        "optimize silicon bandgap",
        artifact,
        ctx,
    )


@pytest.mark.asyncio
async def test_register_artifact_with_sim2l_uses_adapter_hook():
    artifact = SimpleNamespace(name="test_artifact", version="0.1.0")

    class Adapter:
        async def register_artifact(self, registered_artifact):
            assert registered_artifact is artifact
            return {
                "registered": True,
                "sim_name": "test_artifact",
                "sim_version": "0.1.0",
                "catalog_persisted": False,
            }

    workflow = SimpleNamespace(adapter=Adapter(), session_id="session-test")

    result = await chat._register_artifact_with_sim2l(workflow, artifact)

    assert result == {
        "registered": True,
        "sim_name": "test_artifact",
        "sim_version": "0.1.0",
        "catalog_persisted": False,
    }
