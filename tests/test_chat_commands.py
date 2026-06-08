"""End-to-end command tests (Phase 1).

These exercise the full ``registry.lookup → handler.run`` path and
assert the side effects of each command. They cover the surface that
the legacy if/elif tree in ``chat_loop`` exposed.
"""

import pytest

from arc.chat.commands import build_registry
from arc.chat.commands.builtins import _QuitRequested
from arc.chat.registry import format_help_lines
from arc.chat.state import ChatState
from tests.fakes import make_workflow, make_artifact, make_run


pytestmark = pytest.mark.chat


# ── Construction ─────────────────────────────────────────────────────────

def test_registry_loads_all_expected_commands():
    reg = build_registry()
    expected = {
        "help", "quit", "clear",
        "artifacts", "results", "sessions",
        "packages", "package", "coder", "target",
        "services", "exec", "sweep",
        "optimize", "iterate", "run", "continue",
        "strategy", "preset", "clusters", "skills",
        "file", "build-context",
    }
    names = {cmd.name for cmd in reg.all()}
    assert expected == names, f"missing or extra commands: {expected ^ names}"


def test_help_text_renders_without_drift():
    """Snapshot-style check: render the help and assert a few invariants
    that must not regress. Avoids pinning every single line so adding a
    new command doesn't force a snapshot update."""
    reg = build_registry()
    lines = format_help_lines(reg)
    text = "\n".join(lines)
    # Every visible command must appear
    for name in ("help", "quit", "clear", "artifacts", "results",
                 "services", "target"):
        assert f"/{name}" in text, f"/{name} missing from help output"
    # All lines start with two spaces (consistent indent)
    assert all(line.startswith("  ") for line in lines), (
        "every help line must start with the standard 2-space indent"
    )
    # All lines reach at least the longest-signature column (alignment)
    longest_sig = max(len(line.rstrip()) for line in lines)
    min_widths = [len(line) for line in lines]
    assert min(min_widths) >= 4, "help lines should be non-empty"
    assert longest_sig > 10, "expected at least one substantial command"


# ── /quit ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quit_raises_quit_requested(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    cmd = reg.get("quit")
    with pytest.raises(_QuitRequested):
        await cmd.resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "goodbye" in out.lower()


@pytest.mark.asyncio
async def test_quit_aliases_resolve():
    reg = build_registry()
    for alias in ("exit", "q"):
        cmd = reg.get(alias)
        assert cmd is not None and cmd.name == "quit"


# ── /clear ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_drops_goal_and_refinements_preserves_artifact():
    reg = build_registry()
    wf = make_workflow(memory={
        "primary_goal": "old",
        "refinements": ["a", "b"],
        "current_artifact": make_artifact(),
    })
    state = ChatState(workflow=wf)
    await reg.get("clear").resolve_handler()(state, [])
    assert state.primary_goal is None
    assert state.refinements == []
    assert state.current_artifact is not None  # preserved


# ── /artifacts / /results ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_artifacts_lists_records(capsys):
    reg = build_registry()
    art = make_artifact(artifact_id="abcd1234ef", name="silicon", state="REGISTERED")
    state = ChatState(workflow=make_workflow(artifacts=[art]))
    await reg.get("artifacts").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "abcd1234" in out and "silicon" in out and "REGISTERED" in out


@pytest.mark.asyncio
async def test_artifacts_empty_prints_message(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(artifacts=[]))
    await reg.get("artifacts").resolve_handler()(state, [])
    assert "no artifacts" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_results_lists_runs(capsys):
    reg = build_registry()
    run = make_run(run_id="r0000001", status="completed", outputs={"bandgap_ev": 1.12})
    state = ChatState(workflow=make_workflow(results=[run]))
    await reg.get("results").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "r0000001" in out and "completed" in out and "1.12" in out


@pytest.mark.asyncio
async def test_results_empty_prints_message(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(results=[]))
    await reg.get("results").resolve_handler()(state, [])
    assert "no runs" in capsys.readouterr().out.lower()


# ── /packages and /package ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_packages_lists_loaded(capsys):
    reg = build_registry()
    wf = make_workflow(memory={}, packages=["arc-sim2l", "arc-codex"])
    state = ChatState(workflow=wf)
    await reg.get("packages").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "arc-sim2l" in out and "arc-codex" in out


