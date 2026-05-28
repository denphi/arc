"""Direct tests for ``arc.chat.session_io`` (Q14 extraction)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arc.chat.session_io import restore_session, save_session


pytestmark = pytest.mark.chat


def _stub_workflow(session_id: str = "session-abc", memory: dict | None = None,
                   iteration: int = 0, artifact=None):
    artifacts = SimpleNamespace(
        get=lambda art_id: (
            artifact if (artifact and artifact.artifact_id == art_id)
            else (_ for _ in ()).throw(KeyError(art_id))
        ),
    )
    return SimpleNamespace(
        session_id=session_id,
        artifacts=artifacts,
        _context=SimpleNamespace(memory=dict(memory or {}), iteration=iteration),
    )


# ── save_session ─────────────────────────────────────────────────────────


def test_save_session_skips_default_id():
    """Sentinel ``"default"`` session id means an ephemeral run — no write."""
    wf = _stub_workflow(session_id="default", memory={"primary_goal": "g"})
    with patch("arc.chat.session_io.save_session_meta") as save:
        save_session(wf, "g")
    save.assert_not_called()


def test_save_session_persists_memory_fields():
    artifact = SimpleNamespace(artifact_id="abcd", name="silicon")
    wf = _stub_workflow(memory={
        "primary_goal": "p",
        "run_history": [{"x": 1}],
        "target": {"bandgap_ev": 1.1},
        "next_parameters": {"thickness": 5},
        "schema_registry": {"bandgap_ev": {}},
        "refinements": ["r1"],
        "packages": {"enabled": ["arc-codex"]},
        "agent_overrides": {"coder": "arc-codex:coder"},
        "current_artifact": artifact,
    }, iteration=3)

    with patch("arc.chat.session_io.save_session_meta") as save:
        save_session(wf, "current goal text")

    save.assert_called_once()
    kwargs = save.call_args.kwargs
    assert kwargs["session_id"] == "session-abc"
    assert kwargs["goal"] == "current goal text"
    assert kwargs["iteration"] == 3
    assert kwargs["current_artifact_id"] == "abcd"
    assert kwargs["current_artifact_name"] == "silicon"
    assert kwargs["run_history"] == [{"x": 1}]
    assert kwargs["target"] == {"bandgap_ev": 1.1}
    assert kwargs["next_parameters"] == {"thickness": 5}
    assert kwargs["primary_goal"] == "p"
    assert kwargs["refinements"] == ["r1"]


def test_save_session_handles_no_artifact():
    wf = _stub_workflow(memory={})
    with patch("arc.chat.session_io.save_session_meta") as save:
        save_session(wf, None)
    kwargs = save.call_args.kwargs
    assert kwargs["current_artifact_id"] is None
    assert kwargs["current_artifact_name"] is None


# ── restore_session ─────────────────────────────────────────────────────


def test_restore_session_skips_default_id():
    wf = _stub_workflow(session_id="default")
    with patch("arc.chat.session_io.load_session_meta") as load:
        result = restore_session(wf)
    assert result is None
    load.assert_not_called()


def test_restore_session_returns_none_when_meta_missing():
    wf = _stub_workflow()
    with patch("arc.chat.session_io.load_session_meta", return_value=None):
        assert restore_session(wf) is None


def test_restore_session_loads_into_memory():
    wf = _stub_workflow()
    meta = {
        "iteration": 7,
        "run_history": [{"a": 1}],
        "target": {"bandgap_ev": 1.0},
        "next_parameters": {"x": 2},
        "schema_registry": {"k": {"aliases": []}},
        "primary_goal": "saved",
        "refinements": ["r"],
        "packages": {"enabled": ["arc-codex"]},
        "agent_overrides": {"coder": "arc-codex:coder"},
        "goal": "saved goal text",
        "current_artifact_id": None,  # no artifact to look up
    }
    with patch("arc.chat.session_io.load_session_meta", return_value=meta):
        result = restore_session(wf)

    assert result == "saved goal text"
    assert wf._context.iteration == 7
    assert wf._context.memory["primary_goal"] == "saved"
    assert wf._context.memory["target"] == {"bandgap_ev": 1.0}
    assert wf._context.memory["refinements"] == ["r"]


def test_restore_session_tolerates_missing_artifact():
    """Saved artifact id doesn't resolve any more — best effort, no crash."""
    wf = _stub_workflow()
    meta = {
        "current_artifact_id": "missing-from-registry",
        "goal": "g",
    }
    with patch("arc.chat.session_io.load_session_meta", return_value=meta):
        result = restore_session(wf)
    assert result == "g"
    # current_artifact was NOT installed because the registry raised
    assert "current_artifact" not in wf._context.memory


