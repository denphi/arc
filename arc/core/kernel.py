import logging
from pathlib import Path
from typing import Any

from arc.core.config import load_arc_toml, resolve_package_paths
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
        package_config = self.config.get("packages", {})
        resolved_paths = resolve_package_paths(self.config, self.config_path)
        load_packages(self._filter_package_paths(resolved_paths, package_config), self.registry)
        await self._load_extensions()
        logger.info("ARC kernel started. Agents: %s", self.registry.list_agents())

    def _filter_package_paths(self, package_paths: list[str], package_config: dict[str, Any]) -> list[str]:
        enabled = set(package_config.get("enabled", []) or [])
        disabled = set(package_config.get("disabled", []) or [])
        filtered = []
        for path_str in package_paths:
            package_name = self._package_name_for_path(Path(path_str))
            if enabled and package_name not in enabled:
                continue
            if package_name in disabled:
                continue
            filtered.append(path_str)
        return filtered

    def _package_name_for_path(self, package_dir: Path) -> str:
        manifest = package_dir / "package.yaml"
        if not manifest.exists():
            return package_dir.name
        try:
            import yaml
            data = yaml.safe_load(manifest.read_text()) or {}
            return data.get("name") or package_dir.name
        except Exception:
            return package_dir.name

    async def _load_extensions(self) -> None:
        ext_config = self.config.get("extensions", {})
        for ext_name, ext_cfg in ext_config.items():
            if not ext_cfg.get("enabled", False):
                continue
            entrypoint = ext_cfg.get("entrypoint")
            if not entrypoint:
                continue
            try:
                import importlib
                module_path, class_name = entrypoint.rsplit(":", 1)
                module = importlib.import_module(module_path)
                cls = getattr(module, class_name)
                ext = cls()
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
