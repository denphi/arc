"""slurm-runtime extension shim — registers the Slurm adapter."""

from __future__ import annotations

from typing import Any

from arc.contracts.extension import ExtensionContract


class SlurmRuntimeExtension(ExtensionContract):
    name = "slurm-runtime"

    async def initialize(self, config: dict, registry: Any = None) -> None:
        if registry is None:
            return
        from arc.core.loader import _import_class
        adapter_cls = _import_class("arc.packages.arc-slurm.adapter:SlurmRuntimeAdapter")
        registry.register_adapter("slurm", adapter_cls)

    async def shutdown(self) -> None:
        pass
