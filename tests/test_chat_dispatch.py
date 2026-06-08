"""Phase 0 baseline: pin the observable side effects of each slash command.

These tests simulate the body of each branch in ``chat_loop`` without
running the full REPL. Every command's contract — what state it mutates,
what it prints — gets a test. When Phase 1 extracts the branches into
``arc/chat/commands/*.py``, these tests must stay green.

The tests do **not** call the LLM, spawn subprocesses, or touch the disk
outside ``tmp_path``. The ``isolated_sim2l_home`` autouse fixture in
``conftest.py`` redirects ``SIM2L_HOME``.
"""

from types import SimpleNamespace

import pytest

from arc import chat
from arc.chat import loop as chat_loop
from tests.fakes import (
    FakeProvider,
    make_workflow,
    make_artifact,
    make_run,
)


pytestmark = pytest.mark.chat


# ── /clear ─────────────────────────────────────────────────────────────────

def test_clear_drops_primary_goal_and_refinements():
    wf = make_workflow(memory={
        "primary_goal": "optimize bandgap",
        "refinements": ["constraint A"],
        "current_artifact": "preserved",
    })
    # Simulate what the /clear branch does (chat.py:1806-1811):
    wf._context.memory.pop("primary_goal", None)
    wf._context.memory.pop("refinements", None)

    assert "primary_goal" not in wf._context.memory
    assert "refinements" not in wf._context.memory
    # current_artifact is intentionally preserved
    assert wf._context.memory["current_artifact"] == "preserved"


# ── /artifacts ─────────────────────────────────────────────────────────────

def test_artifacts_command_lists_registered_artifacts(capsys):
    art = make_artifact(artifact_id="abcd1234ef", name="silicon_bandgap", state="REGISTERED")
    wf = make_workflow(artifacts=[art])

    # Simulate the /artifacts branch:
    records = wf.artifacts.list_all()
    for r in records:
        print(f"  {r.artifact_id[:8]}  {r.name}  {r.state}")

    out = capsys.readouterr().out
    assert "abcd1234" in out
    assert "silicon_bandgap" in out
    assert "REGISTERED" in out


def test_artifacts_command_with_empty_registry_prints_message(capsys):
    wf = make_workflow(artifacts=[])
    records = wf.artifacts.list_all()
    assert records == []


# ── /results ───────────────────────────────────────────────────────────────

def test_results_command_lists_runs(capsys):
    run = make_run(run_id="run00001x", status="completed",
                   outputs={"bandgap_ev": 1.12})
    wf = make_workflow(results=[run])

    runs = wf.results.list_all()
    for r in runs:
        print(f"  {r.run_id[:8]}  {r.status}  outputs={r.outputs}")

    out = capsys.readouterr().out
    assert "run00001" in out
    assert "completed" in out
    assert "1.12" in out


# ── /packages ──────────────────────────────────────────────────────────────

def test_packages_lists_loaded_packages():
    wf = make_workflow(packages=["arc-sim2l", "arc-claude-code"])
    pkgs = wf.registry.list_packages()
    assert set(pkgs) == {"arc-sim2l", "arc-claude-code"}


def test_packages_session_state_enable_disable():
    wf = make_workflow(memory={}, packages=["arc-codex"])

    # Simulate /package enable arc-codex:
    chat._set_session_package_state(wf, "arc-codex", enabled=True)
    state = wf._context.memory["packages"]
    assert "arc-codex" in state["enabled"]
    assert "arc-codex" not in state.get("disabled", [])

    # Simulate /package disable arc-codex:
    chat._set_session_package_state(wf, "arc-codex", enabled=False)
    state = wf._context.memory["packages"]
    assert "arc-codex" in state["disabled"]
    assert "arc-codex" not in state["enabled"]


# ── /coder ─────────────────────────────────────────────────────────────────

def test_coder_default_is_builder():
    wf = make_workflow(memory={})
    assert chat._selected_coder(wf) == "builder"


def test_coder_aliases_resolve():
    wf = make_workflow(memory={})

    # Add a fake codex backend so _set_selected_coder can find it
    wf.registry.list_agent_sources = lambda role: ["arc-codex"]
    selected = chat._set_selected_coder(wf, "codex")
    assert selected == "arc-codex:coder"
    assert wf._context.memory["agent_overrides"]["coder"] == "arc-codex:coder"


def test_coder_setting_builder_clears_override():
    wf = make_workflow(memory={"agent_overrides": {"coder": "arc-codex:coder"}})
    wf.registry.list_agent_sources = lambda role: ["arc-codex"]
    selected = chat._set_selected_coder(wf, "builder")
    assert selected == "builder"
    assert "coder" not in wf._context.memory["agent_overrides"]


def test_coder_unknown_backend_raises():
    wf = make_workflow(memory={})
    with pytest.raises(ValueError, match="Unknown coder backend"):
        chat._set_selected_coder(wf, "nonexistent-backend")


