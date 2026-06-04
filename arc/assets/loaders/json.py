"""JSON preview loader."""

from __future__ import annotations

import json
from typing import Any

from arc.assets.files import FileAsset
from arc.assets.loaders.base import BaseAssetLoader, LoaderContext


class JsonLoader(BaseAssetLoader):
    name = "json_loader"
    supported_media_types = ("application/json", "application/x-ndjson")
    supported_extensions = (".json", ".jsonl")
    supported_roles = ("data", "metadata", None)

    def load(self, asset: FileAsset, context: LoaderContext) -> list[FileAsset]:
        text = context.file_store.read_text(asset.id)
        summary: dict[str, Any]
        try:
            if asset.name.endswith(".jsonl"):
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
                summary = {
                    "type": "jsonl",
                    "row_count": len(rows),
                    "preview": rows[:5],
                }
            else:
                data = json.loads(text)
                summary = {
                    "type": type(data).__name__,
                    "keys": sorted(data.keys()) if isinstance(data, dict) else None,
                    "length": len(data) if hasattr(data, "__len__") else None,
                    "preview": data,
                }
        except Exception as exc:  # noqa: BLE001
            summary = {"error": str(exc), "preview": text[:500]}

        derived = context.file_store.create_derived(
            asset,
            name=f"{asset.name}.summary.json",
            media_type="application/json",
            role="summary",
            loader=self.name,
            content=json.dumps(summary, indent=2, sort_keys=True, default=str),
            metadata={"source_media_type": asset.media_type},
        )
        return [derived]

