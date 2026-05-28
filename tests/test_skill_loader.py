"""Skill loader tests (Phase 3).

Covers:
  * Frontmatter parsing edge cases.
  * Discovery from builtin / user / project paths.
  * Lazy body loading.
  * Size cap and path-safety invariants.
  * Override gate (default off).
"""

from pathlib import Path

import pytest

from arc.chat.skill_loader import (
    MAX_SKILL_BYTES,
    SkillFrontmatter,
    SkillRecord,
    _discover_one,
    discover_skills,
    parse_frontmatter,
)


pytestmark = pytest.mark.chat


# ── parse_frontmatter ─────────────────────────────────────────────────────

def test_no_frontmatter_returns_none_and_full_body():
    text = "# heading\n\nbody"
    fm, body = parse_frontmatter(text)
    assert fm is None
    assert body == text


def test_well_formed_frontmatter_round_trips():
    text = (
        "---\n"
        "name: read-artifact\n"
        "description: Read an artifact record by ID\n"
        "user-invocable: true\n"
        "model: claude-haiku-4-5-20251001\n"
        "---\n"
        "# body content"
    )
    fm, body = parse_frontmatter(text)
    assert isinstance(fm, SkillFrontmatter)
    assert fm.name == "read-artifact"
    assert fm.description == "Read an artifact record by ID"
    assert fm.user_invocable is True
    assert fm.model == "claude-haiku-4-5-20251001"
    assert body.startswith("# body")


def test_underscored_field_names_also_accepted():
    text = (
        "---\n"
        "name: x\n"
        "user_invocable: true\n"
        "disable_model_invocation: true\n"
        "argument_hint: \"<id>\"\n"
        "---\n"
    )
    fm, _ = parse_frontmatter(text)
    assert fm.user_invocable is True
    assert fm.disable_model_invocation is True
    assert fm.argument_hint == "<id>"


def test_missing_name_is_rejected(caplog):
    text = "---\ndescription: nameless\n---\n"
    fm, _ = parse_frontmatter(text)
    assert fm is None
    assert any("missing required field 'name'" in r.message for r in caplog.records)


def test_malformed_yaml_returns_none_does_not_raise(caplog):
    text = "---\nthis: is: not: valid: yaml\n  - mixed: types\n---\nbody"
    fm, body = parse_frontmatter(text)
    assert fm is None
    # Body must still be returned (skill might be usable without frontmatter)
    assert body == "body"


def test_unclosed_frontmatter_treated_as_no_frontmatter():
    text = "---\nname: x\ndescription: oops never closed\n# body here"
    fm, body = parse_frontmatter(text)
    assert fm is None
    assert body == text  # entire string returned untouched


def test_frontmatter_with_non_mapping_root_rejected(caplog):
    text = "---\n- just\n- a\n- list\n---\nbody"
    fm, body = parse_frontmatter(text)
    assert fm is None
    assert body == "body"


def test_extra_fields_preserved():
    text = (
        "---\n"
        "name: x\n"
        "custom_key: custom_value\n"
        "another: 42\n"
        "---\n"
    )
    fm, _ = parse_frontmatter(text)
    assert fm.extra == {"custom_key": "custom_value", "another": 42}


def test_safe_load_used_not_full_load():
    """Hostile YAML constructors must not be executed."""
    # Construct a string that would execute under yaml.full_load but
    # safely fail to parse under yaml.safe_load. We use !!python/object/new
    # which safe_load refuses to construct.
    text = (
        "---\n"
        "name: x\n"
        "danger: !!python/object/new:os.system [\"echo pwned\"]\n"
        "---\n"
    )
    fm, _ = parse_frontmatter(text)
    # safe_load raises ConstructorError → we return None for the whole
    # frontmatter. Either outcome is fine; the critical assertion is
    # that no command was executed.
    assert fm is None or "danger" not in fm.extra or fm.extra.get("danger") is None


# ── _discover_one ─────────────────────────────────────────────────────────

def test_discover_one_returns_none_for_nonexistent(tmp_path):
    assert _discover_one(tmp_path / "missing.md", "user") is None


def test_discover_one_skips_oversized(tmp_path, caplog):
    big = tmp_path / "huge.md"
    big.write_text("x" * (MAX_SKILL_BYTES + 1))
    rec = _discover_one(big, "user")
    assert rec is None
    assert any("oversized" in r.message for r in caplog.records)


def test_discover_one_handles_missing_frontmatter_with_description_section(tmp_path):
    skill = tmp_path / "noheader.md"
    skill.write_text(
        "# noheader\n\n## Description\nA skill without frontmatter\n\n## Steps\n- a\n"
    )
    rec = _discover_one(skill, "builtin")
    assert rec is not None
    assert rec.name == "noheader"
    assert "without frontmatter" in rec.description
    assert rec.frontmatter is None


def test_discover_one_records_source(tmp_path):
    skill = tmp_path / "s.md"
    skill.write_text("---\nname: s\n---\n")
    rec = _discover_one(skill, "project")
    assert rec.source == "project"


# ── Lazy body loading ─────────────────────────────────────────────────────

def test_body_returns_just_the_body(tmp_path):
    skill = tmp_path / "s.md"
    skill.write_text(
        "---\nname: s\ndescription: x\n---\n# this is the body\nlines\n"
    )
    rec = _discover_one(skill, "user")
    body = rec.body()
    assert body.startswith("# this is the body")
    assert "name: s" not in body  # frontmatter stripped


