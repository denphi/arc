from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass
class AgentContext:
    session_id: str
    iteration: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)

    @property
    def files(self):
        """Session FileStore, when file assets are enabled."""
        return self.memory.get("file_store") or self.memory.get("files")


class AgentContract(ABC):
    name: str
    description: str

    def __init__(self, context: AgentContext | None = None):
        self.context = context or AgentContext(session_id="default")

    @abstractmethod
    async def run(self, input_data: Any) -> Any:
        raise NotImplementedError

    def input_schema(self) -> type[BaseModel] | None:
        return None

    def output_schema(self) -> type[BaseModel] | None:
        return None
