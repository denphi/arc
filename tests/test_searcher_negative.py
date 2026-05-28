"""NegativeResultsSearcherAgent — surfaces failed prior runs.

Unlike the keyword / embedding searchers (which bias toward success),
this one returns *only* the failures: errored runs, all-NaN outputs,
and far-from-target results. The ideator then warns the LLM off
known-bad parameter regions.

Tests stub the catalog + results HTTP helpers (imported into the
negative-results module from the base searcher) so no live services
are needed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arc.packages.arc_sim2l_agents.searcher_negative import (
    NegativeResultsSearcherAgent,
    _annotate_failures,
    _is_failed_run,
)
from arc.schemas.research import ResearchGoal, SearchResult


pytestmark = pytest.mark.chat


# ── _is_failed_run classification ──────────────────────────────────────


def test_failed_status_is_failure():
    is_fail, reason = _is_failed_run(
        {"status": "failed", "output_params": {}}, {},
    )
    assert is_fail
    assert "failed" in reason


def test_non_completed_status_is_failure():
    is_fail, reason = _is_failed_run(
        {"status": "timeout", "output_params": {"x": 1.0}}, {},
    )
    assert is_fail
    assert "timeout" in reason


def test_completed_status_with_good_output_is_not_failure():
    is_fail, _ = _is_failed_run(
        {"status": "completed", "output_params": {"bandgap_ev": 1.1}},
        {"bandgap_ev": 1.1},
    )
    assert is_fail is False


def test_all_nan_outputs_is_failure():
    is_fail, reason = _is_failed_run(
        {"output_params": {"a": float("nan"), "b": float("nan")}}, {},
    )
    assert is_fail
    assert reason == "all-numeric-outputs-nan"


def test_partial_nan_outputs_is_not_failure():
    """At least one finite numeric output → not an all-NaN failure."""
    is_fail, _ = _is_failed_run(
        {"output_params": {"a": float("nan"), "b": 1.0}}, {},
    )
    assert is_fail is False


def test_far_from_target_is_failure():
    # Target 1.1, output 5.0 → ~350% off, well over the 50% threshold.
    is_fail, reason = _is_failed_run(
        {"status": "completed", "output_params": {"bandgap_ev": 5.0}},
        {"bandgap_ev": 1.1},
    )
    assert is_fail
    assert "far-from-target" in reason


def test_close_to_target_is_not_failure():
    is_fail, _ = _is_failed_run(
        {"status": "completed", "output_params": {"bandgap_ev": 1.15}},
        {"bandgap_ev": 1.1},
    )
    assert is_fail is False


def test_far_from_target_requires_all_keys_off():
    """A multi-key target: if only one key is far, it's NOT classified
    as far-from-target (could be a partial success)."""
    is_fail, _ = _is_failed_run(
        {"status": "completed",
         "output_params": {"bandgap_ev": 1.1, "formation_energy": 99.0}},
        {"bandgap_ev": 1.1, "formation_energy": -1.0},
    )
    # bandgap is on target; only formation_energy is off → not all keys.
    assert is_fail is False


def test_classifier_tolerates_inputs_outputs_shape():
    """The in-memory shape (inputs/outputs) works as well as the live
    shape (input_params/output_params)."""
    is_fail, _ = _is_failed_run(
        {"status": "failed", "outputs": {}}, {},
    )
    assert is_fail


def test_classifier_handles_missing_outputs():
    is_fail, _ = _is_failed_run({"status": "completed"}, {})
    assert is_fail is False


# ── _annotate_failures ─────────────────────────────────────────────────


def test_annotate_filters_and_tags():
    records = [
        {"status": "completed", "output_params": {"bandgap_ev": 1.1}},  # good
        {"status": "failed", "output_params": {}},                       # bad
        {"output_params": {"x": float("nan")}},                          # bad
        "not a dict",                                                    # skipped
    ]
    failures = _annotate_failures(records, {"bandgap_ev": 1.1})
    assert len(failures) == 2
    for f in failures:
        assert "_failure_reason" in f


def test_annotate_empty_when_all_good():
    records = [
        {"status": "completed", "output_params": {"bandgap_ev": 1.1}},
    ]
    assert _annotate_failures(records, {"bandgap_ev": 1.1}) == []


# ── Agent search() ─────────────────────────────────────────────────────


def _agent_cls():
    from arc.core.strategies import resolve_role
    return resolve_role("searcher", overrides={"searcher": "negative_results"})


def _agent():
    return _agent_cls()(context=SimpleNamespace(memory={}))


def _resolved_module():
    """The live module object the resolver loaded the agent under.

    The resolver loads by file path with a synthetic module name
    (``arc_strategies.arc_sim2l.agents.searcher_negative``) that isn't
    importable by string. We grab the module object out of sys.modules
    and patch attributes on it directly, since ``mock.patch`` with a
    string target would try (and fail) to import the synthetic name.
    """
    import sys
    return sys.modules[_agent_cls().__module__]


class _PatchHelpers:
    """Context manager that swaps fetch_catalog / fetch_prior_results on
    the resolved module object, restoring them on exit."""

    def __init__(self, catalog, results_by_name):
        self._catalog = catalog
        self._results = results_by_name
        self._mod = _resolved_module()
        self._saved: dict[str, Any] = {}

    def __enter__(self):
        self._saved["fetch_catalog"] = self._mod.fetch_catalog
        self._saved["fetch_prior_results"] = self._mod.fetch_prior_results
        self._mod.fetch_catalog = lambda url, query, limit=5: self._catalog
        self._mod.fetch_prior_results = (
            lambda url, sim_name, limit=3: self._results.get(sim_name, [])
        )
        return self

    def __exit__(self, *exc):
        self._mod.fetch_catalog = self._saved["fetch_catalog"]
        self._mod.fetch_prior_results = self._saved["fetch_prior_results"]
        return False


def _patch_searcher(catalog, results_by_name):
    """Return a single context manager patching both helpers."""
    cm = _PatchHelpers(catalog, results_by_name)
    # Returns the same CM twice so existing `with pc, pr:` call sites
    # still work (entering the same CM twice is a no-op the second time
    # because __enter__ re-reads already-patched attrs — so collapse to
    # one). We adjust the call sites to use a single `with`.
    return cm


def test_search_returns_only_candidates_with_failures():
    catalog = [
        {"id": 1, "name": "sim_a"},
        {"id": 2, "name": "sim_b"},
    ]
    results = {
        "sim_a": [
            {"status": "failed", "output_params": {}},
            {"status": "completed", "output_params": {"bandgap_ev": 1.1}},
        ],
        "sim_b": [
            {"status": "completed", "output_params": {"bandgap_ev": 1.1}},
        ],
    }
    with _patch_searcher(catalog, results):
        result = asyncio.run(_agent().search(
            ResearchGoal(goal="silicon bandgap", domain="materials",
                         target={"bandgap_ev": 1.1}),
        ))
    assert isinstance(result, SearchResult)
    # Only sim_a has a failure → only it is kept.
    names = {h["name"] for h in result.catalog_hits}
    assert names == {"sim_a"}
    # The one failure shows up in prior_results.
    assert len(result.prior_results) == 1
    assert "_failure_reason" in result.prior_results[0]


def test_search_tags_hits_and_counts_failures():
    catalog = [{"id": 1, "name": "sim_a"}]
    results = {
        "sim_a": [
            {"status": "failed", "output_params": {}},
            {"output_params": {"x": float("nan")}},
        ],
    }
    with _patch_searcher(catalog, results):
        result = asyncio.run(_agent().search(ResearchGoal(goal="x")))
    hit = result.catalog_hits[0]
    assert "negative_results" in hit["tags"]
    assert hit["metadata"]["negative_result_count"] == 2


def test_search_empty_when_no_candidates():
    with _patch_searcher([], {}):
        result = asyncio.run(_agent().search(ResearchGoal(goal="x")))
    assert result.catalog_hits == []
    assert result.prior_results == []


def test_search_empty_when_no_failures_anywhere():
    catalog = [{"id": 1, "name": "sim_a"}]
    results = {
        "sim_a": [
            {"status": "completed", "output_params": {"bandgap_ev": 1.1}},
        ],
    }
    with _patch_searcher(catalog, results):
        result = asyncio.run(_agent().search(
            ResearchGoal(goal="x", target={"bandgap_ev": 1.1}),
        ))
    assert result.catalog_hits == []
    assert result.prior_results == []


def test_search_skips_candidates_without_names():
    catalog = [{"id": 1}, {"id": 2, "name": "sim_b"}]  # first has no name
    results = {"sim_b": [{"status": "failed", "output_params": {}}]}
    with _patch_searcher(catalog, results):
        result = asyncio.run(_agent().search(ResearchGoal(goal="x")))
    assert {h["name"] for h in result.catalog_hits} == {"sim_b"}


def test_search_tolerates_helper_exceptions():
    """If the catalog helper raises, search must not crash."""
    def boom(*a, **kw):
        raise RuntimeError("network down")

    mod = _resolved_module()
    saved = mod.fetch_catalog
    mod.fetch_catalog = boom
    try:
        # fetch_catalog itself swallows exceptions and returns [] in the
        # real impl; here we assert our agent doesn't add a new crash
        # path by simulating a helper that raises before returning.
        try:
            result = asyncio.run(_agent().search(ResearchGoal(goal="x")))
        except RuntimeError:
            pytest.fail("search should not propagate helper exceptions")
        assert result.catalog_hits == []
    finally:
        mod.fetch_catalog = saved


# ── Strategy resolver wiring ───────────────────────────────────────────


def test_resolver_returns_negative_results_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role("searcher", overrides={"searcher": "negative_results"})
    assert cls.__name__ == "NegativeResultsSearcherAgent"


def test_default_searcher_unchanged():
    from arc.core.strategies import resolve_role
    assert resolve_role("searcher").__name__ == "KeywordSearcherAgent"
