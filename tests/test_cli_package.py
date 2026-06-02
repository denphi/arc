import inspect

from typer.testing import CliRunner

from arc.cli.main import app
from arc.core.loader import load_package
from arc.core.registry import ComponentRegistry


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def test_package_init_creates_loadable_local_package(tmp_path):
    target = tmp_path / "arc-my-lab"

    result = _runner().invoke(app, ["package", "init", "my-lab", str(target)])

    assert result.exit_code == 0, result.output
    assert (target / "package.yaml").exists()
    assert (target / "agents" / "ideator.py").exists()

    registry = ComponentRegistry()
    load_package(target, registry)

    assert "arc-my-lab" in registry.list_packages()
    assert "my_lab_ideator" in registry.list_agents()


def test_package_validate_accepts_scaffold(tmp_path):
    target = tmp_path / "arc-demo"
    init_result = _runner().invoke(app, ["package", "init", "demo", str(target)])
    assert init_result.exit_code == 0, init_result.output

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 0, result.output
    assert "OK: arc-demo" in result.stdout


def test_package_validate_rejects_missing_skill_file(tmp_path):
    """A declared skill whose file is missing must fail validation — the
    loader only logged the error before (review finding P3-1)."""
    target = tmp_path / "arc-bad-skill"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-bad-skill\n"
        "provides:\n"
        "  skills:\n"
        "    - skills/missing.md\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "skills/missing.md" in result.output
    assert "OK:" not in result.stdout


def test_package_validate_accepts_present_skill(tmp_path):
    """Positive control: a declared skill whose file exists validates OK."""
    target = tmp_path / "arc-good-skill"
    (target / "skills").mkdir(parents=True)
    (target / "skills" / "do-thing.md").write_text("# Do Thing\nA skill.\n", encoding="utf-8")
    (target / "package.yaml").write_text(
        "name: arc-good-skill\n"
        "provides:\n"
        "  skills:\n"
        "    - skills/do-thing.md\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 0, result.output
    assert "OK: arc-good-skill" in result.stdout


def test_package_validate_rejects_missing_declared_path(tmp_path):
    target = tmp_path / "arc-bad"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-bad\n"
        "provides:\n"
        "  agents:\n"
        "    - name: missing\n"
        "      path: agents/missing.py\n"
        "      class: MissingAgent\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "declared path does not exist" in result.output


def test_package_validate_rejects_strategy_missing_role(tmp_path):
    """A strategy that imports but omits required `role` is swallowed by the
    loader; validate must catch that it didn't register (review finding 3)."""
    target = tmp_path / "arc-no-role"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-no-role\n"
        "provides:\n"
        "  strategies:\n"
        "    - name: orphan_ideator\n"            # no `role:` → loader raises, swallows
        "      entrypoint: arc.packages.arc-sim2l.agents.ideator:IdeatorAgent\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "orphan_ideator" in result.output


def test_package_validate_rejects_contribution_failing_at_instantiation(tmp_path):
    """A report-section class that imports but raises in __init__ is swallowed
    by the loader; validate must fail (review finding 3)."""
    target = tmp_path / "arc-bad-report"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-bad-report\n"
        "provides:\n"
        "  report_sections:\n"
        "    - name: failing_report_section\n"
        "      section_name: failing\n"
        "      entrypoint: tests._audit_fixture:FailingReportSection\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "failing_report_section" in result.output


def test_package_validate_accepts_well_formed_strategy(tmp_path):
    """Positive control: a correctly-declared local strategy validates OK.

    A package strategy must reference a file *inside the package* (the
    resolver loads it lazily by file path), so use path + class — the same
    style `arc package init` scaffolds.
    """
    target = tmp_path / "arc-good"
    (target / "agents").mkdir(parents=True)
    (target / "agents" / "ideator.py").write_text(
        "from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent\n"
        "class GoodIdeator(IdeatorAgent):\n"
        "    name = 'good_ideator'\n",
        encoding="utf-8",
    )
    (target / "package.yaml").write_text(
        "name: arc-good\n"
        "provides:\n"
        "  strategies:\n"
        "    - role: ideator\n"
        "      name: good_ideator\n"
        "      path: agents/ideator.py\n"
        "      class: GoodIdeator\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 0, result.output
    assert "OK: arc-good" in result.stdout
