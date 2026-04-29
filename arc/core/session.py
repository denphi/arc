import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ResearchSession:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = ""
    iteration: int = 0
    state: str = "created"
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def record(self, step: str, data: Any) -> None:
        self.history.append({"step": step, "iteration": self.iteration, "data": data})
        self.updated_at = datetime.now(timezone.utc)

    def advance(self) -> None:
        self.iteration += 1
        self.updated_at = datetime.now(timezone.utc)


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, ResearchSession] = {}

    def create(self, goal: str) -> ResearchSession:
        session = ResearchSession(goal=goal)
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> ResearchSession | None:
        return self._sessions.get(session_id)

    def all(self) -> list[ResearchSession]:
        return list(self._sessions.values())
