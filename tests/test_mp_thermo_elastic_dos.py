"""Materials Project thermo / elasticity / DOS → builder defaults.

After the structures wiring, the MP searcher also extracts three more
axes from each summary document and the planner splices them into
``plan.parameters`` as builder defaults:

  * Thermo:     ``energy_above_hull``, ``decomposition_enthalpy``
  * Elasticity: ``bulk_modulus``, ``shear_modulus`` (Voigt-Reuss-Hill)
  * DOS:        ``efermi``, ``dos_energy_up``, ``dos_energy_down``

Each axis gets its own extractor, lands on the hit's metadata under a
named block, and is consumed by ``_apply_mp_property_defaults`` which
generalises the previous lattice-only splice.

Tests use the same file-path module loader the structures tests use
because ``arc-materials`` directory has a hyphen.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arc.schemas.research import ResearchGoal, ResearchProposal


pytestmark = pytest.mark.chat


def _mp_module():
    mod_name = "_test_mp_thermo_module"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    path = (
        Path(__file__).resolve().parent.parent
        / "arc" / "packages" / "arc-materials" / "agents" / "mp_searcher.py"
    )
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _proposal():
    return ResearchProposal(
        hypothesis="x",
        objective="design silicon for stability",
        variables=["effective_mass"],
        methodology="DFT",
        expected_outcomes="x",
        evaluation_metrics=["energy_above_hull"],
    )


def _silicon_doc():
    """Realistic-ish MP summary for diamond-cubic Si."""
    return {
        "material_id": "mp-149",
        "formula_pretty": "Si",
        "elements": ["Si"],
        "band_gap": 0.61,
        "formation_energy_per_atom": -0.31,
        "density": 2.33,
        "is_stable": True,
        "symmetry": {"crystal_system": "Cubic"},
        "structure": {
            "lattice": {
                "a": 5.43, "b": 5.43, "c": 5.43,
                "alpha": 90.0, "beta": 90.0, "gamma": 90.0,
                "volume": 160.1,
            },
            "sites": [],
        },
        "energy_above_hull": 0.0,
        "decomposition_enthalpy": -0.02,
        "bulk_modulus": {"voigt": 99.0, "reuss": 95.0, "vrh": 97.0},
        "shear_modulus": 64.0,           # scalar shape — both should work
        "efermi": 5.55,
        "dos_energy_up": -12.0,
        "dos_energy_down": 6.5,
    }


# ── extract_thermo ─────────────────────────────────────────────────────


def test_extract_thermo_pulls_both_fields():
    out = _mp_module().extract_thermo(_silicon_doc())
    assert out["energy_above_hull"] == pytest.approx(0.0)
    assert out["decomposition_enthalpy"] == pytest.approx(-0.02)


def test_extract_thermo_returns_empty_for_none():
    assert _mp_module().extract_thermo(None) == {}


def test_extract_thermo_skips_missing_fields():
    out = _mp_module().extract_thermo({"energy_above_hull": 0.0})
    assert "energy_above_hull" in out
    assert "decomposition_enthalpy" not in out


def test_extract_thermo_skips_non_numeric():
    out = _mp_module().extract_thermo({"energy_above_hull": "stable"})
    assert "energy_above_hull" not in out


def test_extract_thermo_skips_bool():
    """``True`` is technically an int subclass; reject it explicitly."""
    out = _mp_module().extract_thermo({"energy_above_hull": True})
    assert "energy_above_hull" not in out


# ── extract_elasticity ─────────────────────────────────────────────────


def test_extract_elasticity_picks_vrh_from_dict():
    """VRH average is the preferred isotropic estimate when present."""
    out = _mp_module().extract_elasticity(_silicon_doc())
    assert out["bulk_modulus"] == pytest.approx(97.0)


def test_extract_elasticity_handles_scalar_shape():
    """Some MP records serialise moduli as bare scalars — accept both."""
    out = _mp_module().extract_elasticity({"shear_modulus": 64.0})
    assert out["shear_modulus"] == pytest.approx(64.0)


def test_extract_elasticity_falls_back_to_voigt_when_no_vrh():
    out = _mp_module().extract_elasticity(
        {"bulk_modulus": {"voigt": 99.0, "reuss": 95.0}},
    )
    # No vrh key → fall back to voigt.
    assert out["bulk_modulus"] == pytest.approx(99.0)


def test_extract_elasticity_returns_empty_for_unparseable():
    """An elastic block that's neither a scalar nor a recognised dict
    structure is silently dropped."""
    out = _mp_module().extract_elasticity(
        {"bulk_modulus": {"unrecognised_shape": 5}},
    )
    assert "bulk_modulus" not in out


# ── extract_dos ────────────────────────────────────────────────────────


def test_extract_dos_pulls_all_three_scalars():
    out = _mp_module().extract_dos(_silicon_doc())
    assert out["efermi"] == pytest.approx(5.55)
    assert out["dos_energy_up"] == pytest.approx(-12.0)
    assert out["dos_energy_down"] == pytest.approx(6.5)


def test_extract_dos_skips_missing_fields():
    out = _mp_module().extract_dos({"efermi": 1.0})
    assert out == {"efermi": 1.0}


def test_extract_dos_returns_empty_for_non_dict():
    assert _mp_module().extract_dos("not a dict") == {}


# ── Hit metadata ───────────────────────────────────────────────────────


def test_hit_metadata_carries_all_three_blocks():
    hit = _mp_module().mp_doc_to_catalog_hit(_silicon_doc())
    assert "thermo" in hit["metadata"]
    assert "elasticity" in hit["metadata"]
    assert "dos" in hit["metadata"]
    assert hit["metadata"]["thermo"]["energy_above_hull"] == pytest.approx(0.0)
    assert hit["metadata"]["elasticity"]["bulk_modulus"] == pytest.approx(97.0)
    assert hit["metadata"]["dos"]["efermi"] == pytest.approx(5.55)


def test_hit_metadata_omits_blocks_that_are_empty():
    """A doc with no thermo/elastic/DOS fields shouldn't litter the
    metadata with empty dicts."""
    doc = {
        "material_id": "mp-X",
        "formula_pretty": "X",
        "structure": _silicon_doc()["structure"],
    }
    hit = _mp_module().mp_doc_to_catalog_hit(doc)
    # Lattice still present (from structure); the others should not be.
    assert "lattice" in hit["metadata"]
    assert "thermo" not in hit["metadata"]
    assert "elasticity" not in hit["metadata"]
    assert "dos" not in hit["metadata"]


def test_hit_metadata_includes_partial_blocks():
    """A doc with only some thermo fields lands the available ones."""
    doc = {
        "material_id": "mp-X",
        "formula_pretty": "X",
        "energy_above_hull": 0.05,
        # No decomposition_enthalpy.
    }
    hit = _mp_module().mp_doc_to_catalog_hit(doc)
    assert hit["metadata"]["thermo"] == {"energy_above_hull": 0.05}


# ── Planner splice ─────────────────────────────────────────────────────


def _planner_context(catalog_hits=None, target=None):
    return SimpleNamespace(
        session_id="test-planner-mp",
        iteration=0,
        memory={
            "catalog_hits": list(catalog_hits or []),
            "target": dict(target or {}),
        },
    )


def _mp_hit_full(formula="Si", mp_id="mp-149"):
    return {
        "id": mp_id,
        "name": f"{formula}_{mp_id}",
        "metadata": {
            "source": "materials_project",
            "formula": formula,
            "mp_id": mp_id,
            "lattice": {"lattice_a": 5.43, "lattice_b": 5.43, "lattice_c": 5.43},
            "thermo": {
                "energy_above_hull": 0.0,
                "decomposition_enthalpy": -0.02,
            },
            "elasticity": {
                "bulk_modulus": 97.0,
                "shear_modulus": 64.0,
            },
            "dos": {
                "efermi": 5.55,
                "dos_energy_up": -12.0,
            },
        },
    }


def test_planner_splices_thermo_params():
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_full()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    assert plan.parameters["energy_above_hull"] == pytest.approx(0.0)
    assert plan.parameters["decomposition_enthalpy"] == pytest.approx(-0.02)
    assert plan.parameter_constraints["energy_above_hull"]["units"] == "eV/atom"
    assert "energy_above_hull" in plan.parameter_sweep


def test_planner_splices_elasticity_params():
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_full()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    assert plan.parameters["bulk_modulus"] == pytest.approx(97.0)
    assert plan.parameters["shear_modulus"] == pytest.approx(64.0)
    assert plan.parameter_constraints["bulk_modulus"]["units"] == "GPa"
    # Moduli are non-negative → lower bound must be clamped at 0.
    assert plan.parameter_constraints["bulk_modulus"]["min"] >= 0.0


def test_planner_splices_dos_params():
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_full()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    assert plan.parameters["efermi"] == pytest.approx(5.55)
    assert plan.parameters["dos_energy_up"] == pytest.approx(-12.0)
    assert plan.parameter_constraints["efermi"]["units"] == "eV"


def test_planner_attribution_lists_each_contributing_block():
    """Each block that contributes a parameter logs one design step."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_full()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    tags = " ".join(plan.experimental_design)
    assert "Lattice" in tags
    assert "Thermodynamic" in tags
    assert "Elastic" in tags
    assert "Electronic-structure" in tags
    assert "Si" in tags


