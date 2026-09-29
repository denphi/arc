"""Shared helpers for CLI-backed coding agents (Codex, Claude Code, …).

Both coding packages shell out to an external agent that writes
``workflow.py`` + ``sim2l.yaml`` into a workspace, then turn that workspace
into an :class:`~arc.schemas.artifact.ArtifactDraft`. The pieces that don't
depend on which CLI ran live here so the packages can't drift apart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

_STOP_WORDS = {"a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by", "via"}


def append_option(args: list[str], flag: str, value: str | None) -> None:
    """Append ``flag value`` to ``args`` when ``value`` is non-empty."""
    if value:
        args.extend([flag, value])


def schema_keys_from_yaml(sim2l_path: Path, section: str) -> list[str]:
    """Keys of the ``inputs``/``outputs`` section of a sim2l.yaml ([] on error)."""
    try:
        import yaml

        spec = yaml.safe_load(sim2l_path.read_text(encoding="utf-8")) or {}
        values = spec.get(section, {}) or {}
        return [str(key) for key in values.keys()]
    except Exception:  # noqa: BLE001 — a malformed schema just yields no keys
        return []


def artifact_description(plan: Any, sim2l_path: Path) -> str:
    """A persisted artifact description from the goal and generated schema."""
    objective = " ".join(plan.proposal.objective.split())
    methodology = " ".join(plan.proposal.methodology.split())
    inputs = schema_keys_from_yaml(sim2l_path, "inputs")
    outputs = schema_keys_from_yaml(sim2l_path, "outputs")

    parts = [objective[:180]]
    if inputs:
        parts.append("Inputs: " + ", ".join(inputs[:6]))
    if outputs:
        parts.append("Outputs: " + ", ".join(outputs[:8]))
    elif methodology:
        parts.append(methodology[:120])
    return " | ".join(part for part in parts if part)[:500]


def artifact_slug(plan: Any, fallback: str) -> str:
    """Stable artifact name: up to four objective keywords + a plan digest."""
    words = re.sub(r"[^a-z0-9 ]", " ", plan.proposal.objective.lower()).split()
    stem = "_".join([w for w in words if w not in _STOP_WORDS][:4])[:40] or fallback
    digest = hashlib.sha256(
        json.dumps(plan.model_dump(), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:8]
    return f"{stem}_{digest}"


def allowed_imports_from_env(env_var: str, base: frozenset[str]) -> frozenset[str]:
    """``base`` widened by a comma-separated module list in ``env_var``."""
    extra = frozenset(
        item.strip() for item in os.environ.get(env_var, "").split(",") if item.strip()
    )
    return base | extra


def preflight_workflow(workflow_path: Path, *, extra_imports_env: str) -> None:
    """Validate generated ``workflow.py`` before it becomes an artifact.

    Runs the AST safety lint (stdlib allow-list, widened by
    ``extra_imports_env``) and a timed import in a child process. Raises on
    failure.
    """
    from arc.runtime.workflow_safety import (
        STRICT_ALLOWED_IMPORTS,
        check_workflow_source,
        validate_workflow_import_timeout,
    )

    source = workflow_path.read_text(encoding="utf-8")
    check_workflow_source(
        source,
        allowed_imports=allowed_imports_from_env(extra_imports_env, STRICT_ALLOWED_IMPORTS),
    )
    module_name = f"arc_coder_preflight_{os.getpid()}_{abs(hash(str(workflow_path)))}"
    validate_workflow_import_timeout(source, module_name, str(workflow_path))


def usage_limit_hit(text: str | None) -> bool:
    """True when CLI output reports an exhausted usage/credit limit."""
    lowered = (text or "").lower()
    return "usage limit" in lowered or "purchase more credits" in lowered
