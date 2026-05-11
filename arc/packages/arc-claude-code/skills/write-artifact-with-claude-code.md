# Write Artifact With Claude Code

## Description

Use Claude Code as the coding backend to create a Sim2L artifact from an ARC experiment plan.

## Inputs

- `ExperimentPlan` with parameters, sweeps, constraints, and success criteria.

## Output

- `ArtifactDraft` containing `workflow.py`, `sim2l.yaml`, and metadata.
