"""Default ARC asset loaders."""

from arc.assets.loaders.base import AssetLoader, LoaderContext
from arc.assets.loaders.csv import CsvLoader
from arc.assets.loaders.image import ImageLoader
from arc.assets.loaders.json import JsonLoader
from arc.assets.loaders.pdf import PdfLoader
from arc.assets.loaders.text import TextLoader

DEFAULT_LOADERS = (
    TextLoader(),
    PdfLoader(),
    ImageLoader(),
    CsvLoader(),
    JsonLoader(),
)

__all__ = [
    "AssetLoader",
    "LoaderContext",
    "TextLoader",
    "PdfLoader",
    "ImageLoader",
    "CsvLoader",
    "JsonLoader",
    "DEFAULT_LOADERS",
]

