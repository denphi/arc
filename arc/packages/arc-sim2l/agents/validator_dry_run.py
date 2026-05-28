"""Dry-run / property-based validator.

Domain-neutral post-execution validator. Where ``MaterialsValidatorAgent``
checks outputs against materials-specific physical ranges, this one
checks three *universal* sanity properties that hold for any artifact:

  1. **Finite** — numeric outputs must not be NaN or ±inf. A NaN almost
     always means the workflow crashed mid-compute and returned a
     poisoned value instead of raising. (error)

  2. **Present** — every output key the artifact's ``sim2l_outputs``
     schema declares must appear in the run's outputs. Catches
     workflows that silently drop a return key. (error)

  3. **In-bounds** — when the schema declares ``min`` / ``max`` for an
     output, the value should fall inside. Output bounds are advisory
     (unlike input bounds, which the planner enforces), so a violation
     is a *warning*, not an error. (warning)

The schema comes from ``context.memory["current_artifact"].metadata
["sim2l_outputs"]`` — the canonical location the builder + curator
write. Without a schema we can still run checks 1 and 3-where-possible
and skip the presence check; the validator degrades rather than
refusing.

Use it when you want a cheap, always-applicable safety net regardless
of domain — or stack it conceptually ahead of a domain validator
(run dry_run first to catch structural failures, then a domain
validator for physical plausibility).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from arc.packages.arc_sim2l_agents.validator import _BaseValidator
from arc.schemas.research import ValidatorReport

logger = logging.getLogger(__name__)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _output_schema_from_context(context) -> dict[str, dict]:
    """Pull ``sim2l_outputs`` off the current artifact, or ``{}``.

    Tolerant of every missing-link in the chain (no context, no
    current_artifact, no metadata, no sim2l_outputs) so the validator
    never crashes on a partially-populated memory dict.
    """
    if context is None:
        return {}
    memory = getattr(context, "memory", None) or {}
    artifact = memory.get("current_artifact")
    if artifact is None:
        return {}
    metadata = getattr(artifact, "metadata", None)
    if not isinstance(metadata, dict):
        return {}
    schema = metadata.get("sim2l_outputs")
    return schema if isinstance(schema, dict) else {}


class DryRunValidatorAgent(_BaseValidator):
    """Domain-neutral output sanity validator (finite / present / in-bounds)."""

    name = "dry_run_validator"
    description = (
        "Domain-neutral validator. Checks that numeric outputs are "
        "finite, that every schema-declared output key is present, and "
        "that values fall inside any schema-declared min/max. Works for "
        "any artifact regardless of domain."
    )

    async def validate(
        self,
        outputs: dict[str, Any],
        *,
        target: dict[str, Any] | None = None,  # noqa: ARG002 — kept for sig parity
    ) -> ValidatorReport:
        if not outputs:
            return ValidatorReport(
                passed=False,
                errors=["No outputs to validate."],
            )

        errors: list[str] = []
        warnings: list[str] = []
        evaluations: dict[str, dict[str, Any]] = {}

        # ── 1. Finite check ──────────────────────────────────────────
        for name, value in outputs.items():
            num = _numeric(value)
            if num is None:
                continue
            if math.isnan(num):
                errors.append(f"Output {name!r} is NaN.")
                evaluations[name] = {"passed": False, "reason": "NaN", "value": value}
            elif math.isinf(num):
                errors.append(f"Output {name!r} is infinite.")
                evaluations[name] = {"passed": False, "reason": "inf", "value": value}

        # ── 2 + 3. Presence + bounds (schema-driven) ────────────────
        schema = _output_schema_from_context(self.context)
        for key, spec in schema.items():
            if key not in outputs:
                errors.append(f"Schema-declared output {key!r} is missing.")
                evaluations.setdefault(
                    key, {"passed": False, "reason": "missing"},
                )
                continue
            if not isinstance(spec, dict):
                continue
            value = _numeric(outputs.get(key))
            lo, hi = spec.get("min"), spec.get("max")
            if value is None or lo is None or hi is None:
                continue
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
                continue
            if value < lo or value > hi:
                warnings.append(
                    f"Output {key!r}={value:.4g} outside declared bounds "
                    f"[{lo}, {hi}]."
                )
                evaluations.setdefault(key, {})
                evaluations[key].update({
                    "in_bounds": False, "value": value,
                    "min": lo, "max": hi,
                })
            else:
                evaluations.setdefault(key, {})
                evaluations[key].update({"in_bounds": True, "value": value})

        return ValidatorReport(
            passed=not errors,
            errors=errors,
            warnings=warnings,
            evaluations=evaluations,
        )
