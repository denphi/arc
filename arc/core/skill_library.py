"""Filesystem helpers for mixed legacy and bundled skill libraries."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


def list_skill_entries(root: Path) -> list[Path]:
    """Return legacy ``*.md`` skills plus canonical ``*/SKILL.md`` bundles."""
    return sorted([*root.glob("*.md"), *root.glob("*/SKILL.md")], key=skill_entry_name)


def skill_entry_name(path: Path) -> str:
    return path.parent.name if path.name == "SKILL.md" else path.stem


def skill_entry_digest(path: Path) -> str:
    digest = hashlib.sha256()
    files = [path] if path.name != "SKILL.md" else sorted(
        item for item in path.parent.rglob("*") if item.is_file()
    )
    for item in files:
        digest.update(item.relative_to(path.parent).as_posix().encode("utf-8"))
        digest.update(item.read_bytes())
    return digest.hexdigest()


def copy_skill_entry(path: Path, target: Path, *, force: bool = False) -> str:
    """Copy one skill entry, returning copied/same/conflict/overwritten."""
    if path.name == "SKILL.md":
        destination = target / path.parent.name
        existing_entry = destination / "SKILL.md"
    else:
        destination = target / path.name
        existing_entry = destination
    if existing_entry.exists():
        if skill_entry_digest(existing_entry) == skill_entry_digest(path):
            return "same"
        if not force:
            return "conflict"
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
        status = "overwritten"
    else:
        status = "copied"
    if path.name == "SKILL.md":
        shutil.copytree(path.parent, destination)
    else:
        shutil.copy2(path, destination)
    return status


def delete_skill_entry(path: Path) -> None:
    if path.name == "SKILL.md":
        shutil.rmtree(path.parent)
    else:
        path.unlink()
