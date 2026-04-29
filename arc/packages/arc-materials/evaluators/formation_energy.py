"""Formation energy evaluator for materials science domain."""

from typing import Any


class FormationEnergyEvaluator:
    """Validates formation energy results.

    Formation energy (eV/atom) indicates thermodynamic stability:
    - Negative: thermodynamically stable compound
    - Positive: metastable or unstable
    - Near zero: marginally stable

    Practical range: -5.0 to +2.0 eV/atom
    """

    PHYSICAL_MIN = -5.0   # eV/atom
    PHYSICAL_MAX = 2.0    # eV/atom

    def evaluate(self, outputs: dict[str, Any]) -> dict[str, Any]:
        ef = outputs.get("formation_energy") or outputs.get("e_formation")

        if ef is None:
            return {
                "passed": False,
                "reason": "formation_energy not found in outputs",
                "value": None,
                "stable": None,
            }

        try:
            ef = float(ef)
        except (TypeError, ValueError):
            return {
                "passed": False,
                "reason": f"formation_energy value is not numeric: {ef}",
                "value": ef,
                "stable": None,
            }

        in_range = self.PHYSICAL_MIN <= ef <= self.PHYSICAL_MAX
        stable = ef < 0.0

        return {
            "passed": in_range,
            "value": ef,
            "unit": "eV/atom",
            "stable": stable,
            "reason": (
                f"Formation energy {ef:.3f} eV/atom — "
                + ("thermodynamically stable." if stable else "metastable or unstable.")
                if in_range
                else f"Value {ef} eV/atom outside physical range [{self.PHYSICAL_MIN}, {self.PHYSICAL_MAX}]."
            ),
        }
