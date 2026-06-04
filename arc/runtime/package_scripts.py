"""Controlled execution for package-declared scripts."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from arc.assets.files import FileAsset
from arc.assets.store import FileStore
from arc.core.registry import ComponentRegistry


@dataclass(frozen=True)
class ScriptResult:
    name: str
    returncode: int
    stdout: str
    stderr: str
    generated_assets: list[FileAsset] = field(default_factory=list)


class PackageScriptRunner:
    """Run scripts that a package explicitly declares in `package.yaml`."""

    def __init__(
        self,
        registry: ComponentRegistry,
        file_store: FileStore,
        *,
        session_id: str,
    ) -> None:
        self.registry = registry
        self.file_store = file_store
        self.session_id = session_id

    def run(
        self,
        script_name: str,
        *,
        args: list[str] | None = None,
        cwd: str | Path,
        timeout_s: int = 60,
        disabled_packages: set[str] | None = None,
        import_outputs: bool = True,
        source_asset_id: str | None = None,
    ) -> ScriptResult:
        definition = self.registry.get_script(
            script_name,
            disabled_packages=disabled_packages,
        )
        runtime = definition.get("runtime", "python")
        if runtime != "python":
            raise ValueError(f"Unsupported script runtime: {runtime}")

        workspace = Path(cwd).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        before = self._workspace_files(workspace)
        command = [sys.executable, str(definition["path"]), *(args or [])]
        completed = subprocess.run(
            command,
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        generated_assets: list[FileAsset] = []
        if import_outputs and completed.returncode == 0:
            for path in sorted(self._workspace_files(workspace) - before):
                generated_assets.append(self._import_generated(path, definition, source_asset_id))
        return ScriptResult(
            name=script_name,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            generated_assets=generated_assets,
        )

    def _workspace_files(self, workspace: Path) -> set[Path]:
        return {path.resolve() for path in workspace.rglob("*") if path.is_file()}

    def _import_generated(
        self,
        path: Path,
        definition: dict,
        source_asset_id: str | None,
    ) -> FileAsset:
        metadata = {
            "source": "package_script",
            "script": definition["name"],
        }
        package_name = self.registry.component_source("script", definition["name"])
        if package_name:
            metadata["package_name"] = package_name
        asset = self.file_store.import_file(
            path,
            role="script_output",
            session_id=self.session_id,
            metadata=metadata,
            copy=True,
        )
        if source_asset_id:
            source = self.file_store.get(source_asset_id)
            asset = self.file_store.create_derived(
                source,
                name=path.name,
                media_type=asset.media_type,
                role="script_output",
                loader=definition["name"],
                content=self.file_store.read_bytes(asset.id),
                metadata=metadata,
            )
        return asset
