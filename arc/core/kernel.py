import logging
from pathlib import Path

from arc.core.events import EventBus
from arc.core.loader import load_packages
from arc.core.registry import ComponentRegistry
from arc.core.session import SessionManager

logger = logging.getLogger(__name__)


class Kernel:
    """Central ARC kernel. Owns the registry, event bus, and session manager."""

    def __init__(self, config_path: str = "arc.toml"):
        self.config_path = self._resolve_config_path(config_path)
        self.config = self._load_config(self.config_path)
        self.registry = ComponentRegistry()
        self.events = EventBus()
        self.sessions = SessionManager()
        self._extensions: list = []

    def _resolve_config_path(self, path: str) -> Path:
        config_file = Path(path)
        if config_file.exists():
            return config_file
        for bundled in (
            Path(__file__).resolve().parents[2] / "arc.toml",
            Path(__file__).resolve().parents[1] / "arc.toml",
        ):
            if bundled.exists():
                return bundled
        return config_file

    def _load_config(self, path: Path) -> dict:
        config_file = path
        if not config_file.exists():
            logger.warning("arc.toml not found — using defaults")
            return {}
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-reattr]
        with config_file.open("rb") as f:
            return tomllib.load(f)

    async def startup(self) -> None:
        package_paths = self.config.get("packages", {}).get("paths", [])
        base = self.config_path.parent if self.config_path.exists() else Path.cwd()
        resolved_paths = [
            str((base / path).resolve()) if not Path(path).is_absolute() else path
            for path in package_paths
        ]
        load_packages(resolved_paths, self.registry)
        await self._load_extensions()
        logger.info("ARC kernel started. Agents: %s", self.registry.list_agents())

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
