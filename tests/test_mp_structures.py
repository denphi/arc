"""Materials Project structure → builder defaults.

When the MP searcher returns a hit with a ``structure`` field, we
extract its six lattice parameters (a/b/c/α/β/γ) plus optional volume,
attach them to the hit's ``metadata['lattice']``, and the planner
splices them into ``plan.parameters`` as builder defaults — so the
generated workflow starts from a real, DFT-grade lattice rather than
a placeholder.

Tests cover three layers:

  * ``extract_lattice`` — parsing of the pymatgen-flavoured structure
    dict the next-gen API returns.
  * ``mp_doc_to_catalog_hit`` — the extracted lattice lands on the hit's
    metadata so downstream agents can read it.
  * ``PlannerAgent`` — the planner splices lattice params into
    ``plan.parameters`` when the top catalog hit is MP-sourced, never
    overwriting LLM-supplied or user-supplied values.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc.schemas.research import ResearchProposal


pytestmark = pytest.mark.chat


def _mp_module():
    """File-path load of the MP searcher (arc-materials has a hyphen)."""
    mod_name = "_test_mp_structures_module"
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


def _silicon_structure():
    """Pymatgen-shaped structure for diamond-cubic Si (a≈5.43 Å)."""
    return {
        "lattice": {
            "a": 5.43, "b": 5.43, "c": 5.43,
            "alpha": 90.0, "beta": 90.0, "gamma": 90.0,
            "volume": 160.1,
        },
        "sites": [],
    }


def _proposal():
    return ResearchProposal(
        hypothesis="silicon nanostructures should hit 1.1 eV bandgap",
        objective="design silicon for bandgap=1.1",
        variables=["effective_mass", "temperature"],
        methodology="DFT",
        expected_outcomes="x",
        evaluation_metrics=["bandgap_ev"],
    )


# ── extract_lattice ────────────────────────────────────────────────────


def test_extract_lattice_pulls_six_params_plus_volume():
    extract = _mp_module().extract_lattice
    out = extract(_silicon_structure())
    assert out["lattice_a"] == pytest.approx(5.43)
    assert out["lattice_b"] == pytest.approx(5.43)
    assert out["lattice_c"] == pytest.approx(5.43)
    assert out["lattice_alpha"] == pytest.approx(90.0)
    assert out["lattice_volume"] == pytest.approx(160.1)


def test_extract_lattice_returns_empty_for_none():
    assert _mp_module().extract_lattice(None) == {}


def test_extract_lattice_returns_empty_when_lattice_missing():
    assert _mp_module().extract_lattice({"sites": []}) == {}


def test_extract_lattice_returns_empty_for_non_dict_lattice():
    assert _mp_module().extract_lattice({"lattice": "not a dict"}) == {}


def test_extract_lattice_skips_non_numeric_values():
    """A lattice field with a string value should be silently dropped,
    not produce a broken parameter."""
    out = _mp_module().extract_lattice({"lattice": {"a": "wat", "b": 5.43}})
    assert "lattice_a" not in out
    assert out["lattice_b"] == pytest.approx(5.43)


def test_extract_lattice_skips_bool_values():
    """``True`` is technically an int subclass; reject it explicitly."""
    out = _mp_module().extract_lattice({"lattice": {"a": True}})
    assert "lattice_a" not in out


# ── Hit mapping ────────────────────────────────────────────────────────


def _silicon_doc():
    return {
        "material_id": "mp-149",
        "formula_pretty": "Si",
        "elements": ["Si"],
        "band_gap": 0.61,
        "formation_energy_per_atom": -0.31,
        "density": 2.33,
        "is_stable": True,
        "symmetry": {"crystal_system": "Cubic"},
        "structure": _silicon_structure(),
    }


def test_hit_metadata_includes_lattice_when_present():
    hit = _mp_module().mp_doc_to_catalog_hit(_silicon_doc())
    assert "lattice" in hit["metadata"]
    assert hit["metadata"]["lattice"]["lattice_a"] == pytest.approx(5.43)


def test_hit_metadata_omits_lattice_when_structure_missing():
    doc = _silicon_doc()
    del doc["structure"]
    hit = _mp_module().mp_doc_to_catalog_hit(doc)
    assert "lattice" not in hit["metadata"]


def test_hit_metadata_keeps_crystal_system_separate():
    hit = _mp_module().mp_doc_to_catalog_hit(_silicon_doc())
    assert hit["metadata"]["crystal_system"] == "Cubic"
    # Lattice is its own block — symmetry isn't mixed in.
    assert "crystal_system" not in hit["metadata"].get("lattice", {})


# ── Planner splice ─────────────────────────────────────────────────────


def _planner_context(catalog_hits=None, target=None):
    return SimpleNamespace(
        session_id="test-planner",
        iteration=0,
        memory={
            "catalog_hits": list(catalog_hits or []),
            "target": dict(target or {}),
        },
    )


def _mp_hit_with_lattice(formula="Si", mp_id="mp-149"):
    return {
        "id": mp_id,
        "name": f"{formula}_{mp_id}",
        "metadata": {
            "source": "materials_project",
            "formula": formula,
            "mp_id": mp_id,
            "lattice": {
                "lattice_a": 5.43, "lattice_b": 5.43, "lattice_c": 5.43,
                "lattice_alpha": 90.0, "lattice_beta": 90.0, "lattice_gamma": 90.0,
            },
        },
    }


def test_planner_splices_lattice_when_top_hit_is_mp():
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_with_lattice()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    assert plan.parameters.get("lattice_a") == pytest.approx(5.43)
    assert plan.parameters.get("lattice_alpha") == pytest.approx(90.0)
    # Constraint + sweep were minted alongside the parameter.
    assert "lattice_a" in plan.parameter_constraints
    assert "lattice_a" in plan.parameter_sweep


def test_planner_constraints_match_lattice_units():
    """Lattice lengths get ``angstrom`` units, angles get ``degrees``."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_with_lattice()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    assert plan.parameter_constraints["lattice_a"]["units"] == "angstrom"
    assert plan.parameter_constraints["lattice_alpha"]["units"] == "degrees"


