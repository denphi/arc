"""docker-runtime extension shim — registers the Docker adapter.

A workflow YAML step can name ``adapter: docker`` once this is loaded
(the YAML engine resolves adapters via ``registry.get_adapter``). The
adapter is also reachable via ``ARC_RUNTIME_ADAPTER=docker`` through the
core ``_build_adapter`` dispatch. This shim just registers the class.
"""

from __future__ import annotations

from typing import Any

from arc.contracts.extension import ExtensionContract


class DockerRuntimeExtension(ExtensionContract):
    name = "docker-runtime"

    async def initialize(self, config: dict, registry: Any = None) -> None:
        if registry is None:
            return
        # The package dir is hyphenated (arc-docker), so import the adapter
        # through the loader's hyphen-aware importer rather than a plain
        # ``import``.
        from arc.core.loader import _import_class
        adapter_cls = _import_class("arc.packages.arc-docker.adapter:DockerRuntimeAdapter")
        registry.register_adapter("docker", adapter_cls)

    async def shutdown(self) -> None:
        pass
