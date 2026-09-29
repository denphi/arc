"""Regression tests for target → output key resolution.

The reviewer's deterministic approval, optimizer fitness and chat
diagnostics all resolve target keys through ``arc.runtime.key_matching``.
"""

from __future__ import annotations

from arc.packages.arc_sim2l.agents.optimizer import _fitness
from arc.packages.arc_sim2l.agents.reviewer import _check_target
from arc.runtime.key_matching import best_output_key, match_targets, target_errors


def test_non_numeric_label_does_not_shadow_value():
    outputs = {"band_gap_units": "eV", "band_gap": 1.5, "temperature": 300.0}
    approved, errors = _check_target(outputs, {"band_gap": 1.1, "temperature": 300})
    assert approved is False
    assert errors["band_gap"] == 36.36


def test_exact_key_beats_synonym():
    approved, errors = _check_target({"stress_xx": 10.0, "pressure": 50.0}, {"pressure": 10})
    assert approved is False
    assert "pressure" in errors and "stress_xx" not in errors


def test_unmatched_target_key_fails_approval():
    approved, errors = _check_target({"band_gap": 1.1}, {"band_gap": 1.1, "mobility": 5})
    assert approved is False
    assert errors == {"band_gap": 0.0}


def test_all_keys_within_threshold_approves():
    approved, _ = _check_target({"bandgap_ev": 1.12, "temp": 300.0}, {"band_gap": 1.1, "temperature": 300})
    assert approved is True


def test_bool_outputs_are_not_numbers():
    assert best_output_key("converged", {"converged": True}) is None


def test_match_prefers_most_reliable_tier_across_all_outputs():
    outputs = {"strain_energy": 1.0, "strain": 2.0}
    assert match_targets(outputs, {"strain": 0})["strain"] == "strain"


def test_target_errors_reports_unmatched_and_non_numeric_targets():
    errors, unmatched = target_errors({"a_value": 2.0}, {"a_value": 1.0, "b": 1.0, "c": "x"})
    assert errors == {"a_value": 1.0}
    assert unmatched == ["b", "c"]


def test_fitness_uses_the_numeric_value_not_the_label():
    fit = _fitness({"band_gap_units": "eV", "band_gap": 1.1}, {"band_gap": 1.1})
    assert fit == 0.0
