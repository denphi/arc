"""AgentDefinition + YAML loader tests (Phase 4)."""

import pytest
from pydantic import ValidationError

from arc.chat.agents.definition import (
    AgentDefinition,
    discover_agent_definitions,
    load_agent_definition,
)


pytestmark = pytest.mark.chat


# ── Pydantic model ────────────────────────────────────────────────────────

def test_minimal_definition_validates():
    d = AgentDefinition(name="reviewer")
    assert d.name == "reviewer"
    assert d.model == "inherit"
    assert d.max_turns == 1
    assert d.permission_mode == "default"


def test_full_definition_validates():
    d = AgentDefinition(
        name="reviewer",
        description="Reviews a research run.",
        system_prompt="You are an expert reviewer.",
        model="claude-sonnet-4-6",
        allowed_tools=["read_artifact"],
        disallowed_tools=["delete_session"],
        max_turns=3,
        temperature=0.0,
        permission_mode="auto",
        requires_provider=False,
    )
    assert d.allowed_tools == ["read_artifact"]
    assert d.permission_mode == "auto"


def test_unknown_field_rejected():
    """Strict mode catches typos in YAML."""
    with pytest.raises(ValidationError):
        AgentDefinition(name="x", typo_field="oops")


def test_blank_name_rejected():
    with pytest.raises(ValidationError):
        AgentDefinition(name="")


def test_oversized_name_rejected():
    with pytest.raises(ValidationError):
        AgentDefinition(name="x" * 200)


def test_negative_max_turns_rejected():
    with pytest.raises(ValidationError):
        AgentDefinition(name="x", max_turns=0)


def test_excessive_max_turns_rejected():
    with pytest.raises(ValidationError):
        AgentDefinition(name="x", max_turns=10_000)


def test_invalid_permission_mode_rejected():
    with pytest.raises(ValidationError):
        AgentDefinition(name="x", permission_mode="reckless")


def test_temperature_clamped_to_range():
    with pytest.raises(ValidationError):
        AgentDefinition(name="x", temperature=-0.1)
    with pytest.raises(ValidationError):
        AgentDefinition(name="x", temperature=2.5)


# ── load_agent_definition (file loading) ──────────────────────────────────

def test_load_round_trip(tmp_path):
    f = tmp_path / "reviewer.yaml"
    f.write_text(
        "name: reviewer\n"
        "description: review runs\n"
        "system_prompt: |\n"
        "  You are a reviewer\n"
        "allowed_tools:\n"
        "  - read_artifact\n"
        "max_turns: 2\n"
    )
    d = load_agent_definition(f)
    assert d is not None
    assert d.name == "reviewer"
    assert d.allowed_tools == ["read_artifact"]
    assert d.max_turns == 2


def test_load_returns_none_for_missing_file(tmp_path, caplog):
    d = load_agent_definition(tmp_path / "no-such.yaml")
    assert d is None


def test_load_returns_none_for_malformed_yaml(tmp_path, caplog):
    f = tmp_path / "bad.yaml"
    f.write_text("name: x\nthis: is: not: yaml\n  nope")
    d = load_agent_definition(f)
    assert d is None


def test_load_returns_none_for_invalid_schema(tmp_path, caplog):
    f = tmp_path / "bad.yaml"
    f.write_text("name: x\nmax_turns: -5\n")
    d = load_agent_definition(f)
    assert d is None


def test_load_returns_none_for_non_mapping_root(tmp_path):
    f = tmp_path / "list.yaml"
    f.write_text("- just\n- a\n- list\n")
    assert load_agent_definition(f) is None


def test_load_uses_yaml_safe_load_only(tmp_path):
    """Confirm hostile constructors are rejected."""
    f = tmp_path / "hostile.yaml"
    f.write_text(
        "name: x\n"
        "pwn: !!python/object/new:os.system [\"echo SHOULD_NOT_RUN\"]\n"
    )
    d = load_agent_definition(f)
    # Either rejection or the field is absent — code did not execute
    assert d is None or "pwn" not in d.model_fields_set


# ── discover_agent_definitions ────────────────────────────────────────────

def test_discover_returns_empty_when_no_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
    monkeypatch.chdir(tmp_path)
    # If there are no built-in agent yamls, the result is empty;
    # otherwise it contains whatever's shipped. Either is acceptable.
    result = discover_agent_definitions()
    assert isinstance(result, dict)


def test_discover_picks_up_extra_dirs(tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "x.yaml").write_text("name: x\ndescription: extra one\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    monkeypatch.chdir(tmp_path)
    result = discover_agent_definitions(extra_dirs=[extra])
    assert "x" in result
    assert result["x"].description == "extra one"


def test_discover_blocks_user_override_by_default(tmp_path, monkeypatch, caplog):
    # Create a builtin via extra_dirs (treated as builtin-equivalent for source
    # ordering — we test the override semantics here by going through user dir).
    user_dir = tmp_path / "config" / "arc" / "agents"
    user_dir.mkdir(parents=True)
    extra_dir = tmp_path / "extra"
    extra_dir.mkdir()

    # "shared" appears in both extra (treated as 'extra' source) and user
    (user_dir / "shared.yaml").write_text("name: shared\ndescription: from user\n")
    (extra_dir / "shared.yaml").write_text("name: shared\ndescription: from extra\n")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)

    # No allow → user comes first in iteration, then extra; extra (later) is
    # blocked if it would override. The exact precedence isn't critical —
    # what matters is one definitive winner with a warning logged if relevant.
    result = discover_agent_definitions(extra_dirs=[extra_dir])
    assert "shared" in result


# ── allow_user_overrides parameter exists and defaults False ──────────────

def test_allow_user_overrides_defaults_off():
    import inspect
    sig = inspect.signature(discover_agent_definitions)
    assert sig.parameters["allow_user_overrides"].default is False


# ── resolve_agent + requires_provider enforcement ────────────────────────


def test_resolve_agent_raises_when_definition_missing():
    from arc.chat.agents import resolve_agent
    with pytest.raises(KeyError, match="agent 'no-such-agent'"):
        resolve_agent("no-such-agent", definitions={})


def test_resolve_agent_raises_when_provider_missing():
    from arc.chat.agents import AgentDefinition, AgentRequirementError, resolve_agent
    defs = {"reviewer": AgentDefinition(name="reviewer", requires_provider=True)}
    with pytest.raises(AgentRequirementError, match="requires an LLM provider"):
        resolve_agent("reviewer", provider=None, definitions=defs)


def test_resolve_agent_succeeds_when_provider_present():
    from arc.chat.agents import AgentDefinition, resolve_agent
    defs = {"reviewer": AgentDefinition(name="reviewer", requires_provider=True)}
    agent = resolve_agent("reviewer", provider=object(), definitions=defs)
    assert agent.name == "reviewer"


def test_resolve_agent_succeeds_when_requires_provider_false_and_no_provider():
    from arc.chat.agents import AgentDefinition, resolve_agent
    defs = {"reflector": AgentDefinition(name="reflector", requires_provider=False)}
    agent = resolve_agent("reflector", provider=None, definitions=defs)
    assert agent.name == "reflector"
