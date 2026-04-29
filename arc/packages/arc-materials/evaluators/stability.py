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
        if forces_max is not None and float(forces_max) > 0.05:
            flags.append(f"Max force {forces_max} eV/Å exceeds convergence threshold 0.05 eV/Å")
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
