from typing import Any

from pydantic import BaseModel, Field


class ResearchGoal(BaseModel):
    goal: str
    domain: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)  # e.g. {"bandgap_eV": 1.1}
    mode: str = "single"  # single | continuous | interactive


class ResearchProposal(BaseModel):
    hypothesis: str
    objective: str
    variables: list[str]
    methodology: str
    expected_outcomes: str
    evaluation_metrics: list[str]
    risk_level: str = "medium"


class ExperimentPlan(BaseModel):
    proposal: ResearchProposal
    artifact_strategy: str
    parameters: dict[str, Any]
    parameter_sweep: dict[str, list[Any]] = Field(default_factory=dict)
    parameter_constraints: dict[str, dict[str, Any]] = Field(default_factory=dict)
    experimental_design: list[str] = Field(default_factory=list)
    success_criteria: list[str]


class SearchResult(BaseModel):
    """Catalog + prior-results lookups returned by a Searcher strategy.

    ``catalog_hits`` is the list of simulation records (id, name,
    description, input_schema, output_schema, …) ranked best-first.
    ``prior_results`` is recent results for the top catalog hit, used by
    the ideator's prompt and by the chat's "reuse this artifact?" prompt.
    """
    catalog_hits: list[dict[str, Any]] = Field(default_factory=list)
    prior_results: list[dict[str, Any]] = Field(default_factory=list)


class ValidatorReport(BaseModel):
    """Post-execution validation result from a Validator strategy.

    ``passed`` is the conjunction of every evaluator's pass flag.
    ``errors`` and ``warnings`` are user-facing strings the chat prints.
    ``evaluations`` carries per-evaluator details ({evaluator_name → dict
    returned by that evaluator}) so downstream consumers (the chat UI,
    the reviewer agent) can show or reason about individual checks.

    Distinct from :class:`arc.schemas.artifact.ValidationResult`, which
    is *pre*-execution (schema / import / AST safety). This one runs
    *after* the artifact returns outputs.
    """
    passed: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evaluations: dict[str, dict[str, Any]] = Field(default_factory=dict)
