"""Materials Project searcher — element detection, query mapping,
hit translation, missing-key behaviour, and recipe integration.

The Materials Project (next-gen.materialsproject.org/api) is the
canonical source of DFT-computed reference materials. Our integration
goal is modest: given a goal text + numeric target, hand the LLM a
list of real materials whose properties are in the ballpark, so the
ideator's hypothesis is grounded in known computed values rather than
just keyword catalog matches.

All tests stub ``requests.get`` so no actual API calls leave the
machine. The MP-key-set tests work the same with or without an account.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arc.schemas.research import ResearchGoal, SearchResult


pytestmark = pytest.mark.chat


def _mp_module():
    """Load the MP searcher module by file path.

    arc-materials uses a hyphen in its directory name so it isn't a
    valid Python identifier; the strategy resolver loads it via file
    path. Tests do the same here so they exercise the same load path
    the runtime uses.
    """
    mod_name = "_test_mp_searcher_module"
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


# ── Element / target parsing ────────────────────────────────────────────


def test_detect_elements_finds_named_materials():
    detect_elements = _mp_module().detect_elements
    assert "Si" in detect_elements("design a silicon nanowire")
    assert "Ge" in detect_elements("study germanium")


def test_detect_elements_finds_literal_symbols():
    detect_elements = _mp_module().detect_elements
    elements = detect_elements("GaAs heterostructure")
    assert "Ga" in elements
    assert "As" in elements


def test_detect_elements_returns_empty_for_no_match():
    detect_elements = _mp_module().detect_elements
    assert detect_elements("study cute kittens") == []


def test_detect_elements_caps_result_count():
    detect_elements = _mp_module().detect_elements
    elements = detect_elements("Li Na K Rb Cs alkali metals")
    assert len(elements) <= 4


def test_detect_elements_dedups_named_and_literal():
    """``silicon`` and ``Si`` should not appear twice."""
    detect_elements = _mp_module().detect_elements
    elements = detect_elements("silicon Si nanowire")
    assert elements.count("Si") == 1


def test_target_to_params_band_gap_band():
    target_to_params = _mp_module().target_to_params
    params = target_to_params({"bandgap_ev": 1.1})
    assert params["band_gap_min"] == pytest.approx(0.88)
    assert params["band_gap_max"] == pytest.approx(1.32)


def test_target_to_params_formation_energy():
    target_to_params = _mp_module().target_to_params
    params = target_to_params({"formation_energy_per_atom": -2.0})
    assert "formation_energy_per_atom_min" in params
    assert "formation_energy_per_atom_max" in params


def test_target_to_params_ignores_unknown_keys():
    target_to_params = _mp_module().target_to_params
    assert target_to_params({"random_thing": 5.0}) == {}


def test_target_to_params_ignores_non_numeric():
    target_to_params = _mp_module().target_to_params
    assert target_to_params({"bandgap_ev": "not a number"}) == {}


def test_target_to_params_handles_zero_value():
    """A target of 0 must still produce a non-degenerate band."""
    target_to_params = _mp_module().target_to_params
    params = target_to_params({"bandgap_ev": 0.0})
    assert params["band_gap_min"] < params["band_gap_max"]


# ── Hit translation ────────────────────────────────────────────────────


def _mp_doc(**overrides):
    base = {
        "material_id": "mp-149",
        "formula_pretty": "Si",
        "elements": ["Si"],
        "band_gap": 0.61,
        "formation_energy_per_atom": -0.31,
        "density": 2.33,
        "is_stable": True,
        "symmetry": {"crystal_system": "Cubic"},
    }
    base.update(overrides)
    return base


def test_mp_doc_to_catalog_hit_uses_arc_catalog_shape():
    mp_doc_to_catalog_hit = _mp_module().mp_doc_to_catalog_hit
    hit = mp_doc_to_catalog_hit(_mp_doc())
    for key in ("id", "name", "description", "input_schema",
                "output_schema", "tags", "metadata"):
        assert key in hit


def test_mp_hit_name_combines_formula_and_mp_id():
    mp_doc_to_catalog_hit = _mp_module().mp_doc_to_catalog_hit
    hit = mp_doc_to_catalog_hit(_mp_doc())
    assert hit["name"] == "Si_mp-149"


def test_mp_hit_input_schema_is_empty_because_read_only():
    """MP records aren't runnable artifacts — no inputs to fill."""
    mp_doc_to_catalog_hit = _mp_module().mp_doc_to_catalog_hit
    hit = mp_doc_to_catalog_hit(_mp_doc())
    assert hit["input_schema"] == {}


def test_mp_hit_output_schema_carries_computed_values():
    mp_doc_to_catalog_hit = _mp_module().mp_doc_to_catalog_hit
    hit = mp_doc_to_catalog_hit(_mp_doc())
    assert hit["output_schema"]["band_gap"]["value"] == pytest.approx(0.61)
    assert hit["output_schema"]["band_gap"]["units"] == "eV"
    assert hit["output_schema"]["formation_energy_per_atom"]["value"] == pytest.approx(-0.31)


def test_mp_hit_metadata_marks_source_and_url():
    mp_doc_to_catalog_hit = _mp_module().mp_doc_to_catalog_hit
    hit = mp_doc_to_catalog_hit(_mp_doc())
    assert hit["metadata"]["source"] == "materials_project"
    assert hit["metadata"]["mp_id"] == "mp-149"
    assert "next-gen.materialsproject.org" in hit["metadata"]["url"]


