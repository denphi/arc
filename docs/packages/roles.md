# The role catalogue

*The nine loop roles, their default and alternative strategies, and the
composite-stack merge semantics.*

A **role** is a named slot in the research loop. The {doc}`strategy resolver
<../core/strategies>` maps a role to a strategy (a class), or to an ordered
**stack** of them.

## The nine roles

| Role | Phase | Default | Bundled alternatives |
|---|---|---|---|
| `ideator` | goal → proposal | `IdeatorAgent` | `constraint_aware` |
| `searcher` | catalog + prior-results lookup | `KeywordSearcherAgent` | `embeddings`, `materials_project`, `negative_results`, `github` |
| `planner` | proposal → experiment plan | `PlannerAgent` | `mars_planner`, `active_learning`, `doe_lhs`, `doe_factorial`, `doe_sobol` |
| `builder` | plan → artifact draft | `Sim2LBuilderAgent` | `codex`, `claude_code` |
| `validator` | grade outputs (post-execution) | `NoopValidatorAgent` | `materials_evaluators`, `dry_run` |
| `reviewer` | does the run satisfy the goal? | `ReviewerAgent` | `reflective`, `comparative` |
| `reflector` | extract lessons | `ReflectorAgent` | `skill_extracting`, `failure_clustering` |
| `optimizer` | search the input space | `GeneticOptimizerAgent` | `bayesopt`, `cmaes`, `llm_guided` |
| `curator` | canonicalise output keys | `CuratorAgent` | — |

```{note}
**Not roles:** the publish steps (Register/Persist/Record) are
{doc}`backend actions <../core/backends>`, and the Execute step is the
{doc}`runtime adapter <../core/runtime-adapters>`. Neither is selectable via
`/strategy`.
```

## Composite stacks

When a selector names more than one strategy, each role's composite class
merges the components deterministically:

| Role | Composite behaviour |
|---|---|
| `searcher` | run sources left-to-right; dedupe catalog hits + prior results; tag each hit with `metadata.search_strategy`. |
| `ideator` | run each ideator, collect candidates, rank, return one primary proposal; record alternates. |
| `planner` | first plan is the base; `parameters` first-writer-wins; intersect numeric constraints; concatenate unique sweep values; append design labels. |
| `builder` | fallback order — first builder that produces an artifact wins; failures recorded. |
| `validator` | run all; `passed = all(report.passed)`; concat errors/warnings; namespace evaluations. |
| `reviewer` | consensus — approve only if all approve; tag + concat summaries; most-conservative `strategy`. |
| `reflector` | run all for side effects; merge unique lessons. |
| `optimizer` | split the generation budget across optimizers; keep the global best by fitness. |
| `curator` | ordered normalisers — each receives the prior output. |

A component owned by a {doc}`disabled package <enable-disable>` is dropped
from the stack. A role with no composite class rejects the stack and uses its
default.
