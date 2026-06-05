import pytest

from arc.assets import FileAsset, FileStore


def test_file_store_imports_file_and_reads_text(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("hello arc", encoding="utf-8")
    store = FileStore(tmp_path / "store")

    asset = store.import_file(source, role="paper", session_id="s1")

    assert asset.id.startswith("file_")
    assert asset.name == "paper.txt"
    assert asset.media_type == "text/plain"
    assert asset.role == "paper"
    assert asset.session_id == "s1"
    assert store.get(asset.id) == asset
    assert store.read_text(asset.id) == "hello arc"


def test_file_store_reuses_content_storage_for_same_file(tmp_path):
    source = tmp_path / "data.csv"
    source.write_text("x,y\n1,2\n", encoding="utf-8")
    store = FileStore(tmp_path / "store")

    first = store.import_file(source, role="data")
    second = store.import_file(source, role="data")

    assert first.id == second.id
    assert first.sha256 == second.sha256
    assert first.stored_path == second.stored_path


def test_file_store_creates_derived_assets(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF pretend")
    store = FileStore(tmp_path / "store")
    original = store.import_file(source, role="paper", session_id="s1", run_id="r1")

    derived = store.create_derived(
        original,
        name="paper.extracted.md",
        media_type="text/markdown",
        role="extracted_text",
        loader="pdf_loader",
        content="# Extracted\n",
        metadata={"pages": 1},
    )

    assert derived.derived_from == original.id
    assert derived.loader == "pdf_loader"
    assert derived.session_id == "s1"
    assert derived.run_id == "r1"
    assert derived.metadata == {"pages": 1}
    assert store.read_text(derived.id) == "# Extracted\n"
    assert store.list(derived_from=original.id) == [derived]


def test_file_store_list_filters(tmp_path):
    store = FileStore(tmp_path / "store")
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")

    paper = store.import_file(a, role="paper", session_id="s1")
    store.import_file(b, role="data", session_id="s2")

    assert store.list(role="paper") == [paper]
    assert store.list(session_id="s1") == [paper]
    assert store.list(session_id="missing") == []


def test_file_store_read_bytes_enforces_size_cap(tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"12345")
    store = FileStore(tmp_path / "store")
    asset = store.import_file(source)

    with pytest.raises(ValueError, match="File too large to read"):
        store.read_bytes(asset.id, max_bytes=4)


def test_file_store_rejects_sources_outside_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    store = FileStore(tmp_path / "store", allowed_roots=[allowed])

    with pytest.raises(ValueError, match="outside allowed roots"):
        store.import_file(outside)


def test_file_store_assets_are_read_only(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("notes", encoding="utf-8")
    store = FileStore(tmp_path / "store")
    asset = store.import_file(source)

    with pytest.raises(ValueError, match="read-only"):
        store.open(asset.id, "wb")


def test_file_store_updates_metadata(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("notes", encoding="utf-8")
    store = FileStore(tmp_path / "store")
    asset = store.import_file(source, metadata={"a": 1})

    updated = store.update_metadata(asset.id, {"b": 2})

    assert updated.metadata == {"a": 1, "b": 2}
    assert store.get(asset.id).metadata == {"a": 1, "b": 2}


def test_file_asset_from_dict_ignores_unknown_fields():
    asset = FileAsset.from_dict({
        "id": "file_1",
        "name": "a.txt",
        "media_type": "text/plain",
        "size_bytes": 1,
        "sha256": "abc",
        "stored_path": "/tmp/a.txt",
        "future_field": "ignored",
    })

    assert asset.id == "file_1"
    assert not hasattr(asset, "future_field")


def test_file_store_indexes_without_hashing_or_copying(tmp_path, monkeypatch):
    source = tmp_path / "notes.txt"
    source.write_text("notes", encoding="utf-8")
    store = FileStore(tmp_path / "store")

    def _boom(path):
        raise AssertionError("index_file should not hash content")

    monkeypatch.setattr(store, "_hash_file", _boom)
    asset = store.index_file(source, role="text", session_id="s1")

    assert asset.sha256 == ""
    assert asset.stored_path == str(source.resolve())
    assert asset.metadata["indexed"] is True
    assert not any(store.blob_root.iterdir())


def test_file_store_materializes_indexed_asset_on_first_read(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("notes", encoding="utf-8")
    store = FileStore(tmp_path / "store")
    asset = store.index_file(source, role="text", session_id="s1")

    assert store.read_text(asset.id) == "notes"

    materialized = store.get(asset.id)
    assert materialized.sha256
    assert "indexed" not in materialized.metadata
    assert materialized.stored_path != str(source.resolve())
    assert store.path(asset.id).is_relative_to(store.root)


def test_file_store_rejects_changed_indexed_source_before_materialize(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("notes", encoding="utf-8")
    store = FileStore(tmp_path / "store")
    asset = store.index_file(source, role="text", session_id="s1")
    source.write_text("changed notes", encoding="utf-8")

    with pytest.raises(ValueError, match="Indexed file changed"):
        store.read_text(asset.id)


def test_register_external_is_metadata_only_until_first_access(tmp_path):
    source = tmp_path / "external.txt"
    source.write_text("external", encoding="utf-8")
    store = FileStore(tmp_path / "store")

    asset = store.register_external(source, role="text", session_id="s1")

    assert asset.metadata["indexed"] is True
    assert asset.sha256 == ""
    assert asset.stored_path == str(source.resolve())
    assert not any(store.blob_root.iterdir())
