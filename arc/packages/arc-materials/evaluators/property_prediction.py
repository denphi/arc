"""Generic property prediction evaluator."""

from typing import Any


# Known physical bounds for common materials properties.
PROPERTY_BOUNDS: dict[str, tuple[float, float]] = {
    "band_gap": (0.0, 14.0),          # eV
    "formation_energy": (-5.0, 2.0),  # eV/atom
    "bulk_modulus": (0.0, 600.0),     # GPa
    "shear_modulus": (0.0, 400.0),    # GPa
    "youngs_modulus": (0.0, 1200.0),  # GPa
    "density": (0.5, 25.0),           # g/cm³
    "thermal_conductivity": (0.01, 3000.0),  # W/m·K
    "seebeck_coefficient": (-1000.0, 1000.0),  # µV/K
}


class PropertyPredictionEvaluator:
    """Checks predicted materials properties against known physical bounds."""

    def evaluate(self, outputs: dict[str, Any]) -> dict[str, Any]:
        results = {}
        all_passed = True

        for prop, (lo, hi) in PROPERTY_BOUNDS.items():
            value = outputs.get(prop)
            if value is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                results[prop] = {"passed": False, "reason": f"Non-numeric value: {value}"}
                all_passed = False
                continue

            passed = lo <= v <= hi
            results[prop] = {
                "passed": passed,
                "value": v,
                "bounds": [lo, hi],
                "reason": "Within bounds." if passed else f"{v} outside [{lo}, {hi}].",
            }
            if not passed:
                all_passed = False

        return {
            "passed": all_passed,
            "properties": results,
            "summary": (
                "All predicted properties within physical bounds."
                if all_passed
                else "Some properties outside physical bounds — review outputs."
            ),
        }
