import json

from arc.assets import FileStore
from arc.assets.loaders import (
    DEFAULT_LOADERS,
    CsvLoader,
    ImageLoader,
    JsonLoader,
    LoaderContext,
    PdfLoader,
    TextLoader,
)


def _context(tmp_path):
    return LoaderContext(
        file_store=FileStore(tmp_path / "store"),
        workspace=tmp_path,
        session_id="s1",
    )


def test_default_loader_matching(tmp_path):
    ctx = _context(tmp_path)
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    asset = ctx.file_store.import_file(source, role="notes", session_id="s1")

    matches = [loader.name for loader in DEFAULT_LOADERS if loader.can_load(asset)]

    assert matches == ["text_loader"]


def test_text_loader_creates_normalized_text(tmp_path):
    ctx = _context(tmp_path)
    source = tmp_path / "notes.txt"
    source.write_text("a\r\nb\r", encoding="utf-8")
    asset = ctx.file_store.import_file(source, role="notes", session_id="s1")

    [derived] = TextLoader().load(asset, ctx)

    assert derived.role == "normalized_text"
    assert derived.derived_from == asset.id
    assert ctx.file_store.read_text(derived.id) == "a\nb\n"


def test_pdf_loader_gracefully_creates_extracted_text_asset(tmp_path):
    ctx = _context(tmp_path)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF pretend")
    asset = ctx.file_store.import_file(source, role="paper", session_id="s1")

    [derived] = PdfLoader().load(asset, ctx)

    assert derived.role == "extracted_text"
    assert derived.media_type == "text/markdown"
    assert derived.derived_from == asset.id
    assert "paper.pdf" in ctx.file_store.read_text(derived.id)


def test_csv_loader_creates_profile_and_preview(tmp_path):
    ctx = _context(tmp_path)
    source = tmp_path / "data.csv"
    source.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    asset = ctx.file_store.import_file(source, role="data", session_id="s1")

    profile, preview = CsvLoader().load(asset, ctx)

    assert profile.role == "profile"
    assert preview.role == "preview"
    profile_data = json.loads(ctx.file_store.read_text(profile.id))
    assert profile_data["columns"] == ["x", "y"]
    assert profile_data["row_count"] == 2
    assert "| x | y |" in ctx.file_store.read_text(preview.id)


def test_json_loader_creates_summary(tmp_path):
    ctx = _context(tmp_path)
    source = tmp_path / "data.json"
    source.write_text('{"b": 2, "a": 1}', encoding="utf-8")
    asset = ctx.file_store.import_file(source, role="data", session_id="s1")

    [summary] = JsonLoader().load(asset, ctx)

    data = json.loads(ctx.file_store.read_text(summary.id))
    assert data["type"] == "dict"
    assert data["keys"] == ["a", "b"]


def test_image_loader_creates_metadata_even_without_optional_dependencies(tmp_path):
    ctx = _context(tmp_path)
    source = tmp_path / "figure.png"
    source.write_bytes(b"not a real png")
    asset = ctx.file_store.import_file(source, role="image", session_id="s1")

    [metadata] = ImageLoader().load(asset, ctx)

    assert metadata.role == "image_metadata"
    data = json.loads(ctx.file_store.read_text(metadata.id))
    assert data["name"] == "figure.png"
    assert data["media_type"] == "image/png"

