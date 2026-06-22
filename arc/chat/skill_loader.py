"""Markdown skill loader with YAML frontmatter (Phase 3).

OpenHarness-style skill loading:

  * Skills are markdown files with optional YAML frontmatter (``---`` …
    ``---`` at the top).
  * Bodies are NOT loaded at discovery time — only frontmatter is parsed
    so listing skills is cheap.
  * Three search paths, in precedence order:

      1. Built-in: ``arc/skills/core/*.md``
      2. User:     ``~/.config/arc/skills/*.md``  (or ``$XDG_CONFIG_HOME``)
      3. Project:  ``./.arc/skills/*.md``

  * User and project skills can override builtins ONLY when explicitly
    enabled via ``arc.toml [skills] allow_user_overrides = true``.
    Default off.
  * Frontmatter is parsed with ``yaml.safe_load``. Bad frontmatter is
    logged and the skill is skipped — never crashes startup.
  * Skill body size capped at ``MAX_SKILL_BYTES`` to prevent runaway
    files from blowing up the system prompt.

Used by the chat's ``/skill`` command (Phase 4) and the agent-defined
skill catalog. The existing package-manifest-driven loader in
``arc/core/loader.py`` is unaffected.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


# Hard cap on a single skill body. Above this, the loader refuses to load
# the body (description still indexed). Prevents accidental DOS-by-prompt.
MAX_SKILL_BYTES = 256 * 1024  # 256 KiB


# ── Frontmatter schema ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillFrontmatter:
    """Validated YAML frontmatter for a skill file.

    Unknown fields are preserved in ``extra`` so future fields don't
    silently disappear, but the typed fields are the contract.
    """

    name: str
    description: str = ""
    user_invocable: bool = False
    disable_model_invocation: bool = False
    model: str = "inherit"
    argument_hint: str = ""
    extra: dict = field(default_factory=dict)


def parse_frontmatter(text: str) -> tuple[SkillFrontmatter | None, str]:
    """Split a skill file into (frontmatter, body).

    Returns ``(None, full_text)`` when there's no frontmatter. Returns
    ``(None, body)`` when the frontmatter is malformed — the caller
    should log + skip.
    """
    if not text.startswith("---"):
        # No frontmatter
        return None, text

    # Find the closing ``---``
    rest = text[3:].lstrip("\n")
    end = rest.find("\n---")
    if end == -1:
        # Unclosed delimiter — treat the whole file as body
        return None, text

    block = rest[:end]
    body = rest[end + 4:].lstrip("\n")

    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        logger.warning("skill frontmatter is not valid YAML: %s", exc)
        return None, body

    if not isinstance(data, dict):
        logger.warning("skill frontmatter must be a mapping, got %s", type(data).__name__)
        return None, body

    name = data.pop("name", None)
    if not name or not isinstance(name, str):
        logger.warning("skill frontmatter missing required field 'name'")
        return None, body

    description = data.pop("description", "")
    if description and not isinstance(description, str):
        logger.warning("skill %r: 'description' must be a string", name)
        description = str(description)

    fm = SkillFrontmatter(
        name=name,
        description=description,
        user_invocable=bool(data.pop("user-invocable",
                                      data.pop("user_invocable", False))),
        disable_model_invocation=bool(data.pop("disable-model-invocation",
                                                data.pop("disable_model_invocation", False))),
        model=str(data.pop("model", "inherit")),
        argument_hint=str(data.pop("argument-hint",
                                    data.pop("argument_hint", ""))),
        extra=data,
    )
    return fm, body


# ── Skill record ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillRecord:
    """A single skill discovered on disk.

    The body is loaded LAZILY via ``body()`` — the registry only holds
    metadata until invocation.
    """

    name: str
    path: Path
    source: str            # "builtin" | "user" | "project"
    frontmatter: Optional[SkillFrontmatter]
    description: str
    user_invocable: bool
    disable_model_invocation: bool
    model: str
    argument_hint: str

    def body(self) -> str:
        """Read and return the skill body. Size-capped."""
        size = self.path.stat().st_size
        if size > MAX_SKILL_BYTES:
            raise RuntimeError(
                f"skill {self.name!r} body too large ({size} bytes, max {MAX_SKILL_BYTES})"
            )
        text = self.path.read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        return body

    def available_to_model(self) -> bool:
        return not self.disable_model_invocation

    def available_to_user(self) -> bool:
        return self.user_invocable


# ── Search paths ─────────────────────────────────────────────────────────


def _builtin_skills_dir() -> Path:
    # arc/arc/chat/skill_loader.py  →  arc/arc/skills/core
    here = Path(__file__).resolve()
    return here.parent.parent / "skills" / "core"


def _user_skills_dir() -> Path:
    base = Path(os.environ.get(
        "XDG_CONFIG_HOME",
        Path.home() / ".config",
    ))
    return base / "arc" / "skills"


def _project_skills_dir() -> Path:
    return Path.cwd() / ".arc" / "skills"


def _project_skills_enabled() -> bool:
    """Project-local skills load only when the user explicitly opts in
    via ``ARC_TRUST_PROJECT_SKILLS=1`` or the equivalent ``arc.toml``
    setting. Otherwise a malicious ``.arc/skills/`` planted in the
    current working directory would auto-load.
    """
    from arc.chat._env import env_flag
    return env_flag("ARC_TRUST_PROJECT_SKILLS")


# ── Discovery ────────────────────────────────────────────────────────────


def _discover_one(path: Path, source: str) -> SkillRecord | None:
    """Open ``path``, parse frontmatter, return a SkillRecord.

    Returns None on any parse failure (logged).
    """
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("could not stat skill %s: %s", path, exc)
        return None
    if size > MAX_SKILL_BYTES:
        logger.warning("skipping oversized skill %s (%d bytes)", path, size)
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read skill %s: %s", path, exc)
        return None

    fm, body = parse_frontmatter(text)
    if fm is None:
        # Allow skills without frontmatter — fall back to the filename stem
        # for name and a heuristic description, but flag them.
        name = path.stem
        description = _extract_description_fallback(body) or name
        return SkillRecord(
            name=name,
            path=path,
            source=source,
            frontmatter=None,
            description=description,
            user_invocable=False,
            disable_model_invocation=False,
            model="inherit",
            argument_hint="",
        )

    return SkillRecord(
        name=fm.name,
        path=path,
        source=source,
        frontmatter=fm,
        description=fm.description or fm.name,
        user_invocable=fm.user_invocable,
        disable_model_invocation=fm.disable_model_invocation,
        model=fm.model,
        argument_hint=fm.argument_hint,
    )


def _skill_paths(root: Path) -> list[Path]:
    """Discover legacy flat skills and canonical bundle entry files."""
    return sorted(
        [*root.glob("*.md"), *root.glob("*/SKILL.md")],
        key=lambda path: (path.parent.name if path.name == "SKILL.md" else path.stem),
    )


def _extract_description_fallback(body: str) -> str:
    """For skills without frontmatter: look for a ``## Description`` block."""
    marker = "## Description"
    if marker not in body:
        return ""
    section = body.split(marker, 1)[1]
    if "\n## " in section:
        section = section.split("\n## ", 1)[0]
    return section.strip().split("\n")[0]  # first line only


