"""Content-addressed file store for ARC FileAsset objects."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from arc.assets.files import FileAsset


class FileStore:
    """Manage original and derived files under ARC-controlled storage."""

    def __init__(
        self,
        root: str | Path = "workspace/files",
        *,
        allowed_roots: Iterable[str | Path] | None = None,
        max_file_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        self.root = Path(root).resolve()
        self.blob_root = self.root / "sha256"
        self.index_path = self.root / "assets.json"
        self.max_file_bytes = max_file_bytes
        self.allowed_roots = tuple(Path(p).resolve() for p in (allowed_roots or ()))
        self.root.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)

    def import_file(
        self,
        path: str | Path,
        *,
        role: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        copy: bool = True,
    ) -> FileAsset:
        if not copy:
            return self.index_file(
                path,
                role=role,
                session_id=session_id,
                run_id=run_id,
                metadata=metadata,
            )
        source = self._resolve_source(path)
        stat = source.stat()
        if not source.is_file():
            raise ValueError(f"File asset source is not a file: {source}")
        if stat.st_size > self.max_file_bytes:
            raise ValueError(
                f"File too large: {stat.st_size} bytes exceeds {self.max_file_bytes}"
            )

        sha = self._hash_file(source)
        media_type = self._guess_media_type(source)
        stored_path = self._store_blob(source, sha)
        asset = FileAsset(
            id=self._asset_id(sha, role=role, name=source.name),
            name=source.name,
            media_type=media_type,
            size_bytes=stat.st_size,
            sha256=sha,
            stored_path=str(stored_path),
            source_path=str(source),
            role=role,
            session_id=session_id,
            run_id=run_id,
            created_at=self._now(),
            metadata=metadata or {},
        )
        self._upsert(asset)
        return asset

    def index_file(
        self,
        path: str | Path,
        *,
        role: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> FileAsset:
        """Register a file *without reading its contents*.

        Records stat + extension-sniffed media type and points ``stored_path``
        at the original. **No hashing and no blob copy** happen here — that is
        deferred to first access (``path``/``read_bytes``/``open``/loader run),
        when :meth:`_materialize` copies bytes into the content-addressed store
        and fills in the real ``sha256``. This is the lightweight ``index``
        import mode used by the startup scan, so a large input library isn't
        hashed up front (todo.md Phase 4 — "scan does not read full file
        contents").
        """
        source = self._resolve_source(path)
        if not source.is_file():
            raise ValueError(f"File asset source is not a file: {source}")
        stat = source.stat()
        if stat.st_size > self.max_file_bytes:
            raise ValueError(
                f"File too large: {stat.st_size} bytes exceeds {self.max_file_bytes}"
            )
        media_type = self._guess_media_type(source)
        # A stable, content-independent id for the unmaterialised asset
        # (path + size + mtime). Materialisation keeps this id so references
        # remain valid while the asset gains a content hash and managed path.
        token = hashlib.sha256(
            f"{source}:{stat.st_size}:{stat.st_mtime_ns}:{role or ''}".encode("utf-8")
        ).hexdigest()[:12]
        asset = FileAsset(
            id=f"file_{token}",
            name=source.name,
            media_type=media_type,
            size_bytes=stat.st_size,
            sha256="",                       # unknown until materialised
            stored_path=str(source),         # the original, until materialised
            source_path=str(source),
            role=role,
            session_id=session_id,
            run_id=run_id,
            created_at=self._now(),
            metadata={
                **(metadata or {}),
                "indexed": True,
                "indexed_size_bytes": stat.st_size,
                "indexed_mtime_ns": stat.st_mtime_ns,
            },
        )
        self._upsert(asset)
        return asset

    def _materialize(self, asset: FileAsset) -> FileAsset:
        """Copy an indexed asset's bytes into managed storage on first use.

        Indexed assets (from :meth:`index_file`) have no hash and point at the
        user's original path. The first access hashes + copies the bytes into
        the content-addressed store and persists the updated record. A no-op
        for already-materialised assets.
        """
        if not asset.metadata.get("indexed"):
            return asset
        source = self._resolve_source(asset.stored_path)
        if not source.is_file():
            raise FileNotFoundError(
                f"Indexed file no longer available at its source path: {source}"
            )
        stat = source.stat()
        if stat.st_size > self.max_file_bytes:
            raise ValueError(
                f"File too large: {stat.st_size} bytes exceeds {self.max_file_bytes}"
            )
        indexed_size = asset.metadata.get("indexed_size_bytes")
        indexed_mtime = asset.metadata.get("indexed_mtime_ns")
        if indexed_size is not None and indexed_size != stat.st_size:
            raise ValueError(
                f"Indexed file changed size before materialization: {source}"
            )
        if indexed_mtime is not None and indexed_mtime != stat.st_mtime_ns:
            raise ValueError(
                f"Indexed file changed timestamp before materialization: {source}"
            )
        sha = self._hash_file(source)
        stored_path = self._store_blob(source, sha)
        new_meta = {
            k: v for k, v in asset.metadata.items()
            if k not in {"indexed", "indexed_size_bytes", "indexed_mtime_ns"}
        }
        materialised = replace(
            asset, sha256=sha, stored_path=str(stored_path), metadata=new_meta,
        )
        self._upsert(materialised)
        return materialised

    def register_external(
        self,
        path: str | Path,
        *,
        role: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> FileAsset:
        return self.index_file(
            path,
            role=role,
            session_id=session_id,
            metadata=metadata,
        )

    def create_derived(
        self,
        source: FileAsset | str,
        *,
        name: str,
        media_type: str,
        role: str,
        loader: str,
        content: bytes | str,
        metadata: dict[str, Any] | None = None,
    ) -> FileAsset:
        source_asset = self.get(source) if isinstance(source, str) else source
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        sha = hashlib.sha256(data).hexdigest()
        stored_path = self._store_bytes(data, sha)
        asset = FileAsset(
            id=self._asset_id(sha, role=role, name=name, derived_from=source_asset.id),
            name=name,
            media_type=media_type,
            size_bytes=len(data),
            sha256=sha,
            stored_path=str(stored_path),
            source_path=None,
            role=role,
            session_id=source_asset.session_id,
            run_id=source_asset.run_id,
            derived_from=source_asset.id,
            loader=loader,
            created_at=self._now(),
            metadata=metadata or {},
        )
        self._upsert(asset)
        return asset

    def get(self, file_id: str) -> FileAsset:
        assets = self._load_index()
        if file_id not in assets:
            raise KeyError(f"File asset not found: {file_id}")
        return FileAsset.from_dict(assets[file_id])

    def list(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        role: str | None = None,
        derived_from: str | None = None,
    ) -> list[FileAsset]:
        out = []
        for data in self._load_index().values():
            asset = FileAsset.from_dict(data)
            if session_id is not None and asset.session_id != session_id:
                continue
            if run_id is not None and asset.run_id != run_id:
                continue
            if role is not None and asset.role != role:
                continue
            if derived_from is not None and asset.derived_from != derived_from:
                continue
            out.append(asset)
        return out

    def path(self, file_id: str) -> Path:
        asset = self.get(file_id)
        # An indexed asset's bytes haven't been copied into managed storage
        # yet — materialise on first access so callers always get a managed,
        # in-root path (and the content-addressed invariant holds).
        if asset.metadata.get("indexed"):
            asset = self._materialize(asset)
        path = Path(asset.stored_path).resolve()
        if not self._is_relative_to(path, self.root):
            raise ValueError(f"Stored file path escapes FileStore root: {path}")
        return path

    def open(self, file_id: str, mode: str = "rb"):
        if "w" in mode or "a" in mode or "+" in mode:
            raise ValueError("FileStore assets are read-only")
        return self.path(file_id).open(mode)

    def read_text(self, file_id: str, max_bytes: int = 10_000_000) -> str:
        data = self.read_bytes(file_id, max_bytes=max_bytes)
        return data.decode("utf-8")

    def read_bytes(self, file_id: str, max_bytes: int = 10_000_000) -> bytes:
        path = self.path(file_id)
        size = path.stat().st_size
        if size > max_bytes:
            raise ValueError(f"File too large to read: {size} bytes exceeds {max_bytes}")
        return path.read_bytes()

    def update_metadata(self, file_id: str, metadata: dict[str, Any]) -> FileAsset:
        """Merge metadata into an existing asset and persist the index."""
        asset = self.get(file_id)
        updated = replace(asset, metadata={**asset.metadata, **metadata})
        self._upsert(updated)
        return updated

    def _resolve_source(self, path: str | Path) -> Path:
        source = Path(path).expanduser().resolve()
        if self.allowed_roots and not any(
            self._is_relative_to(source, root) for root in self.allowed_roots
        ):
            raise ValueError(f"File path is outside allowed roots: {source}")
        return source

    def _store_blob(self, source: Path, sha: str) -> Path:
        target = self.blob_root / sha / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        return target.resolve()

    def _store_bytes(self, data: bytes, sha: str) -> Path:
        target = self.blob_root / sha / "derived"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return target.resolve()

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if not self.index_path.exists():
            return {}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, assets: dict[str, dict[str, Any]]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(assets, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.index_path)

    def _upsert(self, asset: FileAsset) -> None:
        assets = self._load_index()
        assets[asset.id] = asset.to_dict()
        self._save_index(assets)

    def _hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _asset_id(
        self,
        sha: str,
        *,
        role: str | None,
        name: str,
        derived_from: str | None = None,
    ) -> str:
        token = hashlib.sha256(
            f"{sha}:{role or ''}:{name}:{derived_from or ''}".encode("utf-8")
        ).hexdigest()[:12]
        return f"file_{token}"

    def _guess_media_type(self, path: Path) -> str:
        guessed, _encoding = mimetypes.guess_type(str(path))
        return guessed or "application/octet-stream"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _is_relative_to(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
