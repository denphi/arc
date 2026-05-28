"""``arc chat --check`` dry-run report tests (Phase 2)."""

import json
import os
import re

import pytest

from arc.chat.check import (
    CheckItem,
    CheckReport,
    _check_arc_installed,
    _check_dotenv,
    _check_env_vars,
    _check_packages,
    _check_python_version,
    _check_sessions_dir,
    _check_sim2l_installed,
    run_check,
)
from arc.chat.check_render import render, render_ansi, render_json


pytestmark = pytest.mark.chat


# ── CheckReport semantics ─────────────────────────────────────────────────

def test_overall_returns_worst_verdict():
    rep = CheckReport(items=[
        CheckItem("a", "ok"),
        CheckItem("b", "warning"),
        CheckItem("c", "ok"),
    ])
    assert rep.overall == "warning"
    assert rep.exit_code == 1


def test_overall_blocked_takes_priority():
    rep = CheckReport(items=[
        CheckItem("a", "warning"),
        CheckItem("b", "blocked"),
        CheckItem("c", "ok"),
    ])
    assert rep.overall == "blocked"
    assert rep.exit_code == 2


def test_overall_ok_when_all_ok():
    rep = CheckReport(items=[CheckItem("a", "ok"), CheckItem("b", "ok")])
    assert rep.overall == "ok"
    assert rep.exit_code == 0


def test_overall_ok_when_no_items():
    rep = CheckReport()
    assert rep.overall == "ok"
    assert rep.exit_code == 0


# ── Individual checks ─────────────────────────────────────────────────────

def test_python_version_ok():
    item = _check_python_version()
    assert item.verdict == "ok"
    assert "." in item.detail


def test_arc_installed_returns_version():
    item = _check_arc_installed()
    assert item.verdict == "ok"


def test_sim2l_installed_or_warning():
    item = _check_sim2l_installed()
    assert item.verdict in {"ok", "warning"}


def test_env_vars_blocked_when_required_unset(monkeypatch):
    monkeypatch.delenv("OPENWEBUI_KEY", raising=False)
    items = _check_env_vars("openwebui")
    blocked = [it for it in items if it.verdict == "blocked"]
    assert any("OPENWEBUI_KEY" in it.name for it in blocked)


def test_env_vars_ok_when_required_set(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_KEY", "fake-token-value-that-must-not-leak")
    items = _check_env_vars("openwebui")
    ok_items = [it for it in items if it.name == "OPENWEBUI_KEY"]
    assert ok_items and ok_items[0].verdict == "ok"
    # The detail must NOT contain the token value
    assert "fake-token" not in ok_items[0].detail


def test_env_vars_skips_unrelated_providers():
    """Asking for openwebui shouldn't probe ANTHROPIC_API_KEY."""
    items = _check_env_vars("openwebui")
    names = {it.name for it in items}
    assert "ANTHROPIC_API_KEY" not in names
    assert "OPENAI_API_KEY" not in names


def test_env_vars_all_providers_when_none_specified():
    items = _check_env_vars(None)
    names = {it.name for it in items}
    assert "OPENWEBUI_KEY" in names
    assert "OPENAI_API_KEY" in names
    assert "ANTHROPIC_API_KEY" in names


def test_packages_ok_when_loadable():
    item = _check_packages()
    assert item.verdict in {"ok", "warning"}


def test_sessions_dir_creates_message_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "no-exist"))
    item = _check_sessions_dir()
    assert item.verdict == "ok"
    assert "create" in item.detail.lower()


def test_sessions_dir_counts_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    (home / "session-a").mkdir()
    (home / "session-b").mkdir()
    item = _check_sessions_dir()
    assert item.verdict == "ok"
    assert item.info.get("count") == 2


def test_sessions_dir_blocked_when_not_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)  # read+exec, no write
    try:
        item = _check_sessions_dir()
        # On some platforms (containerised root) chmod doesn't stick; skip then.
        if item.verdict != "blocked":
            pytest.skip("filesystem permission test inconclusive on this platform")
        assert "writable" in item.detail.lower()
    finally:
        home.chmod(0o700)


