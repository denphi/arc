"""Coverage for the read-only ``/clusters`` and ``/skills`` commands.

Both surface artifacts other parts of the loop already write:

  * ``/clusters`` reads ``memory['failure_clusters']``, which the
    failure-clustering reflector populates.
  * ``/skills`` reads files under ``<session-dir>/skills/learned/``,
    which the skill-extracting reflector writes.

Tests use the global pytest fixture that redirects ``SIM2L_HOME`` so
all writes are sandboxed.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.chat


# ── Fixtures ───────────────────────────────────────────────────────────


def _state(memory=None, *, session_id="test-cluskill"):
    """Build a ChatState with a workflow whose memory we control."""
    from arc.chat.state import ChatState
    from tests.fakes import make_workflow
    return ChatState(workflow=make_workflow(memory=memory, session_id=session_id))


# ── /clusters: empty state ─────────────────────────────────────────────


def test_clusters_command_empty_shows_tip(capsys):
    from arc.chat.commands.clusters import run

    asyncio.run(run(_state(), []))
    out = capsys.readouterr().out
    assert "No failure clusters" in out
    assert "/strategy reflector failure_clustering" in out


def test_clusters_command_invalid_memory_shape_is_treated_as_empty(capsys):
    """A non-list value under 'failure_clusters' must not crash."""
    from arc.chat.commands.clusters import run

    asyncio.run(run(_state(memory={"failure_clusters": "not a list"}), []))
    out = capsys.readouterr().out
    assert "No failure clusters" in out


# ── /clusters: listing ─────────────────────────────────────────────────


def _cluster(signature, *, count, reason=None, entries=None):
    return {
        "signature": signature,
        "count": count,
        "reason": reason or signature,
        "entries": entries or [{}] * min(count, 3),
    }


def test_clusters_command_lists_signatures_and_counts(capsys):
    from arc.chat.commands.clusters import run

    state = _state(memory={
        "failure_clusters": [
            _cluster("all-numeric-outputs-nan", count=4,
                     reason="every numeric output was NaN"),
            _cluster("far-from-target", count=2),
        ],
    })
    asyncio.run(run(state, []))
    out = capsys.readouterr().out
    assert "all-numeric-outputs-nan" in out
    assert "far-from-target" in out
    assert "4" in out
    assert "2" in out
    # The reason should be surfaced next to the signature.
    assert "every numeric output was NaN" in out


def test_clusters_command_warns_when_reflector_not_active(capsys):
    """If the user has a non-clustering reflector active, the listing
    should still print but warn the data is stale."""
    from arc.chat.commands.clusters import run

    state = _state(memory={
        "failure_clusters": [_cluster("x", count=3)],
        "strategy_overrides": {"reflector": "default"},
    })
    asyncio.run(run(state, []))
    out = capsys.readouterr().out
    assert "previous reflector" in out.lower() or "active reflector" in out.lower()


# ── /clusters: drill-down ──────────────────────────────────────────────


def test_clusters_command_drills_into_specific_signature(capsys):
    from arc.chat.commands.clusters import run

    state = _state(memory={
        "failure_clusters": [
            _cluster("scf-fail", count=3, reason="SCF did not converge",
                     entries=[
                         {"inputs": {"x": 1}, "status": "failed"},
                         {"inputs": {"x": 2}, "status": "failed"},
                     ]),
        ],
    })
    asyncio.run(run(state, ["scf-fail"]))
    out = capsys.readouterr().out
    assert "Cluster: scf-fail" in out
    assert "SCF did not converge" in out
    # Sample entries from the cluster appear in the output (JSON-rendered).
    assert "\"status\": \"failed\"" in out


def test_clusters_command_prefix_match(capsys):
    from arc.chat.commands.clusters import run

    state = _state(memory={
        "failure_clusters": [
            _cluster("DivergenceError: SCF did not converge", count=2),
        ],
    })
    asyncio.run(run(state, ["DivergenceError"]))
    out = capsys.readouterr().out
    assert "Cluster:" in out
    assert "DivergenceError" in out


def test_clusters_command_ambiguous_prefix_lists_options(capsys):
    from arc.chat.commands.clusters import run

    state = _state(memory={
        "failure_clusters": [
            _cluster("DivergenceError: A", count=2),
            _cluster("DivergenceError: B", count=2),
        ],
    })
    asyncio.run(run(state, ["DivergenceError"]))
    out = capsys.readouterr().out
    assert "multiple signatures" in out.lower() or "ambig" in out.lower()
    assert "DivergenceError: A" in out
    assert "DivergenceError: B" in out


def test_clusters_command_unknown_signature(capsys):
    from arc.chat.commands.clusters import run

    state = _state(memory={
        "failure_clusters": [_cluster("scf-fail", count=2)],
    })
    asyncio.run(run(state, ["something-else"]))
    out = capsys.readouterr().out
    assert "No cluster with signature" in out


# ── /skills: empty state ───────────────────────────────────────────────


def test_skills_command_empty_shows_tip(capsys):
    from arc.chat.commands.skills import run

    state = _state(session_id="test-skills-empty")
    asyncio.run(run(state, []))
    out = capsys.readouterr().out
    assert "No skills" in out
    assert "/strategy reflector skill_extracting" in out


# ── /skills: listing real files ────────────────────────────────────────


def _skills_dir(session_id: str) -> Path:
    """Resolve <SIM2L_HOME>/<session>/skills/learned/, creating it."""
    base = Path(os.environ["SIM2L_HOME"]) / session_id / "skills" / "learned"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_skill(session_id, filename, body):
    path = _skills_dir(session_id) / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_skills_command_lists_files_and_h1(capsys):
    from arc.chat.commands.skills import run

    _write_skill(
        "test-skills-list",
        "design-silicon-abc12345.md",
        "# learned_skill: design-silicon\n\nbody...",
    )
    _write_skill(
        "test-skills-list",
        "iron-oxide-def67890.md",
        "# learned_skill: iron-oxide\n\nbody...",
    )

    state = _state(session_id="test-skills-list")
    asyncio.run(run(state, []))
    out = capsys.readouterr().out
    assert "design-silicon-abc12345" in out
    assert "iron-oxide-def67890" in out
    assert "learned_skill: design-silicon" in out


def test_skills_command_show_prints_body(capsys):
    from arc.chat.commands.skills import run

    _write_skill(
        "test-skills-show",
        "skill1-abc12345.md",
        "# learned_skill: skill1\n\n## What worked\n- great stuff\n",
    )

    state = _state(session_id="test-skills-show")
    asyncio.run(run(state, ["show", "skill1-abc12345"]))
    out = capsys.readouterr().out
    assert "Skill: skill1-abc12345" in out
    assert "What worked" in out
    assert "great stuff" in out


def test_skills_command_show_accepts_unique_prefix(capsys):
    """``/skills show design-silicon`` should resolve when only one
    file starts with that prefix."""
    from arc.chat.commands.skills import run

    _write_skill(
        "test-skills-prefix",
        "design-silicon-abc12345.md",
        "# learned_skill: design-silicon\n\nbody",
    )

    state = _state(session_id="test-skills-prefix")
    asyncio.run(run(state, ["show", "design-silicon"]))
    out = capsys.readouterr().out
    assert "design-silicon-abc12345" in out


def test_skills_command_show_ambiguous_prefix_lists_options(capsys):
    from arc.chat.commands.skills import run

    _write_skill("test-skills-ambig", "design-silicon-aaa.md", "# x\n")
    _write_skill("test-skills-ambig", "design-silicon-bbb.md", "# y\n")

    state = _state(session_id="test-skills-ambig")
    asyncio.run(run(state, ["show", "design-silicon"]))
    out = capsys.readouterr().out
    assert "Ambiguous" in out
    assert "design-silicon-aaa" in out
    assert "design-silicon-bbb" in out


def test_skills_command_show_unknown_name(capsys):
    from arc.chat.commands.skills import run

    _skills_dir("test-skills-missing")  # touch dir so listing finds it

    state = _state(session_id="test-skills-missing")
    asyncio.run(run(state, ["show", "nothing-like-this"]))
    out = capsys.readouterr().out
    assert "No skill matching" in out


# ── /skills: deletion ─────────────────────────────────────────────────


def test_skills_command_delete_requires_confirmation_no():
    """Pressing 'n' (or anything other than y) cancels the delete."""
    from arc.chat.commands.skills import run

    path = _write_skill(
        "test-skills-delete-no",
        "doomed-abc.md",
        "# learned_skill: doomed\n\nbody",
    )

    async def _fake_input(prompt: str) -> str:
        return "n"

    with patch("arc.chat.commands.skills.chat_input_async", _fake_input):
        state = _state(session_id="test-skills-delete-no")
        asyncio.run(run(state, ["delete", "doomed-abc"]))

    assert path.exists()  # not deleted


def test_skills_command_delete_confirms_yes():
    """`y` confirms and the file is removed."""
    from arc.chat.commands.skills import run

    path = _write_skill(
        "test-skills-delete-yes",
        "doomed-abc.md",
        "# learned_skill: doomed\n\nbody",
    )

    async def _fake_input(prompt: str) -> str:
        return "y"

    with patch("arc.chat.commands.skills.chat_input_async", _fake_input):
        state = _state(session_id="test-skills-delete-yes")
        asyncio.run(run(state, ["delete", "doomed-abc"]))

    assert not path.exists()


def test_skills_command_delete_unknown_name(capsys):
    from arc.chat.commands.skills import run

    _skills_dir("test-skills-delete-missing")
    state = _state(session_id="test-skills-delete-missing")
    asyncio.run(run(state, ["delete", "no-such-skill"]))
    out = capsys.readouterr().out
    assert "No skill matching" in out


# ── Registration sanity ───────────────────────────────────────────────


def test_clusters_and_skills_are_registered():
    """Both commands appear in the canonical registry."""
    from arc.chat.commands import build_registry
    reg = build_registry()
    assert reg.get("clusters") is not None
    assert reg.get("skills") is not None


def test_help_lines_include_clusters_and_skills():
    """Help renders without crashing and mentions both new commands."""
    from arc.chat.commands import build_registry
    from arc.chat.registry import format_help_lines

    reg = build_registry()
    lines = "\n".join(format_help_lines(reg))
    assert "/clusters" in lines
    assert "/skills" in lines


# ── /skills export ─────────────────────────────────────────────────────


def _export_target_dir():
    """Default export target matches the command's own default."""
    return Path(os.environ["SIM2L_HOME"]) / "shared" / "skills"


