"""Plan-mode (Phase 2): write nothing, network nothing."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc.chat.plan_mode import (
    PlanModeBlocked,
    check_plan_gate,
    gated,
    is_plan_mode,
    plan_mode,
    set_plan_mode,
)
from arc.chat.state import ChatState
from tests.fakes import make_workflow, make_artifact


pytestmark = pytest.mark.chat


@pytest.fixture(autouse=True)
def _restore_plan_mode():
    prev = set_plan_mode(False)
    yield
    set_plan_mode(prev)


# ── Flag mechanics ────────────────────────────────────────────────────────

def test_plan_mode_default_off():
    assert is_plan_mode() is False


def test_set_plan_mode_returns_previous():
    assert set_plan_mode(True) is False
    assert is_plan_mode() is True
    assert set_plan_mode(False) is True


def test_plan_mode_context_manager_restores():
    assert is_plan_mode() is False
    with plan_mode(True):
        assert is_plan_mode() is True
    assert is_plan_mode() is False


def test_plan_mode_context_restores_on_exception():
    with pytest.raises(RuntimeError, match="boom"):
        with plan_mode(True):
            raise RuntimeError("boom")
    assert is_plan_mode() is False


# ── check_plan_gate ──────────────────────────────────────────────────────

def test_gate_passes_when_inactive():
    check_plan_gate("anything")  # no exception


def test_gate_raises_when_active():
    with plan_mode(True):
        with pytest.raises(PlanModeBlocked) as exc:
            check_plan_gate("artifact registration",
                            hint="files under workspace/")
    assert "artifact registration" in str(exc.value)
    assert "workspace" in str(exc.value)


def test_blocked_exception_carries_label():
    with plan_mode(True):
        try:
            check_plan_gate("foo", hint="bar")
        except PlanModeBlocked as exc:
            assert exc.label == "foo"
            assert exc.hint == "bar"


# ── @gated decorator ─────────────────────────────────────────────────────

def test_gated_sync_raises_when_active():
    @gated("file write")
    def write_thing(x):
        return x * 2

    assert write_thing(3) == 6  # ok when off
    with plan_mode(True):
        with pytest.raises(PlanModeBlocked):
            write_thing(3)


def test_gated_sync_returns_default_when_blocked():
    @gated("optional fetch", return_on_block=None)
    def fetch():
        return "real result"
    with plan_mode(True):
        assert fetch() is None  # short-circuit, no raise


def test_gated_returns_default_with_non_none_sentinel():
    @gated("optional fetch", return_on_block={"skipped": True})
    def fetch():
        return "real"
    with plan_mode(True):
        assert fetch() == {"skipped": True}


@pytest.mark.asyncio
async def test_gated_async_raises_when_active():
    @gated("async call")
    async def afn(x):
        return x + 1

    assert await afn(1) == 2
    with plan_mode(True):
        with pytest.raises(PlanModeBlocked):
            await afn(1)


@pytest.mark.asyncio
async def test_gated_async_returns_default_when_blocked():
    @gated("async http", return_on_block={"ok": False})
    async def post():
        return {"ok": True}
    with plan_mode(True):
        assert await post() == {"ok": False}


# ── ChatState wires plan-mode flag ────────────────────────────────────────

def test_chat_state_default_does_not_enable_plan_mode():
    state = ChatState(workflow=make_workflow())  # noqa: F841
    assert is_plan_mode() is False


def test_chat_state_plan_mode_enables_global_flag():
    ChatState(workflow=make_workflow(), permission_mode="plan")
    assert is_plan_mode() is True


def test_chat_state_set_permission_mode_updates_flag():
    state = ChatState(workflow=make_workflow())
    state.set_permission_mode("plan")
    assert is_plan_mode() is True
    state.set_permission_mode("default")
    assert is_plan_mode() is False


def test_chat_state_does_not_clobber_global_plan_mode():
    """Regression: ``--plan`` sets the global flag, then ``chat_loop``
    constructs a default ``ChatState``. The constructor must NOT reset
    the flag back to False — that was the P2-1 bug."""
    set_plan_mode(True)
    state = ChatState(workflow=make_workflow())  # default permission_mode
    assert is_plan_mode() is True, "ChatState clobbered the global --plan flag"
    # And the state reconciled itself
    assert state.permission_mode == "plan"


def test_chat_state_default_off_does_not_force_global_off():
    """When permission_mode='default' and global already off, stays off."""
    set_plan_mode(False)
    state = ChatState(workflow=make_workflow())
    assert is_plan_mode() is False
    assert state.permission_mode == "default"


# ── ArtifactRegistry blocks writes ───────────────────────────────────────

def test_artifact_registry_blocks_in_plan_mode(tmp_path):
    from arc.memory.artifact_registry import ArtifactRegistry
    from arc.schemas.artifact import ArtifactDraft

    reg = ArtifactRegistry(root=str(tmp_path))
    draft = ArtifactDraft(
        name="test",
        description="x",
        files={"hello.txt": "hi"},
        metadata={},
    )

    # Off: works normally
    rec = reg.register(draft)
    assert rec is not None
    files_before = list(tmp_path.rglob("*"))
    assert files_before, "expected files to be created"

    # On: refuses
    with plan_mode(True):
        with pytest.raises(PlanModeBlocked):
            reg.register(draft)

    # No NEW artifact dir was created
    files_after = list(tmp_path.rglob("*"))
    # files_after may include the one from above; allow that, just ensure
    # we didn't double-register
    artifact_dirs_before = sum(1 for p in files_before if p.is_dir() and len(p.name) > 30)
    artifact_dirs_after = sum(1 for p in files_after if p.is_dir() and len(p.name) > 30)
    assert artifact_dirs_after == artifact_dirs_before


# ── _register_artifact_with_sim2l skips in plan mode ──────────────────────

@pytest.mark.asyncio
async def test_register_artifact_with_sim2l_skipped_in_plan_mode():
    """The sim2l-push helper must return a structured "skipped" result
    without touching the adapter."""
    from arc.chat.loop import _register_artifact_with_sim2l

    calls = []

    class Adapter:
        async def register_artifact(self, a):
            calls.append(a)
            return {"registered": True}

    workflow = SimpleNamespace(adapter=Adapter(), session_id="s")
    artifact = make_artifact()

    with plan_mode(True):
        result = await _register_artifact_with_sim2l(workflow, artifact)
    assert result is not None
    assert result["registered"] is False
    assert "plan" in result.get("error", "").lower()
    assert calls == [], "adapter must not have been called in plan mode"