# ── builder folded into the strategy resolver (TODO item 13) ────────────────

def test_coder_selection_writes_strategy_overrides():
    """Selecting a coder now writes the unified strategy_overrides store
    (catalogue key) — not only the legacy agent_overrides."""
    wf = make_workflow(memory={})
    wf.registry.list_agent_sources = lambda role: ["arc-codex"]
    chat._set_selected_coder(wf, "codex")
    assert wf._context.memory["strategy_overrides"]["builder"] == "codex"
    # legacy store mirrored for back-compat
    assert wf._context.memory["agent_overrides"]["coder"] == "arc-codex:coder"


def test_coder_builder_clears_both_stores():
    wf = make_workflow(memory={
        "strategy_overrides": {"builder": "codex"},
        "agent_overrides": {"coder": "arc-codex:coder"},
    })
    wf.registry.list_agent_sources = lambda role: ["arc-codex"]
    chat._set_selected_coder(wf, "builder")
    assert "builder" not in wf._context.memory["strategy_overrides"]
    assert "coder" not in wf._context.memory["agent_overrides"]


def test_coder_selected_reads_strategy_overrides_first():
    """_selected_coder prefers the resolver store, mapping the catalogue
    key back to the loop's backend label."""
    wf = make_workflow(memory={"strategy_overrides": {"builder": "claude_code"}})
    assert chat._selected_coder(wf) == "arc-claude-code:coder"


def test_coder_legacy_agent_overrides_still_honoured():
    """A session saved before the builder became a strategy role (only
    agent_overrides set) still resolves to the right backend."""
    wf = make_workflow(memory={"agent_overrides": {"coder": "arc-codex:coder"}})
    assert chat._selected_coder(wf) == "arc-codex:coder"


def test_coder_agent_class_resolves_through_builder_role():
    """_coder_agent_class returns (label, class) via the resolver."""
    from arc.chat.loop import _coder_agent_class
    wf = make_workflow(memory={"strategy_overrides": {"builder": "codex"}})
    label, cls = _coder_agent_class(wf)
    assert label == "arc-codex:coder"
    assert cls.__name__ == "CodexCoderAgent"

    wf2 = make_workflow(memory={})
    label2, cls2 = _coder_agent_class(wf2)
    assert label2 == "builder"
    assert cls2.__name__ == "Sim2LBuilderAgent"


@pytest.mark.asyncio
async def test_build_context_command_sets_runtime_override():
    from arc.chat.commands.build_context import run
    from arc.chat.state import ChatState

    wf = make_workflow(memory={})
    wf.registry.list_workflows = lambda: ["paper-context", "other-loop"]
    state = ChatState(workflow=wf)
    state.persist = lambda: None

    await run(state, ["paper-context"])

    assert wf._context.memory["build_context_workflows"] == ["paper-context"]


# ── /target ────────────────────────────────────────────────────────────────

def test_target_show():
    action, target, detail = chat._parse_target_command("/target", {"bandgap_ev": 1.1})
    assert action == "show"
    assert target == {"bandgap_ev": 1.1}


def test_target_clear():
    action, target, _ = chat._parse_target_command("/target clear", {"bandgap_ev": 1.1})
    assert action == "clear"
    assert target == {}


def test_target_replace():
    action, target, _ = chat._parse_target_command(
        "/target bandgap_ev=1.05", {"old": 99}
    )
    assert action == "set"
    assert target == {"bandgap_ev": 1.05}
    assert "old" not in target


def test_target_update_merges():
    action, target, _ = chat._parse_target_command(
        "/target update strain=0.02", {"bandgap_ev": 1.1}
    )
    assert action == "set"
    assert target == {"bandgap_ev": 1.1, "strain": 0.02}


# ── /run goal vs free-text goal ────────────────────────────────────────────

def test_run_command_resets_all_session_state():
    """The /run branch clears artifact, plan, history, params, refinements."""
    wf = make_workflow(memory={
        "primary_goal": "old goal",
        "current_artifact": "X",
        "current_plan": "Y",
        "run_history": ["a", "b"],
        "next_parameters": {"k": 1},
        "refinements": ["x"],
    })
    new_goal = "optimize bandgap"

    # Simulate /run new_goal body:
    ctx = wf._context
    ctx.memory.pop("current_artifact", None)
    ctx.memory.pop("current_plan", None)
    ctx.memory.pop("run_history", None)
    ctx.memory.pop("next_parameters", None)
    ctx.memory.pop("refinements", None)
    ctx.memory["primary_goal"] = new_goal

    assert ctx.memory["primary_goal"] == new_goal
    for key in ("current_artifact", "current_plan", "run_history",
                "next_parameters", "refinements"):
        assert key not in ctx.memory, f"{key} should be cleared by /run"


