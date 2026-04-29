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
    success_criteria: list[str]