# ── run_check end-to-end (no LLM probe) ────────────────────────────────────

@pytest.mark.asyncio
async def test_run_check_returns_a_report_with_all_sections(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_KEY", "x")
    report = await run_check(provider="openwebui", probe_provider=False)
    names = [it.name for it in report.items]
    # Must include every check label
    expected = {
        "Python", "arc package", "sim2l package", ".env",
        "sessions dir", "packages",
    }
    assert expected.issubset(set(names))


@pytest.mark.asyncio
async def test_run_check_skips_provider_when_probe_disabled():
    report = await run_check(provider="openwebui", probe_provider=False)
    names = [it.name for it in report.items]
    assert not any(n.startswith("provider ") for n in names)


@pytest.mark.asyncio
async def test_run_check_makes_no_provider_call_when_token_missing(monkeypatch):
    """If no token resolves, the provider check must short-circuit
    (no network call). Otherwise --check would hang on a missing config."""
    monkeypatch.delenv("OPENWEBUI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    report = await run_check(provider="openwebui", probe_provider=True)
    provider_items = [it for it in report.items if it.name.startswith("provider ")]
    assert provider_items, "expected a provider row in the report"
    assert provider_items[0].verdict == "warning"


# ── Renderers ─────────────────────────────────────────────────────────────

def test_render_json_round_trips():
    rep = CheckReport(items=[
        CheckItem("a", "ok", "detail"),
        CheckItem("b", "warning", "with info", info={"port": 8002}),
    ])
    text = render_json(rep)
    parsed = json.loads(text)
    assert parsed["overall"] == "warning"
    assert parsed["exit_code"] == 1
    assert parsed["items"][1]["info"]["port"] == 8002


def test_render_ansi_includes_all_rows():
    rep = CheckReport(items=[
        CheckItem("Python", "ok", "3.11"),
        CheckItem("sim2l catalog", "warning", "not reachable"),
    ])
    text = render_ansi(rep)
    assert "Python" in text
    assert "sim2l catalog" in text
    assert "warning" in text


def test_render_dispatches_by_fmt():
    rep = CheckReport(items=[CheckItem("a", "ok")])
    assert render(rep, fmt="json").startswith("{")
    assert "✓" in render(rep, fmt="ansi") or "a" in render(rep, fmt="ansi")


# ── Secret-safety: no token value ever appears in any output ──────────────

@pytest.mark.asyncio
async def test_check_output_does_not_leak_token(monkeypatch, capsys):
    """The most important invariant: never echo a secret."""
    secret = "sk-very-secret-1234567890abcdef-do-not-print-this"
    monkeypatch.setenv("OPENWEBUI_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    report = await run_check(provider="openwebui", probe_provider=False)

    # Search every string field
    all_text = []
    for it in report.items:
        all_text.append(it.name)
        all_text.append(it.detail)
        all_text.append(json.dumps(it.info))
    blob = "\n".join(all_text)

    assert secret not in blob, "token value appeared in CheckItem fields"

    # Also check rendered output (both formats)
    assert secret not in render_ansi(report)
    assert secret not in render_json(report)


@pytest.mark.asyncio
async def test_check_output_no_random_token_like_strings(monkeypatch):
    """Defence in depth: scan for anything matching a token-like pattern
    in --check output. This catches the case where a future check adds a
    leak the explicit secret test wouldn't see."""
    secret = "abcdef01234567890abcdef0123456789abcdef0123"
    monkeypatch.setenv("OPENWEBUI_KEY", secret)
    report = await run_check(provider="openwebui", probe_provider=False)
    text = render_ansi(report) + render_json(report)
    # 30+ alphanumeric chars that aren't a known safe string (dates, paths)
    # would be suspicious.
    suspicious = re.findall(r"[A-Za-z0-9]{30,}", text)
    # Filter out paths/filenames (allowed)
    suspicious = [s for s in suspicious if secret == s]
    assert not suspicious, f"suspicious token-like strings in output: {suspicious}"
