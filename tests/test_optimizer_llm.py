"""LLM-guided optimizer.

Pins the run-contract compatibility with the other optimizers (GA /
BayesOpt / CMA-ES) so ``/optimize`` and recipes can swap between any of
the four without caller changes. Most tests exercise the fallback path
(no provider on memory) because we don't want CI to depend on an LLM;
the provider path is exercised with a scripted ``FakeProvider`` stub.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from arc.packages.arc_sim2l_agents.optimizer_llm import (
    LLMGuidedOptimizerAgent,
    _lhs_candidates,
    _parse_candidates,
)


pytestmark = pytest.mark.chat


# ── Fixtures ────────────────────────────────────────────────────────────


def _artifact(schema=None):
    return SimpleNamespace(
        artifact_id="llm-opt-test",
        name="bandgap",
        path="/tmp/does-not-exist",
        metadata={"sim2l_inputs": schema or {
            "thickness":   {"default": 5.0,   "min": 1.0,   "max": 10.0},
            "temperature": {"default": 300.0, "min": 100.0, "max": 600.0},
        }},
    )


def _adapter(outputs_fn):
    async def _run(artifact, inputs):
        return SimpleNamespace(outputs=outputs_fn(inputs))
    return SimpleNamespace(run=_run)


def _agent(adapter, *, provider=None):
    memory = {"adapter": adapter, "schema_registry": {}}
    if provider is not None:
        memory["provider"] = provider
    return LLMGuidedOptimizerAgent(context=SimpleNamespace(memory=memory))


class _ScriptedProvider:
    """LLM stub that returns a queue of prebuilt responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            return ""
        return self.responses.pop(0)


# ── _parse_candidates ──────────────────────────────────────────────────


_BOUNDS = {"thickness": (1.0, 10.0), "temperature": (100.0, 600.0)}


def test_parse_candidates_accepts_plain_json_list():
    raw = '[{"thickness": 5.2, "temperature": 298.0}]'
    out = _parse_candidates(raw, _BOUNDS, n_points=1)
    assert out == [{"thickness": 5.2, "temperature": 298.0}]


def test_parse_candidates_strips_code_fences():
    raw = "```json\n[{\"thickness\": 3, \"temperature\": 200}]\n```"
    out = _parse_candidates(raw, _BOUNDS, n_points=1)
    assert out == [{"thickness": 3.0, "temperature": 200.0}]


def test_parse_candidates_extracts_json_from_surrounding_prose():
    raw = "Here are two candidates:\n[{\"thickness\": 2, \"temperature\": 150}]"
    out = _parse_candidates(raw, _BOUNDS, n_points=1)
    assert len(out) == 1
    assert out[0]["thickness"] == 2.0


def test_parse_candidates_clamps_overshoots_into_bounds():
    raw = '[{"thickness": 9999, "temperature": -100}]'
    out = _parse_candidates(raw, _BOUNDS, n_points=1)
    assert out[0]["thickness"] == 10.0
    assert out[0]["temperature"] == 100.0


def test_parse_candidates_drops_rows_missing_required_keys():
    raw = '[{"thickness": 5.0}, {"thickness": 6.0, "temperature": 300.0}]'
    out = _parse_candidates(raw, _BOUNDS, n_points=2)
    assert len(out) == 1
    assert out[0]["thickness"] == 6.0


def test_parse_candidates_drops_non_numeric_values():
    raw = '[{"thickness": "five", "temperature": 300.0}]'
    assert _parse_candidates(raw, _BOUNDS, n_points=1) == []


def test_parse_candidates_returns_empty_for_bad_json():
    assert _parse_candidates("not even close to JSON", _BOUNDS, n_points=2) == []


def test_parse_candidates_caps_at_n_points():
    raw = json.dumps([
        {"thickness": 1.5, "temperature": 150.0},
        {"thickness": 2.5, "temperature": 250.0},
        {"thickness": 3.5, "temperature": 350.0},
    ])
    out = _parse_candidates(raw, _BOUNDS, n_points=2)
    assert len(out) == 2


# ── _lhs_candidates ────────────────────────────────────────────────────


def test_lhs_candidates_respects_bounds():
    points = _lhs_candidates(_BOUNDS, n_points=8)
    assert len(points) == 8
    for p in points:
        assert 1.0 <= p["thickness"] <= 10.0
        assert 100.0 <= p["temperature"] <= 600.0


def test_lhs_candidates_deterministic_with_seed():
    a = _lhs_candidates(_BOUNDS, n_points=8, seed=7)
    b = _lhs_candidates(_BOUNDS, n_points=8, seed=7)
    assert a == b


def test_lhs_candidates_zero_dim():
    assert _lhs_candidates({}, n_points=4) == []


# ── Contract: same shape as GA / BO / CMA-ES ───────────────────────────


def test_run_requires_adapter_in_context():
    art = _artifact()
    bad_ctx = SimpleNamespace(memory={})
    agent = LLMGuidedOptimizerAgent(context=bad_ctx)
    with pytest.raises(RuntimeError, match="adapter"):
        asyncio.run(agent.run(art, {"bandgap_ev": 1.1}))


