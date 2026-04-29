"""Band gap evaluator for materials science domain."""

from typing import Any


class BandgapEvaluator:
    """Validates and scores band gap results from materials simulations.

    Known physical ranges (eV):
    - Metals / semimetals: < 0.1
    - Narrow-gap semiconductors: 0.1 – 1.0
    - Semiconductors: 1.0 – 3.5
    - Wide-bandgap semiconductors: 3.5 – 6.0
    - Insulators: > 6.0
    """

    PHYSICAL_MIN = 0.0    # eV (metals have 0)
    PHYSICAL_MAX = 14.0   # eV (practical upper limit for simulation outputs)

    def evaluate(self, outputs: dict[str, Any]) -> dict[str, Any]:
        band_gap = outputs.get("band_gap") or outputs.get("bandgap")

        if band_gap is None:
            return {
                "passed": False,
                "reason": "band_gap not found in outputs",
                "value": None,
                "classification": None,
            }

        try:
            band_gap = float(band_gap)
        except (TypeError, ValueError):
            return {
                "passed": False,
                "reason": f"band_gap value is not numeric: {band_gap}",
                "value": band_gap,
                "classification": None,
            }

        passed = self.PHYSICAL_MIN <= band_gap <= self.PHYSICAL_MAX

        if band_gap < 0.1:
            classification = "metal"
        elif band_gap < 1.0:
            classification = "narrow_gap_semiconductor"
        elif band_gap < 3.5:
            classification = "semiconductor"
        elif band_gap < 6.0:
            classification = "wide_bandgap_semiconductor"
        else:
            classification = "insulator"

        return {
            "passed": passed,
            "value": band_gap,
            "unit": "eV",
            "classification": classification,
            "reason": (
                "Value within physical range."
                if passed
                else f"Value {band_gap} eV outside physical range [{self.PHYSICAL_MIN}, {self.PHYSICAL_MAX}]."
            ),
        }
