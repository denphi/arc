"""arc.toml resolution: bundled defaults + project overlay.

The package list and extension defaults ship inside the ``arc`` package
(``arc/arc.toml``) so a plain ``pip install`` works outside a checkout.
A project ``./arc.toml`` only overlays it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arc.core.config import (
    build_context_workflow_specs,
    bundled_config_path,
    config_section,
    load_arc_toml,
    resolve_config_path,
)

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib

pytestmark = pytest.mark.chat

_REPO = Path(__file__).resolve().parents[1]
_PROJECT_TOML = _REPO / "arc.toml"                    # checkout overlay
_BUNDLED_TOML = _REPO / "arc" / "arc.toml"            # authoritative defaults


def test_bundled_config_lives_inside_the_package():
    assert bundled_config_path() == _BUNDLED_TOML.resolve()


def test_bundled_config_carries_packages_and_extensions():
    data = tomllib.loads(_BUNDLED_TOML.read_text())
    assert "packages/arc-sim2l" in data["packages"]["paths"]
    assert "extensions" in data


def test_project_overlay_declares_no_second_package_catalogue():
    data = tomllib.loads(_PROJECT_TOML.read_text())
    assert not data.get("packages", {}).get("paths")
    assert not data.get("extensions")


def test_outside_a_project_the_bundled_config_is_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved, config = load_arc_toml()
    assert resolved == bundled_config_path()
    paths = config["packages"]["paths"]
    # Relative to the bundled file, so they resolve inside the package.
    assert "packages/arc-sim2l" in paths


def test_current_folder_arc_toml_wins_and_extends_defaults(tmp_path, monkeypatch):
    project_config = tmp_path / "arc.toml"
    project_config.write_text(
        """
[packages]
paths = ["../arc-fenics"]

[strategies]
builder = "codex"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    resolved, config = load_arc_toml()

    assert resolved == project_config
    paths = config.get("packages", {}).get("paths") or []
    assert str((_REPO / "arc" / "packages" / "arc-sim2l").resolve()) in paths
    assert "../arc-fenics" in paths
    assert config.get("strategies", {}).get("builder") == "codex"


def test_missing_explicit_path_falls_back_to_bundled(tmp_path):
    assert resolve_config_path(tmp_path / "nope.toml") == bundled_config_path()


def test_config_section_reads_runtime_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config_section("runtime").get("adapter") == "local"
    assert config_section("api").get("host") == "127.0.0.1"
    assert config_section("does-not-exist") == {}


def test_no_web_ui_extension_block_anywhere():
    """[extensions.web-ui] is not a thing — the UI is a standalone app."""
    for toml_path in (_PROJECT_TOML, _BUNDLED_TOML):
        data = tomllib.loads(toml_path.read_text())
        assert "web-ui" not in (data.get("extensions") or {})


def test_build_context_workflow_specs_accepts_list_form():
    specs = build_context_workflow_specs({
        "workflows": {"build": {"context": ["paper-context"]}},
    })

    assert specs == [{
        "name": "paper-context",
        "required": True,
        "cache": "per_iteration",
        "inputs": {},
    }]


def test_build_context_workflow_specs_accepts_table_form():
    specs = build_context_workflow_specs({
        "workflows": {
            "build": {
                "context": [
                    {
                        "name": "paper-context",
                        "required": False,
                        "cache": "per_plan",
                        "inputs": {"paper": "paper24.pdf"},
                    }
                ]
            }
        },
    })

    assert specs == [{
        "name": "paper-context",
        "required": False,
        "cache": "per_plan",
        "inputs": {"paper": "paper24.pdf"},
    }]