def test_run_raises_when_no_schema():
    art = SimpleNamespace(
        artifact_id="x", name="x", path="/tmp/nope",
        metadata={"sim2l_inputs": {}},
    )
    agent = _agent(_adapter(lambda inputs: {}))
    with pytest.raises(ValueError, match="schema"):
        asyncio.run(agent.run(art, {}))


def test_run_returns_expected_result_shape_via_fallback():
    """No provider → LHS fallback → still returns the canonical dict."""
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.0}))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=2, pop_size=3,
    ))
    assert set(result.keys()) >= {
        "best_inputs", "best_outputs", "best_fitness",
        "generations_run", "converged", "history",
    }
    assert result["generations_run"] >= 1


def test_run_accepts_and_ignores_elite_frac():
    """GA-specific kwarg should be accepted silently for sig parity."""
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.1}))
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=1, pop_size=2, elite_frac=0.5,
    ))


def test_run_calls_on_generation_per_gen():
    art = _artifact()
    calls: list = []

    async def _cb(gen, point, out, fit):
        calls.append((gen, dict(point), dict(out)))

    # Adapter outputs the target → converges on first gen.
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.1}))
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=3, pop_size=2,
        on_generation=_cb,
    ))
    assert len(calls) >= 1
    # Generation indices are monotone.
    assert calls == sorted(calls, key=lambda c: c[0])


def test_run_converges_when_target_in_bounds():
    """Evaluator returns the target → BO stops early."""
    art = _artifact()
    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.1}))
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=5, pop_size=3,
        convergence_threshold=0.01,
    ))
    assert result["converged"] is True


# ── Provider path ──────────────────────────────────────────────────────


def test_run_prompts_provider_when_available():
    """When a provider is on memory, the agent should call ``complete``
    with a prompt that includes the parameter bounds and target."""
    art = _artifact()
    provider = _ScriptedProvider([
        json.dumps([{"thickness": 5.0, "temperature": 300.0}]),
    ])
    agent = _agent(
        _adapter(lambda inputs: {"bandgap_ev": 2.0}),
        provider=provider,
    )
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=1, pop_size=1,
    ))
    assert provider.prompts
    p = provider.prompts[0]
    assert "thickness" in p
    assert "temperature" in p
    assert "bandgap_ev" in p   # target rendered into prompt


def test_run_uses_llm_candidates_directly():
    """An LLM-supplied point that's already optimal is what gets reported."""
    art = _artifact()
    provider = _ScriptedProvider([
        json.dumps([{"thickness": 5.5, "temperature": 305.0}]),
    ])

    seen: list = []

    def _outputs(inputs):
        seen.append(dict(inputs))
        return {"bandgap_ev": 1.1 if inputs["thickness"] == 5.5 else 0.5}

    agent = _agent(_adapter(_outputs), provider=provider)
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.1},
        max_generations=1, pop_size=1,
    ))
    # The single point the LLM picked is what we evaluated.
    assert seen == [{"thickness": 5.5, "temperature": 305.0}]
    assert result["best_inputs"]["thickness"] == 5.5


def test_run_tops_up_with_lhs_when_llm_returns_too_few():
    """LLM returned 1 of 3 requested → 2 LHS candidates fill the rest."""
    art = _artifact()
    provider = _ScriptedProvider([
        json.dumps([{"thickness": 5.0, "temperature": 300.0}]),
    ])

    seen: list = []

    def _outputs(inputs):
        seen.append(dict(inputs))
        return {"bandgap_ev": 2.0}

    agent = _agent(_adapter(_outputs), provider=provider)
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=1, pop_size=3,
        convergence_threshold=0.0,
    ))
    # 3 evaluations in 1 gen even though the LLM only gave 1 candidate.
    assert len(seen) == 3


def test_run_falls_back_to_lhs_when_provider_returns_garbage():
    """LLM response isn't JSON → fallback should still fill the gen."""
    art = _artifact()
    provider = _ScriptedProvider(["not valid JSON at all"])

    n_evals = [0]

    def _outputs(inputs):
        n_evals[0] += 1
        return {"bandgap_ev": 1.5}

    agent = _agent(_adapter(_outputs), provider=provider)
    asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=1, pop_size=4,
        convergence_threshold=0.0,
    ))
    assert n_evals[0] == 4


def test_run_swallows_provider_exceptions():
    """A provider that raises must not crash the optimizer."""
    art = _artifact()

    class _BoomProvider:
        async def complete(self, prompt):
            raise RuntimeError("LLM is down")

    agent = _agent(_adapter(lambda inputs: {"bandgap_ev": 1.5}),
                   provider=_BoomProvider())
    result = asyncio.run(agent.run(
        art, target={"bandgap_ev": 1.0},
        max_generations=1, pop_size=2,
        convergence_threshold=0.0,
    ))
    # Optimizer still finished a generation via the LHS fallback.
    assert result["generations_run"] >= 1


# ── Strategy resolver wiring ───────────────────────────────────────────


def test_resolver_returns_llm_guided_class():
    from arc.core.strategies import resolve_role
    cls = resolve_role("optimizer", overrides={"optimizer": "llm_guided"})
    assert cls.__name__ == "LLMGuidedOptimizerAgent"


def test_default_optimizer_unchanged():
    from arc.core.strategies import resolve_role
    assert resolve_role("optimizer").__name__ == "GeneticOptimizerAgent"