def test_mp_hit_tags_include_crystal_system():
    mp_doc_to_catalog_hit = _mp_module().mp_doc_to_catalog_hit
    hit = mp_doc_to_catalog_hit(_mp_doc())
    assert "materials_project" in hit["tags"]
    assert "cubic" in hit["tags"]


def test_mp_hit_handles_missing_optional_fields():
    mp_doc_to_catalog_hit = _mp_module().mp_doc_to_catalog_hit
    hit = mp_doc_to_catalog_hit({"material_id": "mp-1", "formula_pretty": "X"})
    assert hit["name"] == "X_mp-1"
    assert hit["output_schema"] == {}


# ── Agent search() behaviour ───────────────────────────────────────────


def _agent():
    """Build the searcher with a minimal context."""
    from arc.core.strategies import resolve_role
    cls = resolve_role(
        "searcher", overrides={"searcher": "materials_project"},
    )
    ctx = SimpleNamespace(memory={})
    return cls(context=ctx)


def test_search_returns_empty_when_no_api_key(monkeypatch):
    """No MP_API_KEY → empty result, no requests made."""
    monkeypatch.delenv("MP_API_KEY", raising=False)

    def _no_calls(*a, **kw):
        pytest.fail("MP searcher must not call requests when MP_API_KEY is unset")

    with patch("requests.get", _no_calls):
        result = asyncio.run(_agent().search(
            ResearchGoal(goal="silicon bandgap", domain="materials"),
        ))
    assert isinstance(result, SearchResult)
    assert result.catalog_hits == []
    assert result.prior_results == []


def test_search_calls_mp_with_elements_and_target_band(monkeypatch):
    """Element + target → MP query carries both element list and band-gap range."""
    monkeypatch.setenv("MP_API_KEY", "test-key")

    captured: dict = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [_mp_doc()]}

    def _get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params or {}
        captured["headers"] = headers or {}
        return _Resp()

    with patch("requests.get", _get):
        result = asyncio.run(_agent().search(ResearchGoal(
            goal="design a silicon nanowire",
            domain="materials",
            target={"bandgap_ev": 1.1},
        )))

    assert captured["url"].endswith("/materials/summary/")
    assert captured["headers"]["X-API-KEY"] == "test-key"
    assert captured["params"]["elements"] == "Si"
    assert "band_gap_min" in captured["params"]
    assert "band_gap_max" in captured["params"]
    # MP doc was translated into one catalog hit.
    assert len(result.catalog_hits) == 1
    assert result.catalog_hits[0]["metadata"]["source"] == "materials_project"


def test_search_swallows_network_errors(monkeypatch):
    monkeypatch.setenv("MP_API_KEY", "test-key")

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    with patch("requests.get", _boom):
        result = asyncio.run(_agent().search(
            ResearchGoal(goal="silicon", domain="materials"),
        ))
    assert result.catalog_hits == []


def test_search_swallows_non_200_responses(monkeypatch):
    monkeypatch.setenv("MP_API_KEY", "test-key")

    class _Resp:
        status_code = 403
        text = "forbidden"
        def json(self):
            return {}

    with patch("requests.get", lambda *a, **kw: _Resp()):
        result = asyncio.run(_agent().search(
            ResearchGoal(goal="silicon", domain="materials"),
        ))
    assert result.catalog_hits == []


def test_search_handles_unparseable_body(monkeypatch):
    monkeypatch.setenv("MP_API_KEY", "test-key")

    class _Resp:
        status_code = 200
        def json(self):
            return "not a dict"

    with patch("requests.get", lambda *a, **kw: _Resp()):
        result = asyncio.run(_agent().search(
            ResearchGoal(goal="silicon", domain="materials"),
        ))
    assert result.catalog_hits == []


# ── Strategy + recipe integration ──────────────────────────────────────


def test_strategy_resolver_returns_mp_searcher_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role(
        "searcher", overrides={"searcher": "materials_project"},
    )
    assert cls.__name__ == "MaterialsProjectSearcherAgent"


def test_mp_discovery_recipe_is_valid():
    from arc.core.recipes import get_recipe, validate_recipe
    recipe = get_recipe("mp-discovery")
    assert recipe is not None
    assert recipe.strategies["searcher"] == "materials_project"
    assert recipe.strategies["validator"] == "materials_evaluators"
    assert validate_recipe(recipe) == []


def test_mp_discovery_recipe_applies_through_resolver(monkeypatch):
    import asyncio
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.packages import resolve_role as pkg_resolve_role
    from tests.fakes import make_workflow

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = ChatState(workflow=make_workflow())
    asyncio.run(run(state, ["apply", "mp-discovery"]))

    searcher_cls = pkg_resolve_role("searcher", state.workflow)
    optimizer_cls = pkg_resolve_role("optimizer", state.workflow)
    validator_cls = pkg_resolve_role("validator", state.workflow)
    assert searcher_cls.__name__ == "MaterialsProjectSearcherAgent"
    assert optimizer_cls.__name__ == "BayesOptOptimizerAgent"
    assert validator_cls.__name__ == "MaterialsValidatorAgent"
