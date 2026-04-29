from abc import ABC, abstractmethod
from typing import Any

from arc.contracts.agent import AgentContext


class SkillContract(ABC):
    name: str
    description: str

    @abstractmethod
    async def execute(self, inputs: dict[str, Any], context: AgentContext) -> dict[str, Any]:
        raise NotImplementedError
