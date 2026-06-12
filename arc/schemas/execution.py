from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExecutionRequest(BaseModel):
    artifact_id: str
    version: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    run_id: str
    status: str
    # The reconciled inputs the run actually used. First-class (not buried
    # in metrics) so stored results are indexed by their inputs and sweep
    # points carry their own parameter combination through bookkeeping.
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
