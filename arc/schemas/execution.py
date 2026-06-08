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
    outputs: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
