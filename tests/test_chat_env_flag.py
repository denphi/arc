"""``arc.chat._env.env_flag`` helper tests (Finding #8)."""

import pytest

from arc.chat._env import env_flag


pytestmark = pytest.mark.chat


@pytest.mark.parametrize("value, expected", [
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("True", True),
    ("yes", True),
    ("on", True),
    ("  1  ", True),
    ("  YES  ", True),
    # falsy
    ("0", False),
    ("false", False),
    ("no", False),
    ("off", False),
    ("", False),
    ("banana", False),
    ("truthy", False),
    ("1.0", False),
    ("yes1", False),
])
def test_env_flag_truthy_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("X", value)
    assert env_flag("X") is expected


def test_env_flag_unset_is_false(monkeypatch):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert env_flag("UNSET_VAR") is False


# ── All three callers now use the helper ──────────────────────────────────


def test_callers_use_env_flag_not_inline_parsing():
    """Belt-and-braces: ensure nobody re-introduces the inline pattern.

    Search the three call-site files for the legacy inline string set.
    If a future commit reverts to ``in {"1", "true", "yes", "on"}`` we
    want the test to flag it.
    """
    from pathlib import Path
    chat_root = Path(__file__).resolve().parents[1] / "arc" / "chat"
    files = [
        chat_root / "loop.py",
        chat_root / "skill_loader.py",
        chat_root / "agents" / "definition.py",
    ]
    for f in files:
        text = f.read_text()
        # We expect zero in-line copies of the truthy literal set
        assert '"1", "true", "yes", "on"' not in text, (
            f"{f.name} still has the inline env-flag parsing pattern; "
            f"use arc.chat._env.env_flag instead"
        )
