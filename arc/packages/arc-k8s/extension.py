"""k8s-runtime extension shim — registers the Kubernetes adapter."""

from __future__ import annotations

from typing import Any

from arc.contracts.extension import ExtensionContract


class KubernetesRuntimeExtension(ExtensionContract):
    name = "k8s-runtime"

    async def initialize(self, config: dict, registry: Any = None) -> None:
        if registry is None:
            return
        from arc.core.loader import _import_class
        adapter_cls = _import_class("arc.packages.arc-k8s.adapter:KubernetesRuntimeAdapter")
        registry.register_adapter("k8s", adapter_cls)

    async def shutdown(self) -> None:
        pass
