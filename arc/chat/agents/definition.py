"""AgentDefinition + YAML loader."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


logger = logging.getLogger(__name__)


PermissionMode = Literal["default", "auto", "plan"]


class AgentRequirementError(RuntimeError):
    """Raised by ``resolve_agent`` when an agent definition declares a
    requirement that isn't met (e.g. ``requires_provider=True`` but no
    LLM provider available)."""


class AgentDefinition(BaseModel):
    """Declarative agent record.

    Loaded from ``arc/agents/*.yaml`` (built-in), ``~/.config/arc/agents/``
    (user), or ``./.arc/agents/`` (project). The schema is strict —
    unknown fields are rejected so typos in YAML files become loud
    errors at load time rather than silent ignores.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=4096)
    system_prompt: str = Field(default="", max_length=64_000)
    model: str = Field(default="inherit", max_length=128)
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=1, ge=1, le=100)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    permission_mode: PermissionMode = "default"
    requires_provider: bool = True


# ── Loader helpers ───────────────────────────────────────────────────────


def load_agent_definition(path: Path) -> AgentDefinition | None:
    """Read a YAML file and return an AgentDefinition.

    Returns ``None`` (with a warning logged) on:
      * missing / unreadable file
      * malformed YAML
      * schema validation failure

    Never raises on bad input — the loader is best-effort.
    """
    if not path.is_file():
        logger.warning("agent definition path %s is not a file", path)
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read agent definition %s: %s", path, exc)
        return None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("agent definition %s is not valid YAML: %s", path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("agent definition %s must be a mapping", path)
        return None

    try:
        return AgentDefinition(**data)
    except ValidationError as exc:
        logger.warning("agent definition %s rejected: %s", path, exc)
        return None


def _builtin_agents_dir() -> Path:
    # arc/arc/chat/agents/definition.py → arc/arc/agents/
    here = Path(__file__).resolve()
    return here.parent.parent.parent / "agents"


def _user_agents_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "arc" / "agents"


def _project_agents_dir() -> Path:
    return Path.cwd() / ".arc" / "agents"


def _project_agents_enabled() -> bool:
    """Project-local agent definitions only load with explicit opt-in,
    matching the skill-loader policy (P3-3 / OpenHarness plugin trust)."""
    from arc.chat._env import env_flag
    return env_flag("ARC_TRUST_PROJECT_AGENTS")


def discover_agent_definitions(
    *,
    allow_user_overrides: bool = False,
    extra_dirs: list[Path] | None = None,
) -> dict[str, AgentDefinition]:
    """Build a name → AgentDefinition map across all search paths.

    Precedence matches ``skill_loader.discover_skills``: builtin wins
    unless ``allow_user_overrides=True``. Project-local agent definitions
    require ``ARC_TRUST_PROJECT_AGENTS=1`` to load.
    """
    by_source: dict[str, list[AgentDefinition]] = {
        "builtin": [],
        "user":    [],
        "project": [],
    }

    sources = [
        ("builtin", _builtin_agents_dir()),
        ("user",    _user_agents_dir()),
    ]
    if _project_agents_enabled():
        sources.append(("project", _project_agents_dir()))
    else:
        proj = _project_agents_dir()
        if proj.exists() and (list(proj.glob("*.yaml")) + list(proj.glob("*.yml"))):
            logger.warning(
                "project-local agents in %s are disabled by default; set "
                "ARC_TRUST_PROJECT_AGENTS=1 to load them",
                proj,
            )

    for source, root in sources:
        if not root.exists():
            continue
        for path in sorted(list(root.glob("*.yaml")) + list(root.glob("*.yml"))):
            d = load_agent_definition(path)
            if d is not None:
                by_source[source].append(d)

    for extra_root in (extra_dirs or []):
        if not extra_root.exists():
            continue
        for path in sorted(list(extra_root.glob("*.yaml")) + list(extra_root.glob("*.yml"))):
            d = load_agent_definition(path)
            if d is not None:
                by_source.setdefault("extra", []).append(d)

    merged: dict[str, AgentDefinition] = {}
    for d in by_source["builtin"]:
        merged[d.name] = d
    for source in ("user", "project", "extra"):
        for d in by_source.get(source, []):
            if d.name in merged:
                if allow_user_overrides:
                    merged[d.name] = d
                else:
                    logger.warning(
                        "skipping %s agent %r — overrides built-in (set "
                        "[agents].allow_user_overrides=true in arc.toml to permit)",
                        source, d.name,
                    )
            else:
                merged[d.name] = d

    return merged


# ── Runtime resolver ─────────────────────────────────────────────────────


def resolve_agent(
    name: str,
    *,
    provider: Any | None = None,
    definitions: dict[str, AgentDefinition] | None = None,
) -> AgentDefinition:
    """Return an ``AgentDefinition`` ready to invoke.

    Enforces declared requirements:
      * ``requires_provider=True`` and ``provider is None`` → raises
        ``AgentRequirementError``.

    The default ``definitions`` argument re-discovers from disk on every
    call; callers should pass a cached map for hot paths.

    Raises:
        KeyError: if the named agent isn't defined.
        AgentRequirementError: if a declared requirement isn't met.
    """
    if definitions is None:
        definitions = discover_agent_definitions()
    if name not in definitions:
        raise KeyError(
            f"agent {name!r} is not defined; available: "
            f"{sorted(definitions.keys())}"
        )
    agent = definitions[name]
    if agent.requires_provider and provider is None:
        raise AgentRequirementError(
            f"agent {agent.name!r} requires an LLM provider but none is "
            f"configured (run without --stub, or set agent's "
            f"requires_provider=false)"
        )
    return agent