def test_planner_constraints_band_lengths_around_default():
    """Lattice-length constraints should band ±10% around the value."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_with_lattice()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    c = plan.parameter_constraints["lattice_a"]
    assert c["min"] == pytest.approx(5.43 - 0.543)
    assert c["max"] == pytest.approx(5.43 + 0.543)


def test_planner_does_not_overwrite_existing_parameter():
    """If the LLM (or fallback) already produced ``lattice_a``, the MP
    splice must not stomp on it."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_with_lattice()])
    # Pre-seed a parameter under the same name.
    agent = PlannerAgent(context=ctx)
    # Bypass run() to control plan shape exactly.
    from arc.packages.arc_sim2l_agents.planner import (
        _apply_mp_lattice_defaults, _fallback_plan,
    )
    plan = _fallback_plan(_proposal())
    plan.parameters["lattice_a"] = 99.99   # user-supplied placeholder
    _apply_mp_lattice_defaults(plan, ctx)
    # User-supplied wins.
    assert plan.parameters["lattice_a"] == 99.99
    # Other lattice keys still got spliced.
    assert plan.parameters["lattice_alpha"] == pytest.approx(90.0)


def test_planner_no_op_when_top_hit_is_not_mp():
    """A non-MP catalog hit (regular sim2l catalog entry) must not
    trigger the lattice splice."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    non_mp_hit = {
        "name": "some_local_artifact",
        "metadata": {"source": "sim2l_catalog"},
    }
    ctx = _planner_context(catalog_hits=[non_mp_hit])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    assert not any(k.startswith("lattice_") for k in plan.parameters)


def test_planner_no_op_when_no_catalog_hits():
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))
    assert not any(k.startswith("lattice_") for k in plan.parameters)


def test_planner_no_op_when_lattice_metadata_missing():
    """An MP hit without ``metadata.lattice`` is fine — most MP hits
    will have it, but missing must not crash."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    hit_no_lattice = {
        "name": "Si_mp-149",
        "metadata": {"source": "materials_project", "formula": "Si"},
    }
    ctx = _planner_context(catalog_hits=[hit_no_lattice])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))
    assert not any(k.startswith("lattice_") for k in plan.parameters)


def test_planner_tags_experimental_design_with_mp_attribution():
    """The user should see in the plan that defaults came from MP."""
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent

    ctx = _planner_context(catalog_hits=[_mp_hit_with_lattice()])
    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))

    tags = " ".join(plan.experimental_design)
    assert "Materials Project" in tags
    assert "Si" in tags


def test_planner_skips_malformed_lattice_entries():
    """A lattice dict with non-numeric values must be silently dropped."""
    from arc.packages.arc_sim2l_agents.planner import _apply_mp_lattice_defaults, _fallback_plan

    hit = _mp_hit_with_lattice()
    hit["metadata"]["lattice"] = {"lattice_a": "wat", "lattice_b": 5.43}
    ctx = _planner_context(catalog_hits=[hit])
    plan = _fallback_plan(_proposal())
    _apply_mp_lattice_defaults(plan, ctx)
    assert "lattice_a" not in plan.parameters
    assert plan.parameters["lattice_b"] == pytest.approx(5.43)


# ── End-to-end: search → plan with lattice ─────────────────────────────


def test_search_then_plan_carries_lattice_into_parameters(monkeypatch):
    """Full chain: MP searcher returns Si with structure → ideator stores
    catalog_hits → planner splices lattice into parameters."""
    from unittest.mock import patch

    from arc.core.strategies import resolve_role
    from arc.packages.arc_sim2l_agents.planner import PlannerAgent
    from arc.schemas.research import ResearchGoal

    monkeypatch.setenv("MP_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [_silicon_doc()]}

    def _get(url, params=None, headers=None, timeout=None):
        return _Resp()

    SearcherCls = resolve_role(
        "searcher", overrides={"searcher": "materials_project"},
    )
    # The ideator stores catalog_hits on memory; we mirror that here
    # since this test focuses on the search→plan plumbing, not the LLM.
    ctx = SimpleNamespace(
        session_id="test-e2e",
        iteration=0,
        memory={"target": {"bandgap_ev": 1.1}},
    )
    searcher = SearcherCls(context=ctx)
    goal = ResearchGoal(
        goal="design a silicon nanostructure", domain="materials",
        target={"bandgap_ev": 1.1},
    )

    with patch("requests.get", _get):
        result = asyncio.run(searcher.search(goal))
    ctx.memory["catalog_hits"] = result.catalog_hits

    plan = asyncio.run(PlannerAgent(context=ctx).run(_proposal()))
    assert plan.parameters["lattice_a"] == pytest.approx(5.43)
    assert plan.parameter_constraints["lattice_a"]["units"] == "angstrom"
