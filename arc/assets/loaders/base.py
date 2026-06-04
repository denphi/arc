"""Asset loader contract and common matching helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from arc.assets.files import FileAsset
from arc.assets.store import FileStore


@dataclass(frozen=True)
class LoaderContext:
    file_store: FileStore
    workspace: Path
    session_id: str
    run_id: str | None = None
    package_name: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


class AssetLoader(Protocol):
    name: str
    supported_media_types: tuple[str, ...]
    supported_extensions: tuple[str, ...]
    supported_roles: tuple[str, ...]

    def can_load(self, asset: FileAsset) -> bool: ...

    def load(self, asset: FileAsset, context: LoaderContext) -> list[FileAsset]: ...


class BaseAssetLoader:
    name = "base_loader"
    supported_media_types: tuple[str, ...] = ()
    supported_extensions: tuple[str, ...] = ()
    supported_roles: tuple[str, ...] = ()

    def can_load(self, asset: FileAsset) -> bool:
        suffix = Path(asset.name).suffix.lower()
        media_ok = (
            not self.supported_media_types
            or asset.media_type in self.supported_media_types
        )
        ext_ok = not self.supported_extensions or suffix in self.supported_extensions
        role_ok = not self.supported_roles or asset.role in self.supported_roles
        return (media_ok or ext_ok) and role_ok