def test_body_size_cap_enforced(tmp_path, monkeypatch):
    skill = tmp_path / "s.md"
    skill.write_text("---\nname: s\n---\nbody content")
    rec = _discover_one(skill, "user")

    # Now grow the file past the cap and ensure body() refuses.
    monkeypatch.setattr("arc.chat.skill_loader.MAX_SKILL_BYTES", 10)
    with pytest.raises(RuntimeError, match="too large"):
        rec.body()


# ── discover_skills (full search-path merge) ──────────────────────────────

def test_discover_builds_full_map(tmp_path, monkeypatch):
    # Set up a fake user-skills dir
    user_dir = tmp_path / "config" / "arc" / "skills"
    user_dir.mkdir(parents=True)
    (user_dir / "user-skill.md").write_text(
        "---\nname: user-skill\ndescription: from user\n---\nbody\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Project: chdir to a controlled dir
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".arc").mkdir()
    (project_dir / ".arc" / "skills").mkdir()
    (project_dir / ".arc" / "skills" / "project-skill.md").write_text(
        "---\nname: project-skill\ndescription: from project\n---\n"
    )
    monkeypatch.chdir(project_dir)
    # Project-local skills are off by default; opt in for this test.
    monkeypatch.setenv("ARC_TRUST_PROJECT_SKILLS", "1")

    skills = discover_skills()
    assert "user-skill" in skills
    assert "project-skill" in skills
    assert skills["user-skill"].source == "user"
    assert skills["project-skill"].source == "project"


def test_project_skills_default_off(tmp_path, monkeypatch, caplog):
    """Regression for P3-3: a .arc/skills dir in CWD must NOT auto-load
    unless ARC_TRUST_PROJECT_SKILLS=1."""
    project_dir = tmp_path / "untrusted"
    project_dir.mkdir()
    (project_dir / ".arc").mkdir()
    (project_dir / ".arc" / "skills").mkdir()
    (project_dir / ".arc" / "skills" / "evil.md").write_text(
        "---\nname: evil\ndescription: should NOT load\n---\nbody\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    monkeypatch.delenv("ARC_TRUST_PROJECT_SKILLS", raising=False)
    monkeypatch.chdir(project_dir)

    skills = discover_skills()
    assert "evil" not in skills, "project skills auto-loaded without opt-in"
    # Warning should be visible so users know about the skipped skills
    assert any("disabled by default" in r.message for r in caplog.records)


def test_user_override_blocked_by_default(tmp_path, monkeypatch, caplog):
    """User skill with same name as builtin is dropped by default."""
    # Pick a builtin name from arc/skills/core
    builtin_dir = Path(__file__).resolve().parents[1] / "arc" / "skills" / "core"
    if not builtin_dir.exists() or not list(builtin_dir.glob("*.md")):
        pytest.skip("no built-in skills shipped")
    # Pick the first builtin name (without parsing — file stem is the fallback)
    candidate = sorted(builtin_dir.glob("*.md"))[0]
    builtin_text = candidate.read_text()
    # Best guess at name: if frontmatter, use 'name'; else use stem
    fm, _ = parse_frontmatter(builtin_text)
    builtin_name = fm.name if fm else candidate.stem

    user_dir = tmp_path / "config" / "arc" / "skills"
    user_dir.mkdir(parents=True)
    (user_dir / "shadow.md").write_text(
        f"---\nname: {builtin_name}\ndescription: shadowing builtin\n---\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)  # no project dir

    skills = discover_skills(allow_user_overrides=False)
    assert builtin_name in skills
    assert skills[builtin_name].source == "builtin"
    assert any("overrides built-in" in r.message for r in caplog.records)


def test_user_override_allowed_when_opted_in(tmp_path, monkeypatch):
    builtin_dir = Path(__file__).resolve().parents[1] / "arc" / "skills" / "core"
    if not builtin_dir.exists() or not list(builtin_dir.glob("*.md")):
        pytest.skip("no built-in skills shipped")
    candidate = sorted(builtin_dir.glob("*.md"))[0]
    fm, _ = parse_frontmatter(candidate.read_text())
    builtin_name = fm.name if fm else candidate.stem

    user_dir = tmp_path / "config" / "arc" / "skills"
    user_dir.mkdir(parents=True)
    (user_dir / "shadow.md").write_text(
        f"---\nname: {builtin_name}\ndescription: shadowing builtin\n---\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)

    skills = discover_skills(allow_user_overrides=True)
    assert skills[builtin_name].source == "user"
    assert skills[builtin_name].description == "shadowing builtin"


def test_extra_dirs_are_discovered(tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "extra-skill.md").write_text(
        "---\nname: extra-skill\ndescription: extra\n---\n"
    )
    # Isolate from user/project paths
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    monkeypatch.chdir(tmp_path)

    skills = discover_skills(extra_dirs=[extra])
    assert "extra-skill" in skills


# ── Available_to_* helpers ────────────────────────────────────────────────

def test_user_invocable_routing(tmp_path):
    skill = tmp_path / "s.md"
    skill.write_text("---\nname: s\nuser-invocable: true\n---\n")
    rec = _discover_one(skill, "user")
    assert rec.available_to_user() is True
    # Default model invocation allowed
    assert rec.available_to_model() is True


def test_disable_model_invocation_routing(tmp_path):
    skill = tmp_path / "s.md"
    skill.write_text("---\nname: s\ndisable-model-invocation: true\n---\n")
    rec = _discover_one(skill, "user")
    assert rec.available_to_model() is False
    assert rec.available_to_user() is False  # user-invocable defaulted off