def test_planner_does_not_overwrite_existing_thermo_param():
    """LLM-supplied parameters survive a thermo splice."""
    from arc.packages.arc_sim2l_agents.planner import (
        _apply_mp_property_defaults, _fallback_plan,
    )

    ctx = _planner_context(catalog_hits=[_mp_hit_full()])
    plan = _fallback_plan(_proposal())
    plan.parameters["energy_above_hull"] = 99.99   # user-supplied
    _apply_mp_property_defaults(plan, ctx)
    assert plan.parameters["energy_above_hull"] == 99.99
    # Other thermo keys still splice in.
    assert plan.parameters["decomposition_enthalpy"] == pytest.approx(-0.02)


def test_planner_no_op_when_property_blocks_all_missing():
    """An MP hit without thermo/elastic/DOS blocks must not add any
    non-lattice keys."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    hit = {
        "id": "mp-1",
        "name": "X_mp-1",
        "metadata": {
            "source": "materials_project",
            "formula": "X",
            # lattice only — no thermo/elasticity/dos
            "lattice": {"lattice_a": 5.0},
        },
    }
    ctx = _planner_context(catalog_hits=[hit])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    for axis_param in (
        "energy_above_hull", "decomposition_enthalpy",
        "bulk_modulus", "shear_modulus",
        "efermi", "dos_energy_up", "dos_energy_down",
    ):
        assert axis_param not in plan.parameters


def test_planner_handles_garbage_block_shape():
    """A metadata block that isn't a dict must not crash the splice."""
    from arc.packages.arc_sim2l_agents.planner import (
        _apply_mp_property_defaults, _fallback_plan,
    )

    hit = _mp_hit_full()
    hit["metadata"]["thermo"] = "not a dict"
    ctx = _planner_context(catalog_hits=[hit])
    plan = _fallback_plan(_proposal())
    _apply_mp_property_defaults(plan, ctx)
    # Garbage block dropped; other blocks still spliced.
    assert "energy_above_hull" not in plan.parameters
    assert plan.parameters["bulk_modulus"] == pytest.approx(97.0)


