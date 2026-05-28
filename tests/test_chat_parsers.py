"""Direct tests for ``arc.chat.parsers``.

The parsers used to live in ``loop.py`` and were tested transitively
via ``test_chat_helpers.py`` / ``test_chat_dispatch.py``. Q10 adds
direct unit tests on the canonical module surface so future refactors
can move the imports without losing test coverage.
"""

from types import SimpleNamespace

import pytest

from arc.chat.parsers import (
    NOISE_WORDS,
    build_refined_goal,
    is_related_refinement,
    normalize_chat_command,
    parse_refinement_target,
    parse_target,
    parse_target_command,
    refinement_needs_artifact_rebuild,
    tokens_for_relevance,
)


pytestmark = pytest.mark.chat


# ── parse_target ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    # Explicit key=value
    ("bandgap_ev=1.1",          {"bandgap_ev": 1.1}),
    ("bandgap = 1.1 eV",        {"bandgap_ev": 1.1}),
    ("target: 1.12 eV",         {"target_ev": 1.12}),
    # Natural language with unit
    ("optimize to 1.1 eV bandgap", {"bandgap_ev": 1.1}),
    # Shorthand: "target X eV" → bandgap
    ("target 1.1 eV",           {"bandgap_ev": 1.1}),
    # Noise words must not become target keys
    ("the value is 5",          {}),
    # No numeric target — nothing to extract
    ("just some prose",         {}),
])
def test_parse_target_table(text, expected):
    assert parse_target(text) == expected


def test_parse_target_priority_explicit_wins_over_natural():
    """If both forms appear, key=value takes priority."""
    out = parse_target("looking at 2.0 eV but really target=1.1")
    assert out == {"target": 1.1}


def test_parse_target_skips_noise_words():
    """``to=5`` shouldn't create a 'to' target key."""
    out = parse_target("to=5")
    assert "to" not in out


def test_noise_words_is_frozen():
    assert isinstance(NOISE_WORDS, frozenset)


# ── parse_refinement_target ──────────────────────────────────────────────


def test_refinement_returns_target_only_on_explicit_change():
    """A bare numeric in a refinement should NOT inject a target."""
    assert parse_refinement_target("smaller thickness please") == {}


def test_refinement_returns_target_for_set_form():
    assert parse_refinement_target("set the target to 1.1 eV") == {"bandgap_ev": 1.1}


def test_refinement_returns_target_for_key_value():
    """Explicit key=value always counts."""
    assert parse_refinement_target("bandgap_ev=1.05") == {"bandgap_ev": 1.05}


def test_refinement_returns_target_for_target_to_form():
    assert parse_refinement_target("target to 1.1 eV") == {"bandgap_ev": 1.1}


# ── parse_target_command ─────────────────────────────────────────────────


def test_target_cmd_show():
    action, target, detail = parse_target_command("/target", {"x": 1.0})
    assert action == "show"
    assert target == {"x": 1.0}


def test_target_cmd_show_with_only_whitespace_arg():
    action, _, _ = parse_target_command("/target   ", {"x": 1.0})
    assert action == "show"


def test_target_cmd_clear_keywords():
    for kw in ("clear", "reset", "none", "off"):
        action, target, _ = parse_target_command(f"/target {kw}", {"x": 1.0})
        assert action == "clear"
        assert target == {}


def test_target_cmd_replace_default():
    action, target, _ = parse_target_command("/target bandgap_ev=1.1", {"x": 1.0})
    assert action == "set"
    assert target == {"bandgap_ev": 1.1}
    assert "x" not in target  # default = replace


def test_target_cmd_merge_with_update_prefix():
    action, target, _ = parse_target_command(
        "/target update bandgap_ev=1.1", {"x": 1.0},
    )
    assert action == "set"
    assert target == {"x": 1.0, "bandgap_ev": 1.1}


def test_target_cmd_set_prefix_treated_as_replace():
    action, target, _ = parse_target_command(
        "/target set bandgap_ev=1.1", {"x": 1.0},
    )
    assert action == "set"
    assert target == {"bandgap_ev": 1.1}


def test_target_cmd_error_when_unparseable():
    action, target, detail = parse_target_command(
        "/target garbage", {"x": 1.0},
    )
    assert action == "error"
    assert target == {"x": 1.0}  # current target preserved
    assert "Could not parse" in detail


