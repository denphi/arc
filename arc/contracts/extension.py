from abc import ABC, abstractmethod


class ExtensionContract(ABC):
    name: str

    @abstractmethod
    async def initialize(self, config: dict) -> None:
        raise NotImplementedError

    @abstractmethod
    async def shutdown(self) -> None:
        raise NotImplementedError