# ── End-to-end: search → plan picks up every axis ──────────────────────


def test_search_then_plan_carries_all_axes(monkeypatch):
    """Search returns a Si record → planner has lattice, thermo,
    elastic, and DOS defaults."""
    from arc.core.strategies import resolve_role
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    monkeypatch.setenv("MP_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [_silicon_doc()]}

    SearcherCls = resolve_role(
        "searcher", overrides={"searcher": "materials_project"},
    )
    ctx = SimpleNamespace(
        session_id="test-e2e-mp-axes",
        iteration=0,
        memory={"target": {"energy_above_hull": 0.0}},
    )
    searcher = SearcherCls(context=ctx)
    goal = ResearchGoal(
        goal="design a stable silicon allotrope", domain="materials",
        target={"energy_above_hull": 0.0},
    )

    with patch("requests.get", lambda url, params=None, headers=None,
               timeout=None: _Resp()):
        result = asyncio.run(searcher.search(goal))
    ctx.memory["catalog_hits"] = result.catalog_hits

    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))
    # All four MP axes contributed.
    assert plan.parameters["lattice_a"] == pytest.approx(5.43)
    assert plan.parameters["energy_above_hull"] == pytest.approx(0.0)
    assert plan.parameters["bulk_modulus"] == pytest.approx(97.0)
    assert plan.parameters["efermi"] == pytest.approx(5.55)
