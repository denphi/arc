"""File asset model for ARC-managed local inputs and derived outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class FileAsset:
    """One original or derived file known to ARC."""

    id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    stored_path: str
    source_path: str | None = None
    role: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    derived_from: str | None = None
    loader: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileAsset":
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})