def discover_skills(
    *,
    allow_user_overrides: bool = False,
    extra_dirs: list[Path] | None = None,
) -> dict[str, SkillRecord]:
    """Build a name→SkillRecord map across all search paths.

    Precedence: project > user > builtin, but only if
    ``allow_user_overrides=True``. With the default ``False``, builtins
    win — user/project skills with conflicting names are dropped with
    a warning.
    """
    by_source: dict[str, list[SkillRecord]] = {
        "builtin": [],
        "user":    [],
        "project": [],
    }

    sources = [
        ("builtin", _builtin_skills_dir()),
        ("user",    _user_skills_dir()),
    ]
    if _project_skills_enabled():
        sources.append(("project", _project_skills_dir()))
    else:
        # Soft warning if a .arc/skills/ is sitting in CWD but disabled,
        # so the user knows it exists and can opt in.
        proj = _project_skills_dir()
        if proj.exists() and _skill_paths(proj):
            logger.warning(
                "project-local skills in %s are disabled by default; set "
                "ARC_TRUST_PROJECT_SKILLS=1 to load them",
                proj,
            )

    for source, root in sources:
        if not root.exists():
            continue
        for path in _skill_paths(root):
            rec = _discover_one(path, source)
            if rec is not None:
                by_source[source].append(rec)

    for extra_root in (extra_dirs or []):
        if not extra_root.exists():
            continue
        for path in _skill_paths(extra_root):
            rec = _discover_one(path, "extra")
            if rec is not None:
                by_source.setdefault("extra", []).append(rec)

    merged: dict[str, SkillRecord] = {}
    # Builtins first
    for rec in by_source["builtin"]:
        merged[rec.name] = rec
    # User / project: either override or be dropped
    for source in ("user", "project", "extra"):
        for rec in by_source.get(source, []):
            if rec.name in merged:
                if allow_user_overrides:
                    merged[rec.name] = rec
                else:
                    logger.warning(
                        "skipping %s skill %r — overrides built-in (set "
                        "[skills].allow_user_overrides=true in arc.toml to permit)",
                        source, rec.name,
                    )
            else:
                merged[rec.name] = rec

    return merged