def test_skills_export_default_dir(tmp_path, capsys):
    """``/skills export`` with no args writes under ``SIM2L_HOME/shared/skills``."""
    from arc.chat.commands.skills import run

    src = _write_skill(
        "test-export-default",
        "alpha-aaa.md",
        "# learned_skill: alpha\nbody\n",
    )
    state = _state(session_id="test-export-default")

    asyncio.run(run(state, ["export"]))
    target = _export_target_dir()
    assert (target / "alpha-aaa.md").exists()
    assert (target / "alpha-aaa.md").read_text() == src.read_text()
    out = capsys.readouterr().out
    assert "Exported to" in out


def test_skills_export_to_custom_dir(tmp_path, capsys):
    from arc.chat.commands.skills import run

    _write_skill(
        "test-export-custom",
        "beta-bbb.md",
        "# learned_skill: beta\nbody\n",
    )
    state = _state(session_id="test-export-custom")
    target = tmp_path / "user-shared"

    asyncio.run(run(state, ["export", str(target)]))
    assert (target / "beta-bbb.md").exists()


def test_skills_export_skips_identical_files(tmp_path, capsys):
    """Re-running export when nothing changed is a no-op."""
    from arc.chat.commands.skills import run

    _write_skill(
        "test-export-idempotent",
        "gamma-ccc.md",
        "# learned_skill: gamma\nbody\n",
    )
    state = _state(session_id="test-export-idempotent")
    target = tmp_path / "shared"

    asyncio.run(run(state, ["export", str(target)]))
    capsys.readouterr()  # clear

    asyncio.run(run(state, ["export", str(target)]))
    out = capsys.readouterr().out
    assert "1 identical" in out or "identical" in out


