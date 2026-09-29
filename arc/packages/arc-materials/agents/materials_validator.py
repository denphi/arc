"""Materials-domain validator that wraps the arc-materials evaluators.

The evaluator classes in :mod:`arc.packages.arc-materials.evaluators`
(bandgap, formation_energy, stability, property_prediction) have been
sitting inert for several releases — declared in package.yaml but never
invoked. This validator gives them a home.

It runs every evaluator whose target output key appears in the run's
outputs (e.g. only invokes the bandgap evaluator if outputs contain a
``band_gap`` / ``bandgap`` / ``bandgap_ev`` key). Each evaluator's verdict
becomes a row in :attr:`ValidatorReport.evaluations`; the aggregate
``passed`` is True iff every applicable evaluator passed.

Errors come from out-of-physical-range outputs (e.g. negative bandgap).
Warnings come from in-range but suspicious values (e.g. formation
energy near zero — neither stable nor unstable).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from arc.packages.arc_sim2l.agents.validator import _BaseValidator

from arc.schemas.research import ValidatorReport

logger = logging.getLogger(__name__)


def _load_evaluator_module(filename: str):
    """Import one of the arc-materials evaluator modules (e.g. ``bandgap.py``).

    Uses the package's canonical import name so evaluators share a single
    module object with every other importer.
    """
    stem = filename.removesuffix(".py").replace("/", ".")
    return importlib.import_module(f"arc.packages.arc_materials.evaluators.{stem}")


def _output_keys_present(outputs: dict[str, Any], candidates: tuple[str, ...]) -> bool:
    return any(k in outputs for k in candidates)


class MaterialsValidatorAgent(_BaseValidator):
    """Runs every applicable arc-materials evaluator on the run's outputs."""

    name = "materials_validator"
    description = (
        "Runs the arc-materials evaluators (band gap, formation energy, "
        "stability, property prediction) on the run's outputs. Each "
        "evaluator's verdict appears in ``evaluations``. The aggregate "
        "``passed`` is True iff every applicable evaluator passed."
    )

    # Pairs of (evaluator module filename, output-key candidates).
    # Evaluators that don't have any matching key in the run outputs
    # are skipped — we don't want a bandgap-only run to fail because
    # it didn't produce a formation energy.
    _EVALUATORS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("bandgap.py", "BandgapEvaluator",
         ("band_gap", "bandgap", "bandgap_ev")),
        ("formation_energy.py", "FormationEnergyEvaluator",
         ("formation_energy", "e_formation")),
        ("stability.py", "StructureStabilityEvaluator",
         ("stability", "is_stable", "structure_stability")),
        ("property_prediction.py", "PropertyPredictionEvaluator",
         ("predicted_property", "property_value", "prediction")),
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
        any_applicable = False

        for filename, cls_name, keys in self._EVALUATORS:
            if not _output_keys_present(outputs, keys):
                continue
            any_applicable = True
            try:
                module = _load_evaluator_module(filename)
                evaluator = getattr(module, cls_name)()
                verdict = evaluator.evaluate(outputs) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("Evaluator %s failed: %s", cls_name, exc)
                evaluations[cls_name] = {"passed": False, "error": str(exc)}
                errors.append(f"{cls_name} raised: {exc}")
                continue

            evaluations[cls_name] = verdict
            if not verdict.get("passed", True):
                reason = verdict.get("reason") or f"{cls_name} reported failure"
                errors.append(reason)
            else:
                # In-range outputs may still earn a "marginal" warning,
                # e.g. formation energy near zero (metastable).
                if cls_name == "FormationEnergyEvaluator":
                    value = verdict.get("value")
                    if isinstance(value, (int, float)) and -0.05 < value < 0.05:
                        warnings.append(
                            f"Formation energy {value:.3f} eV/atom is near zero "
                            "— marginally stable."
                        )

        # If no evaluator's target keys appeared, the run wasn't a
        # materials run after all — be neutral rather than failing.
        if not any_applicable:
            return ValidatorReport(
                passed=True,
                warnings=["materials_validator: no materials-domain outputs "
                          "found; skipping."],
            )

        return ValidatorReport(
            passed=not errors,
            errors=errors,
            warnings=warnings,
            evaluations=evaluations,
        )
