from abc import ABC, abstractmethod
from typing import Any


class ExtensionContract(ABC):
    """An optional integration loaded at startup when enabled in arc.toml.

    Extensions are the seam for capabilities that aren't core: MCP tools,
    OpenAPI endpoints, a vector store, etc. Core defines this contract; the
    *implementations* live in packages. An extension contributes by calling
    the registry inside ``initialize`` (e.g. ``registry.register_skill``).
    """

    name: str

    @abstractmethod
    async def initialize(self, config: dict, registry: Any = None) -> None:
        """Set up the extension.

        ``config`` is the extension's ``[extensions.<name>]`` block from
        arc.toml. ``registry`` is the kernel's ``ComponentRegistry`` so the
        extension can register skills/adapters/etc.; it is optional for
        backward compatibility — the kernel passes it when the
        implementation accepts it.
        """
        raise NotImplementedError

    @abstractmethod
    async def shutdown(self) -> None:
        raise NotImplementedError
