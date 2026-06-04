"""Image metadata loader."""

from __future__ import annotations

import json

from arc.assets.files import FileAsset
from arc.assets.loaders.base import BaseAssetLoader, LoaderContext


class ImageLoader(BaseAssetLoader):
    name = "image_loader"
    supported_media_types = ("image/png", "image/jpeg", "image/webp")
    supported_extensions = (".png", ".jpg", ".jpeg", ".webp")
    supported_roles = ("image", "figure", "screenshot", None)

    def load(self, asset: FileAsset, context: LoaderContext) -> list[FileAsset]:
        metadata = {
            "name": asset.name,
            "media_type": asset.media_type,
            "size_bytes": asset.size_bytes,
            "warnings": [],
        }
        try:
            from PIL import Image  # type: ignore

            with Image.open(context.file_store.path(asset.id)) as image:
                metadata.update({
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "format": image.format,
                })
        except Exception as exc:  # noqa: BLE001
            metadata["warnings"].append(f"Image metadata extraction failed: {exc}")

        derived = context.file_store.create_derived(
            asset,
            name=f"{asset.name}.metadata.json",
            media_type="application/json",
            role="image_metadata",
            loader=self.name,
            content=json.dumps(metadata, indent=2, sort_keys=True),
            metadata={"source_media_type": asset.media_type},
        )
        return [derived]

