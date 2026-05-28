"""``arc.chat.research.targets`` — target-distance helper tests.

These were previously inline in ``loop.py`` (Q2 extraction). The
``pct_off`` helper consults the reviewer's ``_keys_match`` and the
optional schema registry; both paths are exercised here.
"""

import pytest

from arc.chat.research.targets import pct_off, registry_keys_match


pytestmark = pytest.mark.chat


# ── registry_keys_match ──────────────────────────────────────────────────


def test_registry_match_same_canonical():
    registry = {"bandgap_ev": {"aliases": ["BandgapEV", "bandgap"]}}
    assert registry_keys_match("bandgap", "BandgapEV", registry) is True


def test_registry_match_normalises_underscores():
    registry = {"bandgap_ev": {"aliases": ["bandgap"]}}
    assert registry_keys_match("bandgap_ev", "bandgap", registry) is True


def test_registry_no_match_when_different_canonicals():
    registry = {
        "bandgap_ev": {"aliases": ["bandgap"]},
        "compliance": {"aliases": []},
    }
    assert registry_keys_match("bandgap", "compliance", registry) is False


def test_registry_empty():
    assert registry_keys_match("a", "b", {}) is False


# ── pct_off ──────────────────────────────────────────────────────────────


def test_pct_off_exact_match_zero_percent():
    out = pct_off({"bandgap_ev": 1.0}, {"bandgap_ev": 1.0})
    assert "bandgap_ev=1" in out
    assert "0.0% off" in out


def test_pct_off_percent_formatting():
    out = pct_off({"bandgap_ev": 1.1}, {"bandgap_ev": 1.0})
    assert "10.0% off" in out


def test_pct_off_empty_target_returns_empty():
    assert pct_off({"bandgap_ev": 1.0}, {}) == ""


def test_pct_off_no_matching_key_returns_marker():
    out = pct_off({"compliance": 1.0}, {"bandgap_ev": 1.0})
    # No fuzzy / registry match → "(no target key match)"
    assert out == "(no target key match)"


def test_pct_off_skips_non_numeric_output():
    out = pct_off({"bandgap_ev": "n/a"}, {"bandgap_ev": 1.0})
    # Loop matches the key but ov isn't numeric — break out empty
    assert out == "(no target key match)"


def test_pct_off_uses_registry_when_provided():
    registry = {"bandgap_ev": {"aliases": ["bandgap"]}}
    out = pct_off(
        {"bandgap": 1.1},
        {"bandgap_ev": 1.0},
        schema_registry=registry,
    )
    assert "bandgap=1.1" in out
    assert "10.0% off" in out
