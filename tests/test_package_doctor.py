import inspect

from typer.testing import CliRunner

from arc.cli.main import app


def _runner():
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def test_package_doctor_reports_runtime_requirements(tmp_path):
    target = tmp_path / "arc-doctor"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-doctor\n"
        "runtime:\n"
        "  python_modules:\n"
        "    - name: json\n"
        "      required: true\n"
        "    - name: definitely_missing_arc_module_xyz\n"
        "      required: false\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "doctor", str(target)])

    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "json" in result.output
    assert "WARN" in result.output
    assert "definitely_missing_arc_module_xyz" in result.output


def test_package_doctor_fails_for_missing_required_runtime(tmp_path):
    target = tmp_path / "arc-doctor-fail"
    target.mkdir()
    (target / "package.yaml").write_text(
        "name: arc-doctor-fail\n"
        "runtime:\n"
        "  python_modules:\n"
        "    - name: definitely_missing_arc_module_xyz\n"
        "      required: true\n",
        encoding="utf-8",
    )

    result = _runner().invoke(app, ["package", "doctor", str(target)])

    assert result.exit_code == 1
    assert "MISSING" in result.output
