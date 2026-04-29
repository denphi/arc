# improve-artifact

## Description
Takes a review result and an existing artifact draft, and produces an improved artifact draft incorporating the reviewer's recommendations.

## Inputs
- `artifact` (ArtifactDraft): The current artifact to improve.
- `review` (ReviewResult): The reviewer's feedback including weaknesses and recommendations.
- `plan` (ExperimentPlan): The original experiment plan for context.

## Outputs
- `improved_artifact` (ArtifactDraft): A new artifact draft with improvements applied.

## Steps
1. Extract `weaknesses` and `recommendations` from the review.
2. If an LLM provider is available: prompt it with the current artifact files + review feedback to generate improved files.
3. If no LLM provider: apply heuristic improvements (e.g., widen parameter ranges, add output assertions).
4. Increment version metadata.
5. Return the improved `ArtifactDraft`.

## Notes
- The improved draft must still be registered via `create-sim2l` before it can be executed.
- Preserve existing metadata; only update `files` and relevant `metadata` fields.
