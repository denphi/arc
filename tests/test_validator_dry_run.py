"""DryRunValidatorAgent — domain-neutral output sanity validator.

Three universal properties:
  1. finite      — numeric outputs not NaN / ±inf  (error)
  2. present     — every schema-declared output key appears  (error)
  3. in-bounds   — values inside schema min/max  (warning)

Schema comes from ``context.memory["current_artifact"].metadata
["sim2l_outputs"]``. Degrades gracefully when no schema is present.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arc.schemas.research import ValidatorReport


pytestmark = pytest.mark.chat


def _artifact(output_schema=None):
    return SimpleNamespace(
        artifact_id="dry-run-test",
        metadata={"sim2l_outputs": output_schema or {}},
    )


def _ctx(output_schema=None):
    memory = {}
    if output_schema is not None:
        memory["current_artifact"] = _artifact(output_schema)
    return SimpleNamespace(memory=memory)


def _cls():
    from arc.core.strategies import resolve_role
    return resolve_role("validator", overrides={"validator": "dry_run"})


def _validate(outputs, output_schema=None, target=None):
    agent = _cls()(context=_ctx(output_schema))
    return asyncio.run(agent.validate(outputs, target=target))


# ── Resolver wiring ────────────────────────────────────────────────────


def test_resolver_returns_dry_run_class():
    assert _cls().__name__ == "DryRunValidatorAgent"


def test_default_validator_unchanged():
    from arc.core.strategies import resolve_role
    assert resolve_role("validator").__name__ == "NoopValidatorAgent"


# ── Contract ──────────────────────────────────────────────────────────


def test_returns_validator_report():
    report = _validate({"x": 1.0})
    assert isinstance(report, ValidatorReport)


def test_empty_outputs_fails():
    report = _validate({})
    assert report.passed is False
    assert report.errors


# ── 1. Finite check ───────────────────────────────────────────────────


def test_passes_finite_outputs():
    report = _validate({"a": 1.0, "b": -3.5, "c": 0.0})
    assert report.passed is True
    assert report.errors == []


def test_fails_on_nan():
    report = _validate({"a": float("nan")})
    assert report.passed is False
    assert any("NaN" in e for e in report.errors)


def test_fails_on_positive_inf():
    report = _validate({"a": float("inf")})
    assert report.passed is False
    assert any("infinite" in e.lower() for e in report.errors)


def test_fails_on_negative_inf():
    report = _validate({"a": float("-inf")})
    assert report.passed is False


def test_ignores_non_numeric_outputs_for_finiteness():
    """A string output isn't subject to the finite check."""
    report = _validate({"label": "silicon", "value": 1.0})
    assert report.passed is True


def test_bool_outputs_not_treated_as_numeric():
    """``True`` is an int subclass but shouldn't trip the finite check."""
    report = _validate({"is_stable": True, "value": 1.0})
    assert report.passed is True


# ── 2. Presence check ─────────────────────────────────────────────────


def test_fails_when_schema_key_missing():
    schema = {
        "bandgap_ev": {"type": "Number"},
        "formation_energy": {"type": "Number"},
    }
    # Output only has bandgap_ev — formation_energy missing.
    report = _validate({"bandgap_ev": 1.1}, output_schema=schema)
    assert report.passed is False
    assert any("formation_energy" in e and "missing" in e.lower()
               for e in report.errors)


def test_passes_when_all_schema_keys_present():
    schema = {"bandgap_ev": {"type": "Number"}}
    report = _validate({"bandgap_ev": 1.1}, output_schema=schema)
    assert report.passed is True


def test_no_schema_skips_presence_check():
    """Without a current_artifact, presence can't be checked — only
    finiteness applies."""
    report = _validate({"anything": 1.0})  # no schema in context
    assert report.passed is True


# ── 3. Bounds check (warnings, not errors) ────────────────────────────


def test_out_of_bounds_is_warning_not_error():
    schema = {"bandgap_ev": {"type": "Number", "min": 0.0, "max": 14.0}}
    report = _validate({"bandgap_ev": 99.0}, output_schema=schema)
    # Out of bounds → warning, but still "passed" (no hard error).
    assert report.passed is True
    assert any("outside declared bounds" in w for w in report.warnings)


