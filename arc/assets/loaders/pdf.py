"""PDF asset loader with optional text extraction."""

from __future__ import annotations

from arc.assets.files import FileAsset
from arc.assets.loaders.base import BaseAssetLoader, LoaderContext


class PdfLoader(BaseAssetLoader):
    name = "pdf_loader"
    supported_media_types = ("application/pdf",)
    supported_extensions = (".pdf",)
    supported_roles = ("paper", "document", "manual", None)

    def load(self, asset: FileAsset, context: LoaderContext) -> list[FileAsset]:
        text = ""
        warnings: list[str] = []
        pages = None
        path = context.file_store.path(asset.id)
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(path))
            pages = len(reader.pages)
            chunks = []
            for index, page in enumerate(reader.pages, start=1):
                extracted = page.extract_text() or ""
                chunks.append(f"\n\n<!-- page {index} -->\n\n{extracted}")
            text = "".join(chunks).strip()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"PDF text extraction unavailable or failed: {exc}")

        if not text:
            text = (
                f"# Extracted text for {asset.name}\n\n"
                "PDF text extraction did not produce text. Use a richer PDF "
                "loader or page-image/OCR workflow if the paper is scanned.\n"
            )

        derived = context.file_store.create_derived(
            asset,
            name=f"{asset.name}.extracted.md",
            media_type="text/markdown",
            role="extracted_text",
            loader=self.name,
            content=text,
            metadata={
                "pages": pages,
                "warnings": warnings,
                "source_media_type": asset.media_type,
            },
        )
        return [derived]

