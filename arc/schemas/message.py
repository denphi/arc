from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class AgentMessage(BaseModel):
    source: str
    destination: str
    message_type: str  # input | output | error | feedback
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str
    iteration: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
