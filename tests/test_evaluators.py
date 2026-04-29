"""Tests for arc-materials domain evaluators."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _load_evaluator(name: str):
    path = ROOT / "arc" / "packages" / "arc-materials" / "evaluators" / f"{name}.py"
    module_name = f"arc_materials.evaluators.{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_bandgap_semiconductor():
    m = _load_evaluator("bandgap")
    ev = m.BandgapEvaluator()
    result = ev.evaluate({"band_gap": 1.1})
    assert result["passed"] is True
    assert result["classification"] == "semiconductor"


def test_bandgap_insulator():
    m = _load_evaluator("bandgap")
    ev = m.BandgapEvaluator()
    result = ev.evaluate({"band_gap": 8.0})
    assert result["passed"] is True
    assert result["classification"] == "insulator"


def test_bandgap_out_of_range():
    m = _load_evaluator("bandgap")
    ev = m.BandgapEvaluator()
    result = ev.evaluate({"band_gap": 20.0})
    assert result["passed"] is False


def test_bandgap_missing():
    m = _load_evaluator("bandgap")
    ev = m.BandgapEvaluator()
    result = ev.evaluate({})
    assert result["passed"] is False


def test_formation_energy_stable():
    m = _load_evaluator("formation_energy")
    ev = m.FormationEnergyEvaluator()
    result = ev.evaluate({"formation_energy": -1.5})
    assert result["passed"] is True
    assert result["stable"] is True


def test_formation_energy_unstable():
    m = _load_evaluator("formation_energy")
    ev = m.FormationEnergyEvaluator()
    result = ev.evaluate({"formation_energy": 0.5})
    assert result["passed"] is True
    assert result["stable"] is False


def test_formation_energy_out_of_range():
    m = _load_evaluator("formation_energy")
    ev = m.FormationEnergyEvaluator()
    result = ev.evaluate({"formation_energy": -10.0})
    assert result["passed"] is False


def test_stability_converged():
    m = _load_evaluator("stability")
    ev = m.StructureStabilityEvaluator()
    result = ev.evaluate({"total_energy": -10.0, "max_force": 0.01, "converged": True})
    assert result["passed"] is True


def test_stability_high_force():
    m = _load_evaluator("stability")
    ev = m.StructureStabilityEvaluator()
    result = ev.evaluate({"total_energy": -10.0, "max_force": 0.1, "converged": True})
    assert result["passed"] is False
    assert len(result["flags"]) > 0


def test_property_prediction_all_valid():
    m = _load_evaluator("property_prediction")
    ev = m.PropertyPredictionEvaluator()
    result = ev.evaluate({"band_gap": 1.5, "formation_energy": -1.0, "bulk_modulus": 100.0})
    assert result["passed"] is True


def test_property_prediction_invalid_bandgap():
    m = _load_evaluator("property_prediction")
    ev = m.PropertyPredictionEvaluator()
    result = ev.evaluate({"band_gap": 20.0})
    assert result["passed"] is False
