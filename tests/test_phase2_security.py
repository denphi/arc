"""Phase 2 security invariants.

Bundles every assertion that ``--check``, the event sinks, and plan mode
must satisfy. The Phase 1 ``test_chat_security_invariants.py`` covered
the chat-package code; this file covers the new Phase 2 surface.
"""

import json
import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.chat


# ── No secret leakage in --check output ───────────────────────────────────

@pytest.mark.asyncio
async def test_check_never_prints_token_value(monkeypatch):
    """Belt-and-braces: render full ANSI + JSON output and search for
    any of the configured tokens. Different from the per-test secret
    check — this exercises real provider env vars."""
    from arc.chat.check import run_check
    from arc.chat.check_render import render_ansi, render_json

    sentinels = {
        "OPENWEBUI_KEY":     "sk-openwebui-do-not-print-secret-1234",
        "OPENAI_API_KEY":    "sk-openai-do-not-print-secret-5678",
        "ANTHROPIC_API_KEY": "sk-anthropic-do-not-print-secret-9012",
    }
    for k, v in sentinels.items():
        monkeypatch.setenv(k, v)

    report = await run_check(probe_provider=False)
    blob = render_ansi(report) + "\n" + render_json(report)
    for var, value in sentinels.items():
        assert value not in blob, f"{var}={value!r} leaked into --check output"


@pytest.mark.asyncio
async def test_check_makes_no_unmocked_network_call(monkeypatch):
    """When the provider probe is disabled, --check must not initiate
    any HTTP request. We intercept ``requests.get`` and assert it gets
    called at most for sim2l health checks (which target localhost)."""
    import requests
    calls: list[str] = []

    real_get = requests.get
    def tracker(url, *args, **kwargs):
        calls.append(url)
        # Don't actually hit the network — return a fake 404.
        class R:
            status_code = 404
            def json(self): return {}
        return R()
    monkeypatch.setattr(requests, "get", tracker)

    from arc.chat.check import run_check
    await run_check(probe_provider=False)

    # Every call must be to a local sim2l service URL
    for url in calls:
        assert "localhost" in url or "127.0.0.1" in url, (
            f"--check made a non-localhost network call: {url}"
        )


# ── JsonlSink size + truncation invariants ────────────────────────────────

def test_jsonl_sink_text_truncated_below_cap(tmp_path):
    from arc.chat.events import ChatEvent, JsonlSink
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)
    sink.handle(ChatEvent(kind="info", text="x" * 1_000_000))
    sink.close()
    parsed = json.loads(path.read_text().strip())
    assert len(parsed["text"]) < 10_000


def test_jsonl_sink_rotates_at_size_cap(tmp_path):
    from arc.chat.events import ChatEvent, JsonlSink
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path, max_bytes=500)
    for _ in range(40):
        sink.handle(ChatEvent(kind="info", text="x" * 80))
    sink.close()
    # After rotation, the active file size must be bounded.
    assert path.stat().st_size < 5_000


def test_jsonl_sink_at_most_one_rotation_file(tmp_path):
    """We don't keep an unbounded rotation history."""
    from arc.chat.events import ChatEvent, JsonlSink
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path, max_bytes=200)
    # Force multiple rotations
    for _ in range(200):
        sink.handle(ChatEvent(kind="info", text="x" * 80))
    sink.close()
    rotation_count = sum(1 for p in tmp_path.glob("events.jsonl.*"))
    assert rotation_count <= 1, "expected at most one .1 rotation file"


# ── Plan mode actually blocks side effects ────────────────────────────────

def test_plan_mode_blocks_artifact_writes(tmp_path):
    from arc.chat.plan_mode import plan_mode, PlanModeBlocked
    from arc.memory.artifact_registry import ArtifactRegistry
    from arc.schemas.artifact import ArtifactDraft

    root = tmp_path / "artifacts"
    reg = ArtifactRegistry(root=str(root))
    draft = ArtifactDraft(name="t", description="x",
                          files={"a.txt": "content"}, metadata={})

    with plan_mode(True):
        with pytest.raises(PlanModeBlocked):
            reg.register(draft)

    # Critically: even though ArtifactRegistry.__init__ may have created
    # the root dir, the per-artifact directory must NOT exist.
    if root.exists():
        subdirs = [p for p in root.iterdir() if p.is_dir()]
        assert subdirs == [], (
            f"plan mode created artifact dirs: {[p.name for p in subdirs]}"
        )


def test_plan_mode_default_off_in_fresh_state():
    """Defence against accidentally turning plan mode on globally."""
    from arc.chat.plan_mode import is_plan_mode
    assert is_plan_mode() is False, (
        "plan mode must default to off — test leaked global state"
    )


# ── --check exit code matches verdict ─────────────────────────────────────

def test_check_exit_code_blocked_is_2():
    from arc.chat.check import CheckItem, CheckReport
    rep = CheckReport(items=[CheckItem("x", "blocked")])
    assert rep.exit_code == 2


def test_check_exit_code_warning_is_1():
    from arc.chat.check import CheckItem, CheckReport
    rep = CheckReport(items=[CheckItem("x", "warning")])
    assert rep.exit_code == 1


def test_check_exit_code_ok_is_0():
    from arc.chat.check import CheckItem, CheckReport
    rep = CheckReport(items=[CheckItem("x", "ok")])
    assert rep.exit_code == 0
