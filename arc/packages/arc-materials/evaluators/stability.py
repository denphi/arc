"""Structure stability evaluator."""

from typing import Any


class StructureStabilityEvaluator:
    """Checks structural stability indicators from simulation outputs."""

    def evaluate(self, outputs: dict[str, Any]) -> dict[str, Any]:
        energy = outputs.get("total_energy")
        forces_max = outputs.get("max_force")
        converged = outputs.get("converged", True)

        flags = []
        if energy is None:
            flags.append("total_energy missing from outputs")
        if forces_max is not None:
            # Tolerate a non-numeric max_force rather than raising — match
            # the other evaluators, which never crash the validate step on a
            # bad output value.
            try:
                forces_val = float(forces_max)
            except (TypeError, ValueError):
                flags.append(f"max_force is not numeric: {forces_max!r}")
            else:
                if forces_val > 0.05:
                    flags.append(
                        f"Max force {forces_val} eV/Å exceeds convergence "
                        "threshold 0.05 eV/Å"
                    )
        if not converged:
            flags.append("Simulation did not converge")

        passed = len(flags) == 0

        return {
            "passed": passed,
            "converged": converged,
            "max_force": forces_max,
            "total_energy": energy,
            "flags": flags,
            "reason": "Structure is stable." if passed else "; ".join(flags),
        }
