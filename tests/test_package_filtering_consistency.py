"""Package enabled/disabled filtering is consistent across runtime paths
(design/todo.md item 3).

Before this, ``Kernel.startup()`` honoured ``[packages].enabled/disabled``
but ``ResearchWorkflow._default_registry()`` (what the CLI/UI/tests
instantiate) loaded every configured package. Both now share
``arc.core.config.filter_package_paths`` so a disabled package can't sneak
into a workflow run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arc.core.config import filter_package_paths, package_name_for_path

pytestmark = pytest.mark.chat


def _packages_root() -> Path:
    return Path(__file__).resolve().parents[1] / "arc" / "packages"


# ── filter_package_paths semantics ──────────────────────────────────────


def test_enabled_is_allowlist():
    root = _packages_root()
    paths = [str(root / "arc-sim2l"), str(root / "arc-materials")]
    out = filter_package_paths(paths, {"enabled": ["arc-sim2l"]})
    assert out == [str(root / "arc-sim2l")]


def test_disabled_is_denylist():
    root = _packages_root()
    paths = [str(root / "arc-sim2l"), str(root / "arc-materials")]
    out = filter_package_paths(paths, {"disabled": ["arc-materials"]})
    assert out == [str(root / "arc-sim2l")]


def test_no_filter_keeps_everything():
    root = _packages_root()
    paths = [str(root / "arc-sim2l"), str(root / "arc-materials")]
    assert filter_package_paths(paths, {}) == paths


def test_package_name_for_path_reads_manifest():
    assert package_name_for_path(_packages_root() / "arc-sim2l") == "arc-sim2l"
    # Missing manifest → directory name fallback.
    assert package_name_for_path(Path("/does/not/exist")) == "exist"


# ── Kernel and ResearchWorkflow load the same set ───────────────────────


def _write_config(tmp_path: Path, enabled: list[str]) -> Path:
    root = _packages_root()
    config = tmp_path / "arc.toml"
    enabled_list = ", ".join(f'"{e}"' for e in enabled)
    config.write_text(
        f"""
[packages]
paths = ["{root / 'arc-sim2l'}", "{root / 'arc-materials'}"]
enabled = [{enabled_list}]
"""
    )
    return config


def test_kernel_and_workflow_agree_on_enabled_set(tmp_path, monkeypatch):
    import asyncio

    from arc.core.config import load_arc_toml
    from arc.core.kernel import Kernel
    from arc.orchestrator import workflow as wf_module
    from arc.orchestrator.workflow import ResearchWorkflow

    config = _write_config(tmp_path, enabled=["arc-sim2l"])

    # Kernel uses an explicit config path.
    kernel = Kernel(config_path=str(config))
    asyncio.run(kernel.startup())
    kernel_packages = set(kernel.registry.list_packages())

    # ResearchWorkflow._default_registry() calls load_arc_toml() with no
    # path — point it at the same temp config so the comparison is fair.
    real_load = load_arc_toml
    monkeypatch.setattr(
        wf_module, "load_arc_toml",
        lambda path=None: real_load(str(config)),
    )
    workflow = ResearchWorkflow()
    workflow_packages = set(workflow.registry.list_packages())

    assert kernel_packages == workflow_packages
    assert "arc-sim2l" in workflow_packages
    assert "arc-materials" not in workflow_packages
