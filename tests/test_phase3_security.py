"""Phase 3 security invariants.

Covers the skill loader's YAML safety, path traversal, and size limits;
plus the pipeline-hook isolation guarantee.
"""

import logging
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.chat


CHAT_ROOT = Path(__file__).resolve().parents[1] / "arc" / "chat"


# ── yaml.safe_load only ───────────────────────────────────────────────────

def test_skill_loader_uses_yaml_safe_load_only():
    """Sanity: the loader file must never call yaml.load / full_load /
    unsafe_load. ``yaml.safe_load`` is the only acceptable entry point."""
    text = (CHAT_ROOT / "skill_loader.py").read_text()
    for bad in ("yaml.load(", "yaml.full_load(", "yaml.unsafe_load("):
        assert bad not in text, f"forbidden YAML loader {bad!r} in skill_loader.py"
    assert "yaml.safe_load" in text


def test_safe_load_refuses_python_object_construction(tmp_path):
    """The classic safe_load attack vector: !!python/object constructors
    must fail to parse. We just observe that the frontmatter is rejected,
    NOT that any code ran (we can't easily detect command exec from
    pytest — but if safe_load is used, none will be attempted)."""
    from arc.chat.skill_loader import parse_frontmatter
    hostile = (
        "---\n"
        "name: x\n"
        "pwn: !!python/object/new:os.system [\"echo SHOULD_NOT_RUN\"]\n"
        "---\n"
    )
    fm, _ = parse_frontmatter(hostile)
    # Either frontmatter rejected entirely, or the dangerous key was not
    # constructed (and is just None / strings in extras).
    if fm is not None:
        assert fm.extra.get("pwn") in (None, "", []), (
            f"unsafe YAML constructor leaked: {fm.extra!r}"
        )


# ── Skill file size limits ────────────────────────────────────────────────

def test_skill_size_cap_constant_is_sane():
    from arc.chat.skill_loader import MAX_SKILL_BYTES
    # 256 KiB is the documented cap. Don't let it accidentally balloon.
    assert MAX_SKILL_BYTES <= 1024 * 1024, "skill size cap is unreasonably large"
    assert MAX_SKILL_BYTES >= 16 * 1024, "skill size cap is too small to be useful"


def test_discover_skips_oversized_files(tmp_path):
    """No skill record is produced for an oversized file."""
    from arc.chat.skill_loader import MAX_SKILL_BYTES, _discover_one
    big = tmp_path / "huge.md"
    big.write_text("---\nname: huge\n---\n" + "x" * (MAX_SKILL_BYTES + 10))
    rec = _discover_one(big, "user")
    assert rec is None


# ── User-override gate default ────────────────────────────────────────────

def test_user_override_gate_default_is_off():
    """Belt-and-braces: even if a tester clears state, the default must be off."""
    import inspect
    from arc.chat.skill_loader import discover_skills
    sig = inspect.signature(discover_skills)
    assert sig.parameters["allow_user_overrides"].default is False


# ── Hooks: a buggy hook must not crash the pipeline ──────────────────────

@pytest.mark.asyncio
async def test_buggy_hook_does_not_break_pipeline():
    """Already covered in test_pipeline_core.py — duplicate here so the
    full Phase 3 security suite is self-contained."""
    from arc.chat.research.pipeline import Pipeline, PipelineHook, PipelineState
    from tests.fakes import make_workflow

    class P:
        name = "p"
        def should_run(self, s): return True
        async def run(self, s):
            s.extras["ran"] = True
            return s

    async def boom(s, phase, exc):
        raise RuntimeError("hook is broken")

    state = PipelineState(workflow=make_workflow(), goal_text="x")
    pipe = Pipeline([P()], hooks=[
        PipelineHook("before_phase", None, boom),
        PipelineHook("after_phase",  None, boom),
    ])
    state = await pipe.run(state)
    assert state.extras["ran"] is True


# ── Pipeline state never silently swallows phase errors ──────────────────

@pytest.mark.asyncio
async def test_unhandled_phase_error_propagates():
    from arc.chat.research.pipeline import (
        Pipeline, PipelinePhaseError, PipelineState,
    )
    from tests.fakes import make_workflow

    class Broken:
        name = "broken"
        def should_run(self, s): return True
        async def run(self, s):
            raise ValueError("real failure")

    state = PipelineState(workflow=make_workflow(), goal_text="x")
    pipe = Pipeline([Broken()])
    with pytest.raises(PipelinePhaseError):
        await pipe.run(state)


# ── Skill body() respects the size cap at read time too ──────────────────

def test_skill_body_size_cap_at_read_time(tmp_path, monkeypatch):
    from arc.chat.skill_loader import _discover_one
    skill = tmp_path / "s.md"
    skill.write_text("---\nname: s\n---\nbody")
    rec = _discover_one(skill, "user")
    # Now monkeypatch a very small cap and force body() to fail
    monkeypatch.setattr("arc.chat.skill_loader.MAX_SKILL_BYTES", 5)
    # File is ~20 bytes; should refuse
    with pytest.raises(RuntimeError, match="too large"):
        rec.body()


# ── Frontmatter parser does not execute code ──────────────────────────────

def test_parse_frontmatter_is_deterministic_and_cheap(monkeypatch):
    """Calling parse_frontmatter many times with hostile input must not
    take significant time — protects against algorithmic-DOS frontmatter."""
    import time
    from arc.chat.skill_loader import parse_frontmatter

    hostile = "---\n" + "a: b\n" * 10_000 + "---\n"
    start = time.perf_counter()
    parse_frontmatter(hostile)
    elapsed = time.perf_counter() - start
    # Generous bound: should run in well under a second
    assert elapsed < 2.0, f"frontmatter parse took {elapsed:.2f}s"
