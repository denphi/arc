# create-sim2l

## Description
Creates a new Sim2L artifact from an experiment plan. Generates the workflow script, Sim2L metadata YAML, and registers the artifact in the artifact registry.

## Inputs
- `plan` (ExperimentPlan): The experiment plan defining parameters, strategy, and success criteria.
- `registry` (ArtifactRegistry): The artifact registry to register the draft into.

## Outputs
- `artifact` (ArtifactRecord): The registered artifact record with ID, version, and path.

## Steps
1. Invoke `Sim2LBuilderAgent` with the experiment plan to generate an `ArtifactDraft`.
2. Register the draft in the `ArtifactRegistry` to obtain an `ArtifactRecord`.
3. Log the creation action in the `ProvenanceLog`.
4. Return the `ArtifactRecord`.

## Notes
- The builder agent may use an LLM provider if configured, or fall back to template-based generation.
- If the artifact already exists (same name and version), increment the version.
