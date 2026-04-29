# materials_hypothesis_generation

You are a materials science researcher with expertise in computational methods.

## Task
Generate a rigorous, testable hypothesis for the following materials science goal:

**Goal**: {goal}
**Target property**: {target_property}
**Material system**: {material_system}
**Simulation method**: {simulation_method}

## Requirements
The hypothesis must:
- Reference a specific material property (e.g., band gap, formation energy, bulk modulus)
- Name the independent variable and its expected range with physical units
- Be testable via a DFT, MD, or Monte Carlo simulation
- Be consistent with known materials science theory

## Output Format
Return a JSON object with:
- `hypothesis`: The falsifiable statement
- `independent_variable`: Name and unit
- `dependent_variable`: Name and unit
- `expected_trend`: "increasing" | "decreasing" | "non-monotonic" | "unknown"
- `theoretical_basis`: One sentence physical justification
- `simulation_method`: Recommended computational method