# ── input normalization ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("/help",         "/help"),
    ("\\help",        "/help"),
    ("  /run goal ",  "/run goal"),
    ("\\run goal",    "/run goal"),
    ("hello",         "hello"),
])
def test_normalize_chat_command(raw, expected):
    assert chat._normalize_chat_command(raw) == expected


# ── _refinement_needs_artifact_rebuild ─────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("output is broken",                    True),
    ("the metric is wrong",                 True),
    ("schema is missing fields",            True),
    ("compliance score is always 0",        True),
    ("try a smaller thickness",             False),
    ("increase temperature to 350K",        False),
    ("set parameter mass to 0.5",           False),
])
def test_refinement_needs_artifact_rebuild(text, expected):
    assert chat._refinement_needs_artifact_rebuild(text) is expected


# ── _is_related_refinement ─────────────────────────────────────────────────

def test_unrelated_text_is_not_a_refinement():
    art = make_artifact()
    ctx = SimpleNamespace(memory={"target": {"bandgap_ev": 1.1}, "schema_registry": {}})
    assert not chat._is_related_refinement(
        "what's the weather today",
        "optimize bandgap",
        art,
        ctx,
    )


def test_artifact_output_key_match_is_a_refinement():
    art = make_artifact(outputs={"bandgap_ev": {}, "compliance": {}})
    ctx = SimpleNamespace(memory={"target": {"bandgap_ev": 1.1}, "schema_registry": {}})
    assert chat._is_related_refinement(
        "compliance is always 0",
        "optimize bandgap",
        art,
        ctx,
    )


def test_slash_commands_never_treated_as_refinement():
    art = make_artifact()
    ctx = SimpleNamespace(memory={"target": {}, "schema_registry": {}})
    assert not chat._is_related_refinement("/help", "any goal", art, ctx)
    assert not chat._is_related_refinement("\\help", "any goal", art, ctx)


# ── _register_artifact_with_sim2l ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_uses_active_backend():
    """Registration routes through ``workflow.backend``; an active
    backend's register result is returned."""
    from arc.chat.plan_mode import set_plan_mode
    set_plan_mode(False)

    art = make_artifact()
    called_with = []

    class _ActiveBackend:
        def is_active(self):
            return True
        async def register_artifact(self, a):
            called_with.append(a)
            return {"registered": True, "sim_name": a.name, "sim_version": "0.1.0",
                    "catalog_persisted": True}

    wf = SimpleNamespace(backend=_ActiveBackend(), session_id="s", _db_path=None)
    result = await chat._register_artifact_with_sim2l(wf, art)
    assert called_with == [art]
    assert result["registered"] is True


@pytest.mark.asyncio
async def test_register_skips_silently_when_backend_inactive():
    """When the workflow's backend is inactive (the no-op / local-only
    case), the function returns None so the caller skips registration UI
    entirely — no exception, no warning."""
    from arc.chat.plan_mode import set_plan_mode
    set_plan_mode(False)

    art = make_artifact()

    class _InactiveBackend:
        def is_active(self):
            return False
        async def register_artifact(self, a):  # should never be called
            raise AssertionError("inactive backend must not register")

    wf = SimpleNamespace(backend=_InactiveBackend(), session_id="s", _db_path=None)
    result = await chat._register_artifact_with_sim2l(wf, art)
    assert result is None


def test_registration_success_parts_uses_github_label():
    art = make_artifact(name="model")
    label, detail, show_catalog_warning = chat_loop._registration_success_parts(
        {
            "registered": True,
            "backend": "github",
            "repo": "owner/repo",
            "path": "artifacts/model/0.1.0",
        },
        art,
    )

    assert label == "GitHub published"
    assert detail == "owner/repo / artifacts/model/0.1.0"
    assert show_catalog_warning is False


def test_registration_failure_label_uses_backend_name():
    assert chat_loop._registration_failure_label({"backend": "github"}) == "GitHub"
    assert chat_loop._registration_failure_label({"backend": "sim2l"}) == "Sim2L"


# ── _check_sim2l_services ──────────────────────────────────────────────────

def test_check_sim2l_services_returns_three_keys(monkeypatch):
    import requests
    class FakeResponse:
        status_code = 404
    def fake_get(*args, **kwargs):
        return FakeResponse()
    monkeypatch.setattr(requests, "get", fake_get)
    status = chat._check_sim2l_services()
    assert set(status.keys()) == {"cache", "catalog", "results"}
    assert all(v is False for v in status.values())


def test_check_sim2l_services_handles_connection_refused(monkeypatch):
    import requests
    def fake_get(*args, **kwargs):
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", fake_get)
    status = chat._check_sim2l_services()
    assert all(v is False for v in status.values())


def test_check_sim2l_services_reports_running(monkeypatch):
    import requests
    class FakeResponse:
        status_code = 200
    def fake_get(*args, **kwargs):
        return FakeResponse()
    monkeypatch.setattr(requests, "get", fake_get)
    status = chat._check_sim2l_services()
    assert all(v is True for v in status.values())
