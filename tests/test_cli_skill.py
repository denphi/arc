import inspect

from typer.testing import CliRunner

from arc.cli.main import app


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _all_output(result) -> str:
    """stdout + stderr regardless of the installed click's mixing default."""
    try:
        stderr = result.stderr
    except (ValueError, AttributeError):
        stderr = ""
    return result.output + ("" if stderr in result.output else stderr)


def test_skill_validate_accepts_canonical_bundle(tmp_path):
    bundle = tmp_path / "demo-skill"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demonstrates validation.\n---\n# Demo\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["skill", "validate", str(bundle)])

    assert result.exit_code == 0, result.output
    assert "OK: demo-skill" in result.stdout


def test_skill_validate_rejects_invalid_bundle(tmp_path):
    bundle = tmp_path / "wrong"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\n# Demo\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["skill", "validate", str(bundle)])

    assert result.exit_code == 1
    assert "must declare description" in _all_output(result)
    assert "must match skill name" in _all_output(result)


def test_skill_validate_rejects_unknown_and_mistyped_frontmatter(tmp_path):
    bundle = tmp_path / "demo-skill"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: 42\n"
        "allowed-tools: 7\n"
        "unexpected: true\n"
        "---\n# Demo\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["skill", "validate", str(bundle)])

    assert result.exit_code == 1
    assert "'description' must be a string" in _all_output(result)
    assert "'allowed-tools' must be a string or list" in _all_output(result)
    assert "unknown SKILL.md frontmatter fields: unexpected" in _all_output(result)
