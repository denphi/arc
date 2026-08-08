import inspect

from typer.testing import CliRunner

from arc.cli.main import app


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _errors(result) -> str:
    """The CLI's error output, whichever click version is installed.

    ``arc skill validate`` writes failures to stderr (``typer.echo(..., err=True)``).
    Under click < 8.2 the runner is built with ``mix_stderr=False``, so
    ``result.output`` is stdout *only* and contains none of them; click >= 8.2
    dropped the parameter and merges the streams again. Asserting on
    ``result.output`` therefore passed on 8.2+ and failed on 8.1.x, which is
    what the environment here has. Read stderr when it's a separate stream.
    """
    try:
        return result.stderr
    except ValueError:
        # click >= 8.2: streams are merged, stderr isn't separately available.
        return result.output


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
    errors = _errors(result)
    assert "must declare description" in errors
    assert "must match skill name" in errors


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
    errors = _errors(result)
    assert "'description' must be a string" in errors
    assert "'allowed-tools' must be a string or list" in errors
    assert "unknown SKILL.md frontmatter fields: unexpected" in errors
