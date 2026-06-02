"""arc.toml resolution + single source of truth (design/todo.md item 11).

Documents which file ``load_arc_toml()`` resolves in the repo tree, and
pins that the fallback ``arc/arc/arc.toml`` no longer carries an
independent package/extension list (so editing the wrong file can't
silently mislead, and the two files can't diverge).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arc.core.config import load_arc_toml, resolve_config_path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib

pytestmark = pytest.mark.chat

_REPO = Path(__file__).resolve().parents[1]          # arc/
_REPO_TOML = _REPO / "arc.toml"                       # authoritative
_FALLBACK_TOML = _REPO / "arc" / "arc.toml"           # tiny fallback


def test_default_resolution_is_repo_level_arc_toml():
    """No explicit path → the repo-level arc.toml wins (it exists)."""
    resolved = resolve_config_path(None)
    assert resolved == _REPO_TOML


def test_repo_level_carries_packages_and_extensions():
    _path, config = load_arc_toml()
    assert config.get("packages", {}).get("paths")        # authoritative list
    assert "extensions" in config


def test_fallback_has_no_independent_package_or_extension_lists():
    """The fallback is a documented pointer, not a second config (item 11)."""
    data = tomllib.loads(_FALLBACK_TOML.read_text())
    assert not data.get("packages", {}).get("paths")
    assert not data.get("extensions")


def test_fallback_used_only_when_repo_level_missing(tmp_path, monkeypatch):
    """If a non-existent path is requested, resolution falls back to the
    bundled defaults (repo-level first), never to a stale parallel list."""
    resolved = resolve_config_path(tmp_path / "nope.toml")
    assert resolved in (_REPO_TOML, _FALLBACK_TOML)
    # In a normal checkout the repo-level file exists, so it's chosen.
    assert resolved == _REPO_TOML


def test_no_web_ui_extension_block_anywhere():
    """[extensions.web-ui] is removed from both files (item 9)."""
    for toml_path in (_REPO_TOML, _FALLBACK_TOML):
        data = tomllib.loads(toml_path.read_text())
        assert "web-ui" not in (data.get("extensions") or {})