def test_skills_export_warns_on_conflict_without_force(tmp_path, capsys):
    """Different content at the destination → skip + warn."""
    from arc.chat.commands.skills import run

    _write_skill(
        "test-export-conflict",
        "delta-ddd.md",
        "# learned_skill: delta\nsession version\n",
    )
    state = _state(session_id="test-export-conflict")
    target = tmp_path / "shared"
    target.mkdir()
    (target / "delta-ddd.md").write_text(
        "# learned_skill: delta\nDIFFERENT shared version\n",
        encoding="utf-8",
    )

    asyncio.run(run(state, ["export", str(target)]))
    out = capsys.readouterr().out
    assert "Conflict" in out or "conflict" in out.lower()
    # The shared file was not touched.
    assert "DIFFERENT shared version" in (target / "delta-ddd.md").read_text()


def test_skills_export_overwrites_with_force(tmp_path):
    from arc.chat.commands.skills import run

    _write_skill(
        "test-export-force",
        "epsilon-eee.md",
        "# learned_skill: epsilon\nsession version\n",
    )
    state = _state(session_id="test-export-force")
    target = tmp_path / "shared"
    target.mkdir()
    (target / "epsilon-eee.md").write_text(
        "# learned_skill: epsilon\nOLD shared version\n",
        encoding="utf-8",
    )

    asyncio.run(run(state, ["export", str(target), "--force"]))
    assert (target / "epsilon-eee.md").read_text() == (
        "# learned_skill: epsilon\nsession version\n"
    )


