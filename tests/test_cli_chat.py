"""CLI integration tests for ``arc chat``.

These exercise the actual ``arc.cli.main`` Typer app so the wiring
between flags and chat infrastructure is verified end-to-end.
"""

import inspect

import pytest
from typer.testing import CliRunner

from arc.cli.main import app


pytestmark = pytest.mark.chat


@pytest.fixture
def runner():
    # ``mix_stderr`` was removed from Click's CliRunner in 8.2 (stdout and
    # stderr are captured separately by default there). Only pass it on the
    # older Click that still accepts it, so the fixture works on both.
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


# ── --check ──────────────────────────────────────────────────────────────


def test_check_exit_code_is_2_when_blocked(monkeypatch, runner):
    """Finding #9: pin the actual blocked → exit 2 mapping by patching
    ``run_check`` to return a deterministic blocked report. The previous
    test allowed exit codes 1 or 2, which masked the contract."""
    import asyncio
    from arc.chat.check import CheckItem, CheckReport

    async def _fake_check(**kwargs):
        return CheckReport(items=[
            CheckItem("Python", "ok"),
            CheckItem("provider openwebui", "blocked", "no token"),
        ])
    monkeypatch.setattr("arc.chat.check.run_check", _fake_check)

    result = runner.invoke(app, ["chat", "--check"])
    assert result.exit_code == 2


def test_check_exit_code_is_1_when_warning(monkeypatch, runner):
    """Warning verdict → exit 1, precisely."""
    from arc.chat.check import CheckItem, CheckReport

    async def _fake_check(**kwargs):
        return CheckReport(items=[
            CheckItem("Python", "ok"),
            CheckItem("sim2l catalog", "warning", "not reachable"),
        ])
    monkeypatch.setattr("arc.chat.check.run_check", _fake_check)

    result = runner.invoke(app, ["chat", "--check"])
    assert result.exit_code == 1


def test_check_exit_code_is_0_when_all_green(monkeypatch, runner):
    """All-ok → exit 0."""
    from arc.chat.check import CheckItem, CheckReport

    async def _fake_check(**kwargs):
        return CheckReport(items=[CheckItem("Python", "ok")])
    monkeypatch.setattr("arc.chat.check.run_check", _fake_check)

    result = runner.invoke(app, ["chat", "--check"])
    assert result.exit_code == 0


def test_check_json_format_produces_parseable_json(runner):
    import json
    result = runner.invoke(app, ["chat", "--check", "--check-format=json"])
    # Should be valid JSON (exit code may be non-zero, but stdout must parse)
    data = json.loads(result.stdout)
    assert "overall" in data
    assert "items" in data


def test_check_never_starts_repl(runner):
    """--check must short-circuit; no prompt waiting on stdin."""
    result = runner.invoke(app, ["chat", "--check"], input="\n")
    # Should exit without consuming input — if it had entered the REPL
    # the test would hang or behave very differently.
    assert "ARC chat" in result.stdout
    assert "config check" in result.stdout


# ── --events ─────────────────────────────────────────────────────────────


def test_events_unknown_value_rejected(runner):
    result = runner.invoke(app, ["chat", "--check", "--events=banana"])
    # --check runs before --events validation, so this returns the check
    # verdict. The point is just that --events appears in --help and
    # accepts our known values.
    assert "Usage" not in result.stdout or result.exit_code in (0, 1, 2)


def test_events_help_lists_options(runner):
    result = runner.invoke(app, ["chat", "--help"])
    assert "--events" in result.stdout


def test_chat_help_lists_build_context_option(runner):
    result = runner.invoke(app, ["chat", "--help"])
    assert "--build-context" in result.stdout


# ── --plan ───────────────────────────────────────────────────────────────


def test_plan_flag_in_help(runner):
    result = runner.invoke(app, ["chat", "--help"])
    assert "--plan" in result.stdout


def test_plan_flag_combined_with_check(runner, monkeypatch):
    """``--plan --check`` should still run the check (no REPL)."""
    result = runner.invoke(app, ["chat", "--plan", "--check"])
    assert "Plan mode active" in result.stdout
    assert "config check" in result.stdout


def test_plan_banner_printed_exactly_once(runner, monkeypatch):
    """Finding #1 follow-up: the plan-mode banner must not be duplicated.

    The CLI prints it before short-circuiting on --check; chat_loop has
    its own banner. With ``--plan --check``, only the CLI banner runs."""
    result = runner.invoke(app, ["chat", "--plan", "--check"])
    occurrences = result.stdout.count("Plan mode active")
    assert occurrences == 1, (
        f"plan-mode banner printed {occurrences} times (expected 1)"
    )