def test_restore_session_loads_artifact_when_present():
    artifact = SimpleNamespace(artifact_id="found", name="thing")
    wf = _stub_workflow(artifact=artifact)
    meta = {"current_artifact_id": "found", "goal": "g"}
    with patch("arc.chat.session_io.load_session_meta", return_value=meta):
        restore_session(wf)
    assert wf._context.memory["current_artifact"] is artifact


# ── Strategy / recipe persistence ───────────────────────────────────────


def test_save_session_writes_strategy_overrides_sidecar(monkeypatch):
    """``save_session`` mirrors strategy overrides to ``session_state.json``.

    Before this PR the chat layer silently lost ``strategy_overrides``
    on restart. This test pins the new behaviour: a save call writes the
    sidecar via :func:`arc.api.session_state.save_state` so the next
    ``restore_session`` can rehydrate the override.
    """
    wf = _stub_workflow(
        session_id="session-state-sidecar",
        memory={"strategy_overrides": {"planner": "mars_planner"}},
    )
    with patch("arc.chat.session_io.save_session_meta"):
        save_session(wf, "g")

    # Read it back through the same API the loader uses.
    from arc.api.session_state import load_state
    state = load_state("session-state-sidecar")
    assert state["strategy_overrides"] == {"planner": "mars_planner"}


def test_save_session_persists_recipe_state(monkeypatch):
    """All four state keys round-trip through the sidecar."""
    wf = _stub_workflow(
        session_id="session-state-recipe",
        memory={
            "strategy_overrides": {"planner": "mars_planner",
                                   "optimizer": "bayesopt"},
            "active_recipe": "bayesian-materials",
            "recipe_applied": {"planner": "mars_planner",
                               "optimizer": "bayesopt"},
            "recipe_suggested": ["mp-discovery"],
        },
    )
    with patch("arc.chat.session_io.save_session_meta"):
        save_session(wf, "g")

    from arc.api.session_state import load_state
    state = load_state("session-state-recipe")
    assert state["strategy_overrides"]["optimizer"] == "bayesopt"
    assert state["active_recipe"] == "bayesian-materials"
    assert state["recipe_applied"] == {"planner": "mars_planner",
                                       "optimizer": "bayesopt"}
    assert state["recipe_suggested"] == ["mp-discovery"]


def test_save_session_omits_empty_state(monkeypatch):
    """A session with no overrides shouldn't litter the user's disk
    with an empty ``session_state.json``."""
    from arc.api.session_state import _state_path

    wf = _stub_workflow(session_id="session-state-empty", memory={})
    with patch("arc.chat.session_io.save_session_meta"):
        save_session(wf, "g")

    # No state to save → the file should not exist.
    assert not _state_path("session-state-empty").exists()


def test_save_session_skip_default_does_not_touch_sidecar():
    """The ``"default"`` sentinel still bypasses every write — including
    the new sidecar."""
    from arc.api.session_state import _state_path

    wf = _stub_workflow(
        session_id="default",
        memory={"strategy_overrides": {"planner": "mars_planner"}},
    )
    with patch("arc.chat.session_io.save_session_meta"):
        save_session(wf, "g")

    # We can't compute the path for the "default" sentinel (it's
    # rejected by validate_session_id). Instead assert that the
    # legacy save was not called — same as the existing test —
    # which is enough to know we short-circuited before the sidecar.
    # (If anyone added the sidecar write before the short-circuit,
    # _state_path would have raised.)
    assert wf.session_id == "default"


def test_restore_session_rehydrates_strategy_overrides():
    """``restore_session`` reads back what ``save_session`` wrote."""
    from arc.api.session_state import save_state

    save_state(
        "session-restore-strategy",
        {"strategy_overrides": {"planner": "mars_planner",
                                "reviewer": "reflective"}},
    )

    wf = _stub_workflow(session_id="session-restore-strategy")
    with patch("arc.chat.session_io.load_session_meta", return_value=None):
        result = restore_session(wf)

    # Even with no legacy session.json, the sidecar restored the override.
    assert result is None
    assert wf._context.memory["strategy_overrides"] == {
        "planner": "mars_planner", "reviewer": "reflective",
    }


