"""Text asset loader."""

from __future__ import annotations

from arc.assets.files import FileAsset
from arc.assets.loaders.base import BaseAssetLoader, LoaderContext


class TextLoader(BaseAssetLoader):
    name = "text_loader"
    supported_media_types = ("text/plain", "text/markdown")
    supported_extensions = (".txt", ".md", ".rst")
    supported_roles = ("text", "document", "prompt", "notes", None)

    def load(self, asset: FileAsset, context: LoaderContext) -> list[FileAsset]:
        text = context.file_store.read_text(asset.id)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        derived = context.file_store.create_derived(
            asset,
            name=f"{asset.name}.normalized.txt",
            media_type="text/plain",
            role="normalized_text",
            loader=self.name,
            content=normalized,
            metadata={"source_media_type": asset.media_type},
        )
        return [derived]

