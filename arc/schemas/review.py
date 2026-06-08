from typing import Any, Literal

from pydantic import BaseModel, Field


class ReviewResult(BaseModel):
    approved: bool
    summary: str
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    next_parameters: dict[str, Any] = Field(default_factory=dict)  # suggested inputs for next run
    iteration_complete: bool = False
    # "step"   → use next_parameters for one more run
    # "explore" → run genetic algorithm to search more broadly
    # "stop"   → no further progress possible
    strategy: Literal["step", "explore", "stop"] = "step"
    failure_classification: Literal[
        "context_failure",
        "implementation_failure",
        "runtime_failure",
        "acceptance_failure",
        "none",
    ] = "none"
