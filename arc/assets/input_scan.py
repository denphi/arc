"""Session startup scan for ARC input files."""

from __future__ import annotations

import os
import logging
from pathlib import Path

from arc.assets.files import FileAsset
from arc.assets.store import FileStore


DEFAULT_INPUTS_DIR = "./data"
logger = logging.getLogger(__name__)


def scan_inputs_from_env(file_store: FileStore, *, session_id: str) -> list[FileAsset]:
    """Scan session inputs and register discovered files.

    ``ARC_INPUTS_DIR`` overrides the default ``./data`` folder. This function
    is intentionally conservative: it never reads file contents into prompt
    context. By default it uses ``index`` mode, registering metadata only and
    deferring hashing/copying into managed storage until first access. Set
    ``ARC_INPUTS_IMPORT_MODE=copy`` to materialise discovered files at startup.
    """
    env_root = os.environ.get("ARC_INPUTS_DIR")
    root_value = env_root or DEFAULT_INPUTS_DIR
    source_label = "ARC_INPUTS_DIR" if env_root else DEFAULT_INPUTS_DIR
    root = Path(root_value).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return []

    recursive = os.environ.get("ARC_INPUTS_RECURSIVE", "1").lower() not in {
        "0", "false", "no",
    }
    mode = os.environ.get("ARC_INPUTS_IMPORT_MODE", "index").lower()
    copy = mode == "copy"
    max_mb = int(os.environ.get("ARC_INPUTS_MAX_FILE_MB", "200") or 200)
    max_bytes = max_mb * 1024 * 1024

    pattern = "**/*" if recursive else "*"
    assets: list[FileAsset] = []
    for path in sorted(root.glob(pattern)):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            if path.stat().st_size > max_bytes:
                logger.debug("Skipping input file above size limit: %s", path)
                continue
            importer = file_store.import_file if copy else file_store.index_file
            assets.append(
                importer(
                    path,
                    role=_infer_role(path),
                    session_id=session_id,
                    metadata={"source": source_label},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping input file %s: %s", path, exc)
            continue
    return assets


def _infer_role(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "paper"
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return "image"
    if suffix in {".csv", ".tsv"}:
        return "data"
    if suffix in {".txt", ".md", ".rst"}:
        return "text"
    return "file"
