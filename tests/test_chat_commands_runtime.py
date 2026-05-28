"""Coverage tests for ``/iterate``, ``/run``, ``/sessions``, ``/help``.

These commands either call ``_run_with_continuation`` (the long-running
research loop) or render REPL state. We stub the long-running path and
test the happy/sad branches each handler picks.
"""

import pytest

from arc.chat.commands import build_registry
from arc.chat.state import ChatState
from tests.fakes import make_artifact, make_workflow


pytestmark = pytest.mark.chat


# ── /iterate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_iterate_no_goal_prints_error(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("iterate").resolve_handler()(state, [])
    assert "No goal" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_iterate_no_artifact_no_provider_refuses(capsys):
    """Stub mode + no artifact: can't iterate (would need an LLM to plan)."""
    reg = build_registry()
    wf = make_workflow(memory={"primary_goal": "g"})
    wf.provider = None  # stub
    state = ChatState(workflow=wf)
    await reg.get("iterate").resolve_handler()(state, [])
    assert "No artifact and no LLM provider" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_iterate_default_n_is_3(monkeypatch):
    """When no argv is given, max_iterations defaults to 3."""
    captured = {}
    async def fake_run(workflow, goal, *, max_iterations=None, start_artifact=None):
        captured["max_iterations"] = max_iterations
        captured["start_artifact"] = start_artifact
        captured["goal"] = goal
    monkeypatch.setattr("arc.chat.loop._run_with_continuation", fake_run)

    art = make_artifact()
    state = ChatState(workflow=make_workflow(memory={
        "primary_goal": "g", "current_artifact": art,
    }))
    state.current_artifact = art
    reg = build_registry()
    await reg.get("iterate").resolve_handler()(state, [])
    assert captured["max_iterations"] == 3
    assert captured["goal"] == "g"
    assert captured["start_artifact"] is art


@pytest.mark.asyncio
async def test_iterate_explicit_n(monkeypatch):
    captured = {}
    async def fake_run(workflow, goal, *, max_iterations=None, start_artifact=None):
        captured["max_iterations"] = max_iterations
    monkeypatch.setattr("arc.chat.loop._run_with_continuation", fake_run)

    art = make_artifact()
    state = ChatState(workflow=make_workflow(memory={
        "primary_goal": "g", "current_artifact": art,
    }))
    state.current_artifact = art
    reg = build_registry()
    await reg.get("iterate").resolve_handler()(state, ["7"])
    assert captured["max_iterations"] == 7


@pytest.mark.asyncio
async def test_iterate_non_numeric_argv_falls_back_to_default(monkeypatch):
    """``/iterate banana`` parses to default 3, doesn't crash."""
    captured = {}
    async def fake_run(workflow, goal, *, max_iterations=None, start_artifact=None):
        captured["max_iterations"] = max_iterations
    monkeypatch.setattr("arc.chat.loop._run_with_continuation", fake_run)

    art = make_artifact()
    state = ChatState(workflow=make_workflow(memory={
        "primary_goal": "g", "current_artifact": art,
    }))
    state.current_artifact = art
    reg = build_registry()
    await reg.get("iterate").resolve_handler()(state, ["banana"])
    assert captured["max_iterations"] == 3


# ── /run ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_no_goal_anywhere_prints_error(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("run").resolve_handler()(state, [])
    assert "No goal" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_uses_argv_as_goal(monkeypatch):
    captured = {}
    async def fake_run(workflow, goal, *, max_iterations=None):
        captured["goal"] = goal
        captured["max_iterations"] = max_iterations
    monkeypatch.setattr("arc.chat.loop._run_with_continuation", fake_run)

    state = ChatState(workflow=make_workflow(memory={}), max_iterations=20)
    reg = build_registry()
    await reg.get("run").resolve_handler()(state, ["simulate", "silicon", "at", "300K"])
    assert captured["goal"] == "simulate silicon at 300K"
    assert captured["max_iterations"] == 20


@pytest.mark.asyncio
async def test_run_resets_session_state(monkeypatch):
    """``/run`` should clear current_artifact, refinements, etc."""
    async def fake_run(workflow, goal, *, max_iterations=None):
        pass
    monkeypatch.setattr("arc.chat.loop._run_with_continuation", fake_run)

    state = ChatState(workflow=make_workflow(memory={
        "primary_goal": "old",
        "refinements": ["r1"],
        "current_artifact": make_artifact(),
        "current_plan": "plan",
        "next_parameters": {"x": 1},
    }))
    reg = build_registry()
    await reg.get("run").resolve_handler()(state, ["new goal"])

    mem = state.workflow._context.memory
    assert mem["primary_goal"] == "new goal"
    for cleared in ("refinements", "current_artifact", "current_plan", "next_parameters"):
        assert cleared not in mem, f"{cleared} should have been cleared by /run"