def test_skills_export_warns_when_no_skills(tmp_path, capsys):
    """Empty session → warn, don't write anything to the target."""
    from arc.chat.commands.skills import run

    _skills_dir("test-export-empty")  # create dir but no files
    state = _state(session_id="test-export-empty")
    target = tmp_path / "shared"

    asyncio.run(run(state, ["export", str(target)]))
    out = capsys.readouterr().out
    assert "No skills" in out
    # Target may or may not be created — either way it should be empty.
    if target.exists():
        assert list(target.iterdir()) == []


def test_skills_export_appears_in_help():
    from arc.chat.commands import build_registry
    from arc.chat.registry import format_help_lines

    lines = "\n".join(format_help_lines(build_registry()))
    assert "/skills" in lines
    assert "export" in lines


# ── /skills import ─────────────────────────────────────────────────────


def _seed_shared(target: Path, files: dict[str, str]) -> None:
    """Populate a directory with skill markdowns for an import test."""
    target.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (target / name).write_text(body, encoding="utf-8")


def _session_learned(session_id: str) -> Path:
    return Path(os.environ["SIM2L_HOME"]) / session_id / "skills" / "learned"


def test_skills_import_default_source(tmp_path, capsys):
    """``/skills import`` with no args reads from ``SIM2L_HOME/shared/skills``."""
    from arc.chat.commands.skills import run

    # Seed the default shared dir.
    _seed_shared(
        _export_target_dir(),
        {"alpha-aaa.md": "# learned_skill: alpha\nbody\n"},
    )
    state = _state(session_id="test-import-default")

    asyncio.run(run(state, ["import"]))

    learned = _session_learned("test-import-default")
    assert (learned / "alpha-aaa.md").exists()
    out = capsys.readouterr().out
    assert "Imported into" in out
    assert "1 new" in out


def test_skills_import_from_custom_source(tmp_path):
    from arc.chat.commands.skills import run

    src = tmp_path / "library"
    _seed_shared(src, {"beta-bbb.md": "# learned_skill: beta\nbody\n"})

    state = _state(session_id="test-import-custom")
    asyncio.run(run(state, ["import", str(src)]))

    learned = _session_learned("test-import-custom")
    assert (learned / "beta-bbb.md").exists()


