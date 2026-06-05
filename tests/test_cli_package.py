import inspect

import pytest
from typer.testing import CliRunner

from arc.cli.main import app
from arc.core.loader import _import_from_file, load_package
from arc.core.registry import ComponentRegistry


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _all_output(result):
    return (result.output or "") + (getattr(result, "stderr", "") or "")


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
    assert "skills/missing.md" in _all_output(result)
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


def test_package_validate_rejects_workflow_missing_skill_reference(tmp_path):
    target = tmp_path / "arc-bad-workflow"
    (target / "workflows").mkdir(parents=True)
    (target / "workflows" / "loop.yaml").write_text(
        "name: loop\n"
        "steps:\n"
        "  - id: missing\n"
        "    skill: missing-skill\n"
        "    input: {}\n",
        encoding="utf-8",
    )
    (target / "package.yaml").write_text(
        "name: arc-bad-workflow\n"
        "provides:\n"
        "  workflows:\n"
        "    - name: loop\n"
        "      path: workflows/loop.yaml\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "missing-skill" in _all_output(result)


def test_package_loads_skill_bundle_names_from_frontmatter(tmp_path):
    target = tmp_path / "arc-skill-bundles"
    for skill_name in ("first-skill", "second-skill"):
        bundle = target / "skills" / skill_name
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text(
            "---\n"
            f"name: {skill_name}\n"
            f"description: {skill_name} description\n"
            "---\n\n"
            f"# {skill_name}\n",
            encoding="utf-8",
        )
    (target / "package.yaml").write_text(
        "name: arc-skill-bundles\n"
        "provides:\n"
        "  skills:\n"
        "    - skills/first-skill/SKILL.md\n"
        "    - skills/second-skill/SKILL.md\n",
        encoding="utf-8",
    )

    registry = ComponentRegistry()
    load_package(target, registry)

    assert set(registry.list_skills()) == {"first-skill", "second-skill"}

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 0, result.output
    assert "OK: arc-skill-bundles" in result.stdout


def test_package_validate_accepts_explicit_skill_manifest_name(tmp_path):
    target = tmp_path / "arc-explicit-skill"
    (target / "skills" / "bundle").mkdir(parents=True)
    (target / "skills" / "bundle" / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    (target / "package.yaml").write_text(
        "name: arc-explicit-skill\n"
        "provides:\n"
        "  skills:\n"
        "    - name: explicit-skill\n"
        "      path: skills/bundle/SKILL.md\n",
        encoding="utf-8",
    )

    registry = ComponentRegistry()
    load_package(target, registry)

    assert registry.list_skills() == ["explicit-skill"]

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 0, result.output
    assert "OK: arc-explicit-skill" in result.stdout


def test_package_validate_rejects_duplicate_resolved_skill_names(tmp_path):
    target = tmp_path / "arc-duplicate-skills"
    for path in ("skills/a", "skills/b"):
        bundle = target / path
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text(
            "---\nname: duplicate-skill\n---\n\n# Duplicate\n",
            encoding="utf-8",
        )
    (target / "package.yaml").write_text(
        "name: arc-duplicate-skills\n"
        "provides:\n"
        "  skills:\n"
        "    - skills/a/SKILL.md\n"
        "    - skills/b/SKILL.md\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "duplicate skill name after resolution: duplicate-skill" in _all_output(result)


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
    assert "declared path does not exist" in _all_output(result)


def test_load_package_records_component_errors(tmp_path):
    target = tmp_path / "arc-partial"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-partial\n"
        "provides:\n"
        "  agents:\n"
        "    - name: missing\n"
        "      path: agents/missing.py\n"
        "      class: MissingAgent\n",
        encoding="utf-8",
    )

    registry = ComponentRegistry()
    load_package(target, registry)

    assert registry.list_packages() == ["arc-partial"]
    assert registry.list_load_errors() == [{
        "package": "arc-partial",
        "kind": "agent",
        "name": "missing",
        "error": "Cannot load 'MissingAgent'; file does not exist: "
        f"{target / 'agents' / 'missing.py'}",
    }]


def test_import_from_file_uses_unique_module_names_for_same_layout(tmp_path):
    first = tmp_path / "one" / "agents"
    second = tmp_path / "two" / "agents"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "coder.py").write_text("class Marker:\n    value = 'one'\n", encoding="utf-8")
    (second / "coder.py").write_text("class Marker:\n    value = 'two'\n", encoding="utf-8")

    first_cls = _import_from_file(first / "coder.py", "Marker")
    second_cls = _import_from_file(second / "coder.py", "Marker")

    assert first_cls.value == "one"
    assert second_cls.value == "two"


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
    assert "orphan_ideator" in _all_output(result)


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
    assert "failing_report_section" in _all_output(result)


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


def test_package_validate_accepts_declared_loader(tmp_path):
    target = tmp_path / "arc-loader"
    (target / "loaders").mkdir(parents=True)
    (target / "loaders" / "demo.py").write_text(
        "class DemoLoader:\n"
        "    name = 'demo_loader'\n",
        encoding="utf-8",
    )
    (target / "package.yaml").write_text(
        "name: arc-loader\n"
        "provides:\n"
        "  loaders:\n"
        "    - name: demo_loader\n"
        "      path: loaders/demo.py\n"
        "      class: DemoLoader\n",
        encoding="utf-8",
    )

    registry = ComponentRegistry()
    load_package(target, registry)

    assert registry.list_loaders() == ["demo_loader"]
    assert registry.get_loader("demo_loader").__name__ == "DemoLoader"

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 0, result.output
    assert "OK: arc-loader" in result.stdout


def test_package_validate_rejects_missing_declared_loader(tmp_path):
    target = tmp_path / "arc-bad-loader"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-bad-loader\n"
        "provides:\n"
        "  loaders:\n"
        "    - name: missing_loader\n"
        "      path: loaders/missing.py\n"
        "      class: MissingLoader\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "missing_loader" in _all_output(result) or "loaders/missing.py" in _all_output(result)


def test_package_loader_honours_disabled_package_filter(tmp_path):
    target = tmp_path / "arc-loader-disable"
    (target / "loaders").mkdir(parents=True)
    (target / "loaders" / "demo.py").write_text("class DemoLoader: pass\n", encoding="utf-8")
    (target / "package.yaml").write_text(
        "name: arc-loader-disable\n"
        "provides:\n"
        "  loaders:\n"
        "    - name: demo_loader\n"
        "      path: loaders/demo.py\n"
        "      class: DemoLoader\n",
        encoding="utf-8",
    )
    registry = ComponentRegistry()
    load_package(target, registry)

    assert registry.list_loaders(disabled_packages={"arc-loader-disable"}) == []
    with pytest.raises(KeyError):
        registry.get_loader("demo_loader", disabled_packages={"arc-loader-disable"})
