"""Canonical ARC skill bundles, compatible with the Agent Skills layout."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024
_MAX_COMPATIBILITY_LENGTH = 500
_MAX_SKILL_BYTES = 256 * 1024
_KNOWN_FIELDS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
    # ARC extensions retained for executable workflow skills.
    "output_format",
    "output_schema",
    "user-invocable",
    "disable-model-invocation",
    "model",
    "argument-hint",
}


@dataclass(frozen=True)
class SkillBundle:
    """Metadata-only representation of a ``SKILL.md`` bundle."""

    name: str
    description: str
    skill_path: Path
    root: Path
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def instructions(self) -> str:
        """Load the full skill instructions on activation."""
        size = self.skill_path.stat().st_size
        if size > _MAX_SKILL_BYTES:
            raise ValueError(
                f"skill instructions exceed {_MAX_SKILL_BYTES} bytes: {self.skill_path}"
            )
        text = self.skill_path.read_text(encoding="utf-8")
        _, body = split_frontmatter(text)
        return body

    def list_resources(self, subdir: str | None = None) -> list[str]:
        base = self.root / subdir if subdir else self.root
        if not base.exists() or not base.is_dir():
            return []
        resources: list[str] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path == self.skill_path:
                continue
            try:
                resources.append(path.resolve().relative_to(self.root.resolve()).as_posix())
            except ValueError:
                continue
        return resources

    def resolve_resource(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise ValueError("Skill resources must use relative paths")
        root = self.root.resolve()
        path = (root / raw).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Skill resource escapes bundle root: {relative_path}") from exc
        if not path.is_file():
            raise FileNotFoundError(f"Skill resource not found: {relative_path}")
        return path


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return parsed YAML frontmatter and the Markdown body."""
    if not text.startswith("---"):
        return {}, text
    rest = text[3:].lstrip("\n")
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    data = yaml.safe_load(rest[:end])
    return (data if isinstance(data, dict) else {}), rest[end + 4:].lstrip("\n")


def load_skill_bundle(path: Path) -> SkillBundle:
    """Load bundle metadata without retaining its instruction body."""
    skill_path = path / "SKILL.md" if path.is_dir() else path
    if skill_path.name != "SKILL.md":
        raise ValueError(f"canonical skill bundle entry must be SKILL.md: {skill_path}")
    if not skill_path.is_file():
        raise FileNotFoundError(skill_path)
    if skill_path.stat().st_size > _MAX_SKILL_BYTES:
        raise ValueError(f"skill instructions exceed {_MAX_SKILL_BYTES} bytes: {skill_path}")
    frontmatter, _ = split_frontmatter(skill_path.read_text(encoding="utf-8"))
    allowed = frontmatter.get("allowed-tools") or []
    if isinstance(allowed, str):
        allowed = [item for item in allowed.split() if item]
    elif not isinstance(allowed, list):
        allowed = []
    metadata = frontmatter.get("metadata") or {}
    extra = {key: value for key, value in frontmatter.items() if key not in _KNOWN_FIELDS}
    return SkillBundle(
        name=str(frontmatter.get("name") or ""),
        description=str(frontmatter.get("description") or ""),
        skill_path=skill_path,
        root=skill_path.parent,
        license=str(frontmatter["license"]) if frontmatter.get("license") else None,
        compatibility=(
            str(frontmatter["compatibility"]) if frontmatter.get("compatibility") else None
        ),
        allowed_tools=tuple(str(item) for item in allowed),
        metadata=metadata if isinstance(metadata, dict) else {},
        extra=extra,
    )


def validate_skill_bundle(path: Path) -> list[str]:
    """Validate one canonical Agent Skills-style bundle."""
    try:
        bundle = load_skill_bundle(path)
        raw_frontmatter, _ = split_frontmatter(
            bundle.skill_path.read_text(encoding="utf-8")
        )
    except Exception as exc:  # noqa: BLE001
        return [str(exc)]

    errors: list[str] = []
    if not bundle.name:
        errors.append("SKILL.md frontmatter must declare name")
    elif len(bundle.name) > _MAX_NAME_LENGTH or not _NAME_RE.fullmatch(bundle.name):
        errors.append(
            "skill name must be 1-64 lowercase letters, digits, or single hyphens"
        )
    elif bundle.root.name != bundle.name:
        errors.append(
            f"skill directory name {bundle.root.name!r} must match skill name {bundle.name!r}"
        )

    if not bundle.description:
        errors.append("SKILL.md frontmatter must declare description")
    elif len(bundle.description) > _MAX_DESCRIPTION_LENGTH:
        errors.append(f"skill description exceeds {_MAX_DESCRIPTION_LENGTH} characters")

    if (
        bundle.compatibility is not None
        and len(bundle.compatibility) > _MAX_COMPATIBILITY_LENGTH
    ):
        errors.append(f"skill compatibility exceeds {_MAX_COMPATIBILITY_LENGTH} characters")
    for field_name in ("name", "description", "license", "compatibility"):
        value = raw_frontmatter.get(field_name)
        if value is not None and not isinstance(value, str):
            errors.append(f"SKILL.md frontmatter field {field_name!r} must be a string")
    metadata = raw_frontmatter.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append("SKILL.md frontmatter field 'metadata' must be a mapping")
    allowed_tools = raw_frontmatter.get("allowed-tools")
    if allowed_tools is not None and not (
        isinstance(allowed_tools, str)
        or (
            isinstance(allowed_tools, list)
            and all(isinstance(item, str) for item in allowed_tools)
        )
    ):
        errors.append("SKILL.md frontmatter field 'allowed-tools' must be a string or list")
    if bundle.extra:
        errors.append("unknown SKILL.md frontmatter fields: " + ", ".join(sorted(bundle.extra)))
    return errors
