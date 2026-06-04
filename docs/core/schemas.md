# Schemas

*The pydantic data model that flows through the loop.*

Every payload between roles is a typed pydantic model (`arc/schemas/`). They
are the contract for what each loop step produces and consumes.

## Research models (`arc/schemas/research.py`)

| Model | Produced by | Key fields |
|---|---|---|
| `ResearchGoal` | the user / CLI / API | `goal`, `domain`, `constraints`, `target`, `mode` |
| `ResearchProposal` | `ideator` | `hypothesis`, `objective`, `variables`, `methodology`, `expected_outcomes`, `evaluation_metrics`, `risk_level` |
| `ExperimentPlan` | `planner` | `proposal`, `artifact_strategy`, `parameters`, `parameter_sweep`, `parameter_constraints`, `experimental_design`, `success_criteria` |
| `SearchResult` | `searcher` | `catalog_hits`, `prior_results` |
| `ValidatorReport` | `validator` | `passed`, `errors`, `warnings`, `evaluations` |

## Artifact models (`arc/schemas/artifact.py`)

| Model | Meaning |
|---|---|
| `ArtifactDraft` | a newly built artifact: `name`, `description`, `files`, `metadata` |
| `ArtifactRecord` | a registered artifact: `artifact_id`, `version`, `state`, `path`, `metadata` |
| `ValidationResult` | pre-execution check: `valid`, `errors`, `warnings`, `test_run_id` |

## Execution & review (`arc/schemas/execution.py`, `review.py`)

| Model | Meaning |
|---|---|
| `ExecutionRequest` | `artifact_id`, `version`, `inputs` |
| `ExecutionResult` | `run_id`, `status`, `outputs`, `logs`, `metrics` |
| `ReviewResult` | `approved`, `summary`, `strengths`, `weaknesses`, `recommendations`, `next_parameters`, `iteration_complete`, `strategy` |

## API reference

```{eval-rst}
.. automodule:: arc.schemas.research
   :members:
   :undoc-members:

.. automodule:: arc.schemas.artifact
   :members:
   :undoc-members:

.. automodule:: arc.schemas.execution
   :members:
   :undoc-members:

.. automodule:: arc.schemas.review
   :members:
   :undoc-members:
```