@pytest.mark.asyncio
async def test_run_with_no_argv_reuses_existing_goal(monkeypatch):
    captured = {}
    async def fake_run(workflow, goal, *, max_iterations=None):
        captured["goal"] = goal
    monkeypatch.setattr("arc.chat.loop._run_with_continuation", fake_run)

    state = ChatState(workflow=make_workflow(memory={"primary_goal": "existing"}))
    reg = build_registry()
    await reg.get("run").resolve_handler()(state, [])
    assert captured["goal"] == "existing"


# ── /sessions ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sessions_no_sessions_message(capsys, monkeypatch):
    monkeypatch.setattr("arc.chat.commands.sessions.list_sessions", lambda: [])
    reg = build_registry()
    state = ChatState(workflow=make_workflow(session_id="sess-x"))
    await reg.get("sessions").resolve_handler()(state, [])
    assert "No sessions yet" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_sessions_lists_and_marks_current(capsys, monkeypatch):
    sessions = [
        {"session_id": "sess-x", "iteration": 3, "goal": "current goal text"},
        {"session_id": "sess-y", "iteration": 1, "goal": "other"},
    ]
    monkeypatch.setattr("arc.chat.commands.sessions.list_sessions", lambda: sessions)
    reg = build_registry()
    state = ChatState(workflow=make_workflow(session_id="sess-x"))
    await reg.get("sessions").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "sess-x" in out
    assert "sess-y" in out
    assert "current" in out  # the active session marker
    # The resume hint is shown at the end
    assert "arc chat --session" in out


@pytest.mark.asyncio
async def test_sessions_truncates_long_goal(capsys, monkeypatch):
    long_goal = "x" * 200
    monkeypatch.setattr(
        "arc.chat.commands.sessions.list_sessions",
        lambda: [{"session_id": "s", "iteration": 0, "goal": long_goal}],
    )
    reg = build_registry()
    state = ChatState(workflow=make_workflow(session_id="other"))
    await reg.get("sessions").resolve_handler()(state, [])
    out = capsys.readouterr().out
    # Goal preview is capped at 50 chars; full string shouldn't appear
    assert long_goal not in out


@pytest.mark.asyncio
async def test_sessions_handles_session_with_no_goal(capsys, monkeypatch):
    """``goal=None`` shouldn't crash the truncation (regression check
    for the ``(s["goal"] or "")[:50]`` defence)."""
    monkeypatch.setattr(
        "arc.chat.commands.sessions.list_sessions",
        lambda: [{"session_id": "s", "iteration": 0, "goal": None}],
    )
    reg = build_registry()
    state = ChatState(workflow=make_workflow(session_id="other"))
    await reg.get("sessions").resolve_handler()(state, [])
    out = capsys.readouterr().out
    # Just confirm the row is rendered without raising
    assert "iter=0" in out


# ── /help (full render) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_renders_session_meta(capsys):
    """The help-handler tail renders state snapshot — exercise every line."""
    art = make_artifact(artifact_id="abcd1234efgh", name="my-art")
    wf = make_workflow(memory={"primary_goal": "g", "target": {"k": 1.0}})
    state = ChatState(workflow=wf)
    state.current_artifact = art
    state.add_refinement("a refinement")

    reg = build_registry()
    await reg.get("help").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "ARC Commands" in out
    assert "g" in out                   # primary goal
    assert "1 constraint(s)" in out     # refinement count
    assert "my-art" in out              # artifact name
    assert "abcd1234" in out            # artifact id prefix


@pytest.mark.asyncio
async def test_help_with_no_active_state(capsys):
    """With an empty session the snapshot still renders with 'none' placeholders."""
    state = ChatState(workflow=make_workflow(memory={}))
    reg = build_registry()
    await reg.get("help").resolve_handler()(state, [])
    out = capsys.readouterr().out
    # Should include the "none" placeholders rather than crashing
    assert "none" in out


@pytest.mark.asyncio
async def test_help_aliased_question_mark_resolves_to_same_handler(capsys):
    """``/?`` is an alias for ``/help``."""
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("?").resolve_handler()(state, [])
    assert "ARC Commands" in capsys.readouterr().out
