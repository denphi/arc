from abc import ABC, abstractmethod

from pydantic import BaseModel


class ProviderContract(ABC):
    name: str

    @abstractmethod
    async def complete(self, prompt: str, **kwargs) -> str:
        raise NotImplementedError

    @abstractmethod
    async def complete_structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        **kwargs,
    ) -> BaseModel:
        raise NotImplementedError
