"""CSV preview/profile loader."""

from __future__ import annotations

import csv
import json

from arc.assets.files import FileAsset
from arc.assets.loaders.base import BaseAssetLoader, LoaderContext


class CsvLoader(BaseAssetLoader):
    name = "csv_loader"
    supported_media_types = ("text/csv",)
    supported_extensions = (".csv",)
    supported_roles = ("data", "table", None)

    def load(self, asset: FileAsset, context: LoaderContext) -> list[FileAsset]:
        text = context.file_store.read_text(asset.id)
        rows = list(csv.reader(text.splitlines()))
        header = rows[0] if rows else []
        data_rows = rows[1:]
        profile = {
            "columns": header,
            "row_count": len(data_rows),
            "preview": data_rows[:5],
        }
        schema = context.file_store.create_derived(
            asset,
            name=f"{asset.name}.profile.json",
            media_type="application/json",
            role="profile",
            loader=self.name,
            content=json.dumps(profile, indent=2, sort_keys=True),
            metadata={"source_media_type": asset.media_type},
        )
        preview_lines = ["| " + " | ".join(header) + " |"] if header else []
        if header:
            preview_lines.append("| " + " | ".join("---" for _ in header) + " |")
            for row in data_rows[:5]:
                preview_lines.append("| " + " | ".join(row) + " |")
        preview = context.file_store.create_derived(
            asset,
            name=f"{asset.name}.preview.md",
            media_type="text/markdown",
            role="preview",
            loader=self.name,
            content="\n".join(preview_lines) + ("\n" if preview_lines else ""),
            metadata={"source_media_type": asset.media_type},
        )
        return [schema, preview]