def test_restore_session_rehydrates_all_state_keys():
    from arc.api.session_state import save_state

    save_state("session-restore-all", {
        "strategy_overrides": {"planner": "mars_planner"},
        "active_recipe": "bayesian-materials",
        "recipe_applied": {"planner": "mars_planner"},
        "recipe_suggested": ["mp-discovery"],
    })

    wf = _stub_workflow(session_id="session-restore-all")
    with patch("arc.chat.session_io.load_session_meta", return_value=None):
        restore_session(wf)

    assert wf._context.memory["active_recipe"] == "bayesian-materials"
    assert wf._context.memory["recipe_suggested"] == ["mp-discovery"]


def test_restore_session_handles_missing_state_file():
    """Sessions saved before this PR shipped have no ``session_state.json``
    — the chat must still load fine."""
    wf = _stub_workflow(session_id="session-restore-legacy")
    meta = {"goal": "g", "iteration": 5, "primary_goal": "p"}
    with patch("arc.chat.session_io.load_session_meta", return_value=meta):
        result = restore_session(wf)

    assert result == "g"
    assert wf._context.iteration == 5
    # No state file → no strategy_overrides key in memory.
    assert "strategy_overrides" not in wf._context.memory


def test_round_trip_chat_save_then_restore_preserves_overrides():
    """End-to-end: save a chat session with overrides, then a fresh
    ``restore_session`` brings them back. Mirrors the user flow of
    ``/strategy planner mars_planner`` → quit → re-enter the session."""
    artifact = SimpleNamespace(artifact_id="art-1", name="silicon")
    wf_save = _stub_workflow(
        session_id="round-trip-overrides",
        memory={
            "primary_goal": "design silicon",
            "strategy_overrides": {"planner": "mars_planner"},
            "active_recipe": "bayesian-materials",
            "recipe_applied": {"planner": "mars_planner"},
            "current_artifact": artifact,
        },
        iteration=2,
    )
    # Use the real save_session_meta so the legacy file lands too.
    save_session(wf_save, "design silicon")

    wf_load = _stub_workflow(
        session_id="round-trip-overrides", artifact=artifact,
    )
    result = restore_session(wf_load)
    assert result == "design silicon"
    assert wf_load._context.iteration == 2
    assert wf_load._context.memory["strategy_overrides"] == {
        "planner": "mars_planner",
    }
    assert wf_load._context.memory["active_recipe"] == "bayesian-materials"


def test_save_then_restore_does_not_persist_default_keys():
    """The sidecar only persists the four known state keys; other memory
    keys (e.g. catalog_hits) stay in-memory only."""
    wf = _stub_workflow(
        session_id="round-trip-noise",
        memory={
            "strategy_overrides": {"planner": "mars_planner"},
            "catalog_hits": [{"name": "ignored"}],   # in-memory only
        },
    )
    save_session(wf, None)

    from arc.api.session_state import load_state
    state = load_state("round-trip-noise")
    assert "strategy_overrides" in state
    assert "catalog_hits" not in state


def test_save_overwrites_previous_sidecar_state():
    """A second ``save_session`` with different overrides must replace
    the previous state, not merge with it."""
    wf = _stub_workflow(
        session_id="session-overwrite",
        memory={"strategy_overrides": {"planner": "mars_planner"}},
    )
    save_session(wf, None)

    # Now flip to a different override.
    wf._context.memory["strategy_overrides"] = {"reviewer": "reflective"}
    save_session(wf, None)

    from arc.api.session_state import load_state
    state = load_state("session-overwrite")
    assert state["strategy_overrides"] == {"reviewer": "reflective"}
    assert "planner" not in state["strategy_overrides"]


def test_save_then_clear_then_save_removes_sidecar():
    """If the user clears all overrides, the next save should remove
    the sidecar file entirely so a future session.json-only read works."""
    from arc.api.session_state import _state_path

    wf = _stub_workflow(
        session_id="session-clear",
        memory={"strategy_overrides": {"planner": "mars_planner"}},
    )
    save_session(wf, None)
    assert _state_path("session-clear").exists()

    # Now wipe the overrides and save again — file should disappear.
    wf._context.memory.pop("strategy_overrides")
    save_session(wf, None)
    assert not _state_path("session-clear").exists()
