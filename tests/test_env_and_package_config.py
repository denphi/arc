"""Package config from `.env` (design/todo.md item 1.5).

Pins the two halves:
  * ``arc.core.env.load_env`` — a zero-dep ``.env`` loader where the
    process environment is authoritative;
  * ``ComponentRegistry.package_config`` — resolving a package's declared
    ``config:`` manifest section against the environment.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.chat


# ── .env parsing + load precedence ─────────────────────────────────────


def test_parse_env_file_handles_quotes_export_comments():
    from arc.core.env import parse_env_file
    parsed = parse_env_file(
        "# a comment\n"
        "export FOO=bar\n"
        'BAZ="quoted value"\n'
        "QUX='single'\n"
        "EMPTY=\n"
        "no_equals_here\n"
        "=missing_key\n"
        "  SPACED  =  trimmed  \n"
    )
    assert parsed == {
        "FOO": "bar",
        "BAZ": "quoted value",
        "QUX": "single",
        "EMPTY": "",
        "SPACED": "trimmed",
    }


def test_load_env_reads_dotenv_file(tmp_path, monkeypatch):
    import arc.core.env as env
    importlib.reload(env)  # reset the module-level _loaded guard
    (tmp_path / ".env").write_text("ARC_TEST_ENV_VAR=from_file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARC_TEST_ENV_VAR", raising=False)

    applied = env.load_env(force=True)
    import os
    assert os.environ.get("ARC_TEST_ENV_VAR") == "from_file"
    assert applied.get("ARC_TEST_ENV_VAR") == "from_file"


def test_load_env_does_not_override_process_env(tmp_path, monkeypatch):
    import arc.core.env as env
    importlib.reload(env)
    (tmp_path / ".env").write_text("ARC_TEST_ENV_VAR=from_file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARC_TEST_ENV_VAR", "from_process")  # already set → wins

    env.load_env(force=True)
    import os
    assert os.environ["ARC_TEST_ENV_VAR"] == "from_process"


# ── registry.package_config resolution ─────────────────────────────────


def _registry_with(manifest: dict):
    from arc.core.registry import ComponentRegistry
    reg = ComponentRegistry()
    reg.register_package(manifest["name"], manifest)
    return reg


def test_package_config_resolves_from_env(monkeypatch):
    monkeypatch.setenv("FOO_KEY", "live-value")
    reg = _registry_with({
        "name": "pkg",
        "config": [
            {"name": "FOO_KEY", "secret": True},
            {"name": "BAR_OPT", "default": "fallback"},
        ],
    })
    cfg = reg.package_config("pkg")
    assert cfg == {"FOO_KEY": "live-value", "BAR_OPT": "fallback"}


def test_package_config_empty_for_unknown_or_configless():
    reg = _registry_with({"name": "noconfig"})
    assert reg.package_config("noconfig") == {}
    assert reg.package_config("does-not-exist") == {}


def test_package_config_string_entry_shorthand(monkeypatch):
    monkeypatch.delenv("PLAIN", raising=False)
    reg = _registry_with({"name": "pkg", "config": ["PLAIN"]})
    # bare-string entry → resolves to "" when unset
    assert reg.package_config("pkg") == {"PLAIN": ""}


def test_required_unset_config_warns_on_load(tmp_path, monkeypatch, caplog):
    """A required config var that's unset surfaces a warning at load time."""
    import logging
    from arc.core.loader import load_package
    from arc.core.registry import ComponentRegistry

    pkg = tmp_path / "arc-needs-key"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: arc-needs-key\n"
        "config:\n"
        "  - name: ARC_REQUIRED_TEST_KEY\n"
        "    description: needed for the thing\n"
        "    required: true\n"
    )
    monkeypatch.delenv("ARC_REQUIRED_TEST_KEY", raising=False)
    reg = ComponentRegistry()
    with caplog.at_level(logging.WARNING):
        load_package(pkg, reg)
    assert any("ARC_REQUIRED_TEST_KEY" in r.message for r in caplog.records)


# ── bundled packages declare their config ───────────────────────────────


def test_bundled_packages_declare_expected_config():
    from arc.orchestrator.workflow import _default_registry
    reg = _default_registry()
    materials = reg.package_config("arc-materials")
    assert "MP_API_KEY" in materials
    providers = reg.package_config("arc-providers")
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} <= set(providers)
