import inspect

import pytest
from typer.testing import CliRunner

from arc.assets import FileStore
from arc.cli.main import app
from arc.core.loader import load_package
from arc.core.registry import ComponentRegistry
from arc.orchestrator.workflow import ResearchWorkflow
from arc.runtime.package_scripts import PackageScriptRunner
from arc.schemas.research import ResearchGoal


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _all_output(result):
    return (result.output or "") + (getattr(result, "stderr", "") or "")


def _script_package(tmp_path):
    target = tmp_path / "arc-script"
    (target / "scripts").mkdir(parents=True)
    (target / "scripts" / "make_output.py").write_text(
        "from pathlib import Path\n"
        "Path('out.md').write_text('# generated\\n', encoding='utf-8')\n"
        "print('done')\n",
        encoding="utf-8",
    )
    (target / "package.yaml").write_text(
        "name: arc-script\n"
        "provides:\n"
        "  scripts:\n"
        "    - name: make_output\n"
        "      path: scripts/make_output.py\n"
        "      runtime: python\n",
        encoding="utf-8",
    )
    return target


def test_package_validate_accepts_declared_script(tmp_path):
    target = _script_package(tmp_path)

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 0, result.output
    assert "scripts:   ['make_output']" in result.output


def test_package_validate_rejects_missing_declared_script(tmp_path):
    target = tmp_path / "arc-bad-script"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-bad-script\n"
        "provides:\n"
        "  scripts:\n"
        "    - name: missing\n"
        "      path: scripts/missing.py\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "validate", str(target)])

    assert result.exit_code == 1
    assert "declared script path does not exist" in _all_output(result)


def test_package_script_runner_imports_generated_assets(tmp_path):
    target = _script_package(tmp_path)
    registry = ComponentRegistry()
    load_package(target, registry)
    store = FileStore(tmp_path / "store")
    runner = PackageScriptRunner(registry, store, session_id="s1")

    result = runner.run("make_output", cwd=tmp_path / "work")

    assert result.returncode == 0
    assert "done" in result.stdout
    assert len(result.generated_assets) == 1
    asset = result.generated_assets[0]
    assert asset.name == "out.md"
    assert asset.role == "script_output"
    assert asset.metadata["script"] == "make_output"
    assert asset.metadata["package_name"] == "arc-script"


def test_package_script_runner_honours_disabled_package(tmp_path):
    target = _script_package(tmp_path)
    registry = ComponentRegistry()
    load_package(target, registry)
    runner = PackageScriptRunner(registry, FileStore(tmp_path / "store"), session_id="s1")

    with pytest.raises(KeyError, match="disabled"):
        runner.run(
            "make_output",
            cwd=tmp_path / "work",
            disabled_packages={"arc-script"},
        )


@pytest.mark.asyncio
async def test_workflow_can_invoke_declared_package_script(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "home"))
    target = _script_package(tmp_path)
    registry = ComponentRegistry()
    load_package(target, registry)
    workflow = ResearchWorkflow(
        registry=registry,
        session_id="script-workflow",
        workflow_name="script-loop",
    )
    registry.register_workflow(
        "script-loop",
        {
            "name": "script-loop",
            "steps": [
                {
                    "id": "export",
                    "script": "make_output",
                    "input": {"cwd": str(tmp_path / "workflow-work")},
                }
            ],
        },
    )

    result = await workflow.run_once(ResearchGoal(goal="Run script"))

    output = result["steps"]["export"]
    assert output["returncode"] == 0
    assert output["generated_assets"][0]["name"] == "out.md"