def test_skills_import_creates_learned_dir_for_new_session(tmp_path):
    """A session that has never run the skill reflector still gets a
    ``learned/`` dir on first import — no setup dance required."""
    from arc.chat.commands.skills import run

    src = tmp_path / "library"
    _seed_shared(src, {"gamma-ccc.md": "# learned_skill: gamma\nbody\n"})

    state = _state(session_id="test-import-new")
    # Sanity: directory does not exist yet.
    assert not _session_learned("test-import-new").exists()

    asyncio.run(run(state, ["import", str(src)]))
    assert _session_learned("test-import-new").exists()


def test_skills_import_skips_identical_files(tmp_path, capsys):
    """Re-importing the same files is a no-op."""
    from arc.chat.commands.skills import run

    src = tmp_path / "library"
    _seed_shared(src, {"delta-ddd.md": "# learned_skill: delta\nbody\n"})

    state = _state(session_id="test-import-idempotent")
    asyncio.run(run(state, ["import", str(src)]))
    capsys.readouterr()

    asyncio.run(run(state, ["import", str(src)]))
    out = capsys.readouterr().out
    assert "1 identical" in out or "identical" in out


def test_skills_import_warns_on_conflict_without_force(tmp_path, capsys):
    """Different content in this session → skip + warn, session wins."""
    from arc.chat.commands.skills import run

    _write_skill(
        "test-import-conflict",
        "epsilon-eee.md",
        "# learned_skill: epsilon\nsession version\n",
    )
    src = tmp_path / "library"
    _seed_shared(
        src,
        {"epsilon-eee.md": "# learned_skill: epsilon\nSHARED version\n"},
    )

    state = _state(session_id="test-import-conflict")
    asyncio.run(run(state, ["import", str(src)]))

    out = capsys.readouterr().out
    assert "Conflict" in out or "conflict" in out.lower()
    # Session-local file unchanged.
    assert "session version" in (
        _session_learned("test-import-conflict") / "epsilon-eee.md"
    ).read_text()


def test_skills_import_overwrites_with_force(tmp_path):
    from arc.chat.commands.skills import run

    _write_skill(
        "test-import-force",
        "zeta-fff.md",
        "# learned_skill: zeta\nold session version\n",
    )
    src = tmp_path / "library"
    _seed_shared(
        src,
        {"zeta-fff.md": "# learned_skill: zeta\nnew shared version\n"},
    )

    state = _state(session_id="test-import-force")
    asyncio.run(run(state, ["import", str(src), "--force"]))

    assert "new shared version" in (
        _session_learned("test-import-force") / "zeta-fff.md"
    ).read_text()


def test_skills_import_warns_when_source_missing(tmp_path, capsys):
    from arc.chat.commands.skills import run

    state = _state(session_id="test-import-no-source")
    asyncio.run(run(state, ["import", str(tmp_path / "does-not-exist")]))

    out = capsys.readouterr().out
    assert "does not exist" in out.lower()


def test_skills_import_warns_when_source_empty(tmp_path, capsys):
    """A real but empty directory should produce a clear message."""
    from arc.chat.commands.skills import run

    src = tmp_path / "library"
    src.mkdir()

    state = _state(session_id="test-import-empty-src")
    asyncio.run(run(state, ["import", str(src)]))

    out = capsys.readouterr().out
    assert "No skills found" in out


def test_skills_export_then_import_round_trip(tmp_path):
    """End-to-end: export from session A, import into session B."""
    from arc.chat.commands.skills import run

    _write_skill(
        "test-round-trip-A",
        "eta-ggg.md",
        "# learned_skill: eta\nshared body\n",
    )
    shared = tmp_path / "shared"

    state_a = _state(session_id="test-round-trip-A")
    asyncio.run(run(state_a, ["export", str(shared)]))

    state_b = _state(session_id="test-round-trip-B")
    asyncio.run(run(state_b, ["import", str(shared)]))

    learned_b = _session_learned("test-round-trip-B") / "eta-ggg.md"
    assert learned_b.exists()
    assert "shared body" in learned_b.read_text()


def test_skills_import_appears_in_help():
    from arc.chat.commands import build_registry
    from arc.chat.registry import format_help_lines

    lines = "\n".join(format_help_lines(build_registry()))
    assert "/skills" in lines
    assert "import" in lines