def test_in_bounds_no_warning():
    schema = {"bandgap_ev": {"type": "Number", "min": 0.0, "max": 14.0}}
    report = _validate({"bandgap_ev": 1.1}, output_schema=schema)
    assert report.passed is True
    assert report.warnings == []
    assert report.evaluations["bandgap_ev"]["in_bounds"] is True


def test_bounds_below_min_warns():
    schema = {"x": {"min": 0.0, "max": 10.0}}
    report = _validate({"x": -5.0}, output_schema=schema)
    assert any("outside" in w for w in report.warnings)


def test_schema_without_bounds_skips_bounds_check():
    schema = {"x": {"type": "Number"}}  # no min/max
    report = _validate({"x": 999.0}, output_schema=schema)
    assert report.passed is True
    assert report.warnings == []


def test_partial_bounds_skipped():
    """A schema with only ``min`` (no ``max``) skips the bounds check
    rather than half-applying it."""
    schema = {"x": {"min": 0.0}}
    report = _validate({"x": -5.0}, output_schema=schema)
    # No max → bounds check skipped, no warning.
    assert report.warnings == []


# ── Combined + robustness ─────────────────────────────────────────────


def test_nan_and_missing_both_reported():
    schema = {"a": {"type": "Number"}, "b": {"type": "Number"}}
    report = _validate({"a": float("nan")}, output_schema=schema)
    assert report.passed is False
    # NaN on 'a' AND 'b' missing → two errors.
    assert any("NaN" in e for e in report.errors)
    assert any("b" in e and "missing" in e.lower() for e in report.errors)


def test_evaluations_populated_per_output():
    schema = {"x": {"min": 0.0, "max": 10.0}}
    report = _validate({"x": 5.0}, output_schema=schema)
    assert "x" in report.evaluations
    assert report.evaluations["x"]["value"] == 5.0


def test_handles_artifact_without_metadata():
    """A current_artifact whose metadata isn't a dict shouldn't crash."""
    ctx = SimpleNamespace(memory={
        "current_artifact": SimpleNamespace(metadata=None),
    })
    agent = _cls()(context=ctx)
    report = asyncio.run(agent.validate({"x": 1.0}))
    assert report.passed is True


def test_handles_no_context():
    """Constructed with the default context (no memory) — must not crash."""
    from arc.packages.arc_sim2l_agents.validator_dry_run import (
        DryRunValidatorAgent,
    )
    agent = DryRunValidatorAgent()  # default AgentContext
    report = asyncio.run(agent.validate({"x": 1.0}))
    assert report.passed is True


# ── Execution-phase integration ───────────────────────────────────────


def test_dry_run_validator_through_execution_phase():
    """End-to-end: ExecutionPhase resolves dry_run and stores the report
    on state.extras."""
    from arc.chat.research.pipeline import PipelineState
    from arc.schemas.execution import ExecutionResult

    async def _prepare_inputs(artifact, run_inputs):
        return run_inputs or {"thickness": 5.0}

    async def _run(artifact, inputs):
        return ExecutionResult(
            run_id="e1", status="completed",
            outputs={"bandgap_ev": float("nan")},  # NaN → dry_run fails
            logs=[],
        )

    workflow = SimpleNamespace(
        adapter=SimpleNamespace(prepare_inputs=_prepare_inputs, run=_run),
        results=SimpleNamespace(save=lambda e: None),
        _context=SimpleNamespace(
            memory={"strategy_overrides": {"validator": "dry_run"}},
        ),
    )
    artifact = SimpleNamespace(
        artifact_id="a1",
        metadata={"sim2l_inputs": {"thickness": {}},
                  "sim2l_outputs": {"bandgap_ev": {"type": "Number"}}},
    )
    state = PipelineState(
        workflow=workflow,
        goal_text="x",
        artifact=artifact,
        is_new_artifact=True,
    )
    from arc.chat.research.phases import ExecutionPhase
    asyncio.run(ExecutionPhase().run(state))
    report = state.extras.get("validator_report")
    assert report is not None
    assert report.passed is False
    assert any("NaN" in e for e in report.errors)
