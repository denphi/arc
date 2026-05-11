from typing import Any

from pydantic import BaseModel, Field


class ArtifactDraft(BaseModel):
    name: str
    description: str
    files: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(BaseModel):
    artifact_id: str
    name: str
    description: str = ""
    version: str
    state: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    test_run_id: str | None = None