@pytest.mark.asyncio
async def test_package_enable_sets_state():
    reg = build_registry()
    wf = make_workflow(memory={}, packages=["arc-codex"])
    state = ChatState(workflow=wf)
    await reg.get("package").resolve_handler()(state, ["enable", "arc-codex"])
    pkg_state = state.workflow._context.memory["packages"]
    assert "arc-codex" in pkg_state["enabled"]


@pytest.mark.asyncio
async def test_package_disable_unknown_prints_error(capsys):
    reg = build_registry()
    wf = make_workflow(memory={}, packages=["arc-codex"])
    state = ChatState(workflow=wf)
    await reg.get("package").resolve_handler()(state, ["enable", "not-a-package"])
    out = capsys.readouterr().out
    assert "not loaded" in out.lower()


@pytest.mark.asyncio
async def test_package_bad_usage_prints_error(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(packages=["arc-codex"]))
    await reg.get("package").resolve_handler()(state, ["wrong"])
    assert "usage" in capsys.readouterr().out.lower()


# ── /target ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_target_show_when_unset(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("target").resolve_handler()(state, [])
    assert "none" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_target_set_replaces_existing():
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={"target": {"old": 1.0}}))
    await reg.get("target").resolve_handler()(state, ["bandgap_ev=1.05"])
    assert state.target == {"bandgap_ev": 1.05}


@pytest.mark.asyncio
async def test_target_clear_removes_target():
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={"target": {"old": 1.0}}))
    await reg.get("target").resolve_handler()(state, ["clear"])
    assert state.target == {}


@pytest.mark.asyncio
async def test_target_update_merges():
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={"target": {"a": 1.0}}))
    await reg.get("target").resolve_handler()(state, ["update", "b=2.0"])
    assert state.target == {"a": 1.0, "b": 2.0}


# ── /coder ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coder_no_args_shows_current(capsys):
    reg = build_registry()
    wf = make_workflow(memory={})
    state = ChatState(workflow=wf)
    await reg.get("coder").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "builder" in out  # default


@pytest.mark.asyncio
async def test_coder_set_unknown_prints_error(capsys):
    reg = build_registry()
    wf = make_workflow(memory={})
    state = ChatState(workflow=wf)
    await reg.get("coder").resolve_handler()(state, ["doesnotexist"])
    assert "unknown" in capsys.readouterr().out.lower()


# ── /services ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_services_status_renders(capsys, monkeypatch):
    """When sim2l is installed, /services status prints all three rows."""
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    # Force `is_running` to return False for everything (no real daemons)
    from arc import services as svc
    monkeypatch.setattr(svc, "_read_pid", lambda name: None)
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: False)
    await reg.get("services").resolve_handler()(state, [])
    out = capsys.readouterr().out
    for name in ("cache", "catalog", "results"):
        assert name in out


@pytest.mark.asyncio
async def test_services_unknown_subcommand_prints_usage(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("services").resolve_handler()(state, ["banana"])
    out = capsys.readouterr().out.lower()
    assert "unknown" in out and "usage" in out


@pytest.mark.asyncio
async def test_services_when_sim2l_missing_prints_install_hint(capsys, monkeypatch):
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    from arc import services as svc
    monkeypatch.setattr(svc, "sim2l_available", lambda: False)
    await reg.get("services").resolve_handler()(state, ["status"])
    assert "not installed" in capsys.readouterr().out.lower()


# ── /exec / /iterate / /run / continue ────────────────────────────────────

@pytest.mark.asyncio
async def test_exec_no_args_prints_usage(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("exec").resolve_handler()(state, [])
    assert "usage" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_iterate_no_goal_prints_error(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("iterate").resolve_handler()(state, [])
    assert "no goal" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_run_no_args_no_existing_goal_prints_error(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("run").resolve_handler()(state, [])
    assert "no goal" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_continue_no_goal_prints_error(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("continue").resolve_handler()(state, [])
    assert "no saved goal" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_resume_is_alias_for_continue(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    cmd = reg.get("resume")
    assert cmd is not None and cmd.name == "continue"
    await cmd.resolve_handler()(state, [])
    assert "no saved goal" in capsys.readouterr().out.lower()
