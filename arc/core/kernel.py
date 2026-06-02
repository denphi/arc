import logging

from arc.core.config import (
    filter_package_paths,
    load_arc_toml,
    resolve_package_paths,
)
from arc.core.events import EventBus
from arc.core.loader import load_packages
from arc.core.registry import ComponentRegistry
from arc.core.session import SessionManager

logger = logging.getLogger(__name__)


class Kernel:
    """Central ARC kernel. Owns the registry, event bus, and session manager."""

    def __init__(self, config_path: str = "arc.toml"):
        self.config_path, self.config = load_arc_toml(config_path)
        if not self.config_path.exists():
            logger.warning("arc.toml not found — using defaults")
        self.registry = ComponentRegistry()
        self.events = EventBus()
        self.sessions = SessionManager()
        self._extensions: list = []

    async def startup(self) -> None:
        from arc.core.env import load_env
        load_env()  # populate os.environ from .env before packages load
        package_config = self.config.get("packages", {})
        resolved_paths = resolve_package_paths(self.config, self.config_path)
        load_packages(filter_package_paths(resolved_paths, package_config), self.registry)
        await self._load_extensions()
        logger.info("ARC kernel started. Agents: %s", self.registry.list_agents())

    async def _load_extensions(self) -> None:
        configured = self.config.get("extensions", {}) or {}
        names = set(configured)
        names.update(self.registry.list_extension_definitions())
        for ext_name in sorted(names):
            ext_cfg = dict(configured.get(ext_name, {}) or {})
            definition = self.registry.get_extension_definition(ext_name) or {}
            for key in ("entrypoint", "path", "class", "function", "_package_dir"):
                if key not in ext_cfg and definition.get(key):
                    ext_cfg[key] = definition[key]
            if not ext_cfg.get("enabled", definition.get("enabled", False)):
                continue
            entrypoint = ext_cfg.get("entrypoint")
            if not entrypoint and not ext_cfg.get("path"):
                continue
            try:
                import inspect
                from pathlib import Path

                from arc.core.loader import _import_class, _import_declared
                # Use the package loader's importers so package-hosted
                # extensions work with either a dotted entrypoint (including
                # hyphenated bundled packages) or local ``path`` + ``class``.
                if entrypoint:
                    cls = _import_class(entrypoint)
                else:
                    cls = _import_declared(ext_cfg, Path(ext_cfg["_package_dir"]))
                ext = cls()
                # Pass the registry when the implementation accepts it, so
                # the extension can register skills/adapters. Hand it a
                # package-scoped proxy so anything the extension registers
                # inherits the owning package as its source — keeping
                # /package disable effective for extension-created components
                # (review finding P2-2). Older extensions with a one-arg
                # initialize keep working.
                pkg_name = ext_cfg.get("_package_name") or definition.get("_package_name")
                scoped_registry = self.registry.scoped(pkg_name)
                params = inspect.signature(ext.initialize).parameters
                if len(params) >= 2:
                    await ext.initialize(ext_cfg, scoped_registry)
                else:
                    await ext.initialize(ext_cfg)
                self.registry.register_extension(ext_name, ext)
                self._extensions.append(ext)
            except Exception as exc:
                logger.error("Failed to load extension '%s': %s", ext_name, exc)

    async def shutdown(self) -> None:
        for ext in self._extensions:
            try:
                await ext.shutdown()
            except Exception as exc:
                logger.error("Error shutting down extension: %s", exc)
        logger.info("ARC kernel stopped.")