# ── refinement_needs_artifact_rebuild ────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    # Logic refinements → rebuild
    ("output is broken",                    True),
    ("the metric is wrong",                 True),
    ("compliance score is always 0",        True),
    ("formula has a bug",                   True),
    # Parameter-only refinements → no rebuild
    ("try smaller thickness",               False),
    ("increase temperature",                False),
    ("set parameter mass to 0.5",           False),
    # Mixed: parameter AND logic words → still a rebuild
    ("the output parameter is wrong",       True),
])
def test_refinement_rebuild_table(text, expected):
    assert refinement_needs_artifact_rebuild(text) is expected


# ── normalize_chat_command ───────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("/help",         "/help"),
    ("\\help",        "/help"),
    ("  /run goal ",  "/run goal"),
    ("\\run a goal",  "/run a goal"),
    ("hello",         "hello"),
    ("",              ""),
])
def test_normalize_chat_command_table(raw, expected):
    assert normalize_chat_command(raw) == expected


# ── tokens_for_relevance ─────────────────────────────────────────────────


def test_tokens_filters_short_words():
    """Tokens under 3 chars are dropped."""
    out = tokens_for_relevance("the a bad an XX bandgap")
    assert "bandgap" in out
    assert "the" not in out   # stop word AND short
    assert "an" not in out


def test_tokens_filters_stop_words():
    out = tokens_for_relevance("explain how this works")
    # 'explain', 'how', 'this' are stop words
    assert out == {"works"}


def test_tokens_lowercases_and_splits():
    out = tokens_for_relevance("Bandgap-Energy,FERMI level")
    assert out == {"bandgap", "energy", "fermi", "level"}


def test_tokens_empty_string():
    assert tokens_for_relevance("") == set()


# ── is_related_refinement ────────────────────────────────────────────────


def _make_artifact(inputs=None, outputs=None, name="art"):
    return SimpleNamespace(
        name=name,
        description="",
        metadata={
            "sim2l_inputs": inputs or {},
            "sim2l_outputs": outputs or {},
        },
    )


def _make_ctx(target=None, schema_registry=None):
    return SimpleNamespace(memory={
        "target": target or {},
        "schema_registry": schema_registry or {},
    })


def test_related_refinement_slash_command_never_refinement():
    art = _make_artifact()
    ctx = _make_ctx()
    assert is_related_refinement("/help", "any goal", art, ctx) is False
    assert is_related_refinement("\\help", "any goal", art, ctx) is False


def test_related_refinement_matches_output_key_exact():
    art = _make_artifact(outputs={"target_bandgap_compliance": {}})
    ctx = _make_ctx()
    assert is_related_refinement(
        "target_bandgap_compliance is always 0",
        "optimize bandgap",
        art, ctx,
    ) is True


def test_related_refinement_unrelated_text_returns_false():
    art = _make_artifact(outputs={"bandgap_ev": {}})
    ctx = _make_ctx()
    assert is_related_refinement(
        "what's the weather in Bordeaux",
        "optimize bandgap",
        art, ctx,
    ) is False


def test_related_refinement_uses_token_overlap():
    art = _make_artifact(outputs={"bandgap_ev": {}})
    ctx = _make_ctx()
    # 'bandgap' appears in both → token overlap with refinement-vocab "smaller"
    assert is_related_refinement(
        "make the bandgap smaller",
        "optimize bandgap",
        art, ctx,
    ) is True


def test_related_refinement_no_artifact_still_works():
    """When artifact is None, the function falls back to goal + target context."""
    ctx = _make_ctx(target={"bandgap_ev": 1.1})
    assert is_related_refinement(
        "bandgap_ev should be smaller",
        "optimize bandgap",
        None, ctx,
    ) is True


# ── build_refined_goal ───────────────────────────────────────────────────


def test_build_refined_goal_no_refinements_returns_primary():
    assert build_refined_goal("primary", []) == "primary"


def test_build_refined_goal_joins_with_semicolons():
    out = build_refined_goal("primary", ["a", "b"])
    assert "primary" in out
    assert "a; b" in out
    assert "Additional constraints" in out


def test_build_refined_goal_empty_refinement_skipped_by_join():
    out = build_refined_goal("primary", ["a", "", "b"])
    # join puts empty strings through as gaps; that's fine and documented
    assert "primary" in out
    assert "a" in out and "b" in out
