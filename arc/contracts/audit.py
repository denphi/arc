"""Audit-action contract — package-provided observers across the research
lifecycle (design/todo.md item 7).

Packages can already provide agents, skills, adapters, evaluators, prompts,
templates, constraints, vocabularies, providers, workflows, and extensions.
Auditing was the gap: a domain package could not say "after ideation, check
vocabulary coverage" or "after validation, record a units audit" without
being called directly by a strategy or patching core.

An :class:`AuditActionContract` is a small async observer bound to a named
lifecycle ``phase``. The :class:`~arc.runtime.audit.AuditDispatcher` invokes
every registered action for a phase, ordered by ``priority``, persists the
results to provenance + session state, and emits them to the UI event
stream. A ``blocking`` action that returns ``fail`` aborts the run with a
clear message; non-blocking actions only surface warnings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


# Stable lifecycle phases. Both the YAML workflow path and the chat phase
# path dispatch at these points so audits are not UI-specific.
AUDIT_PHASES: tuple[str, ...] = (
    "goal.received",
    "ideation.before",
    "ideation.after",
    "search.after",
    "planning.after",
    "build_context.after",
    "build.after",
    "validation.before",
    "validation.after",
    "register.after",
    "execution.before",
    "execution.after",
    "review.after",
    "reflection.after",
    "iteration.after",
    "workflow.error",
)


@dataclass
class AuditEvent:
    """Context handed to an audit action when its phase fires."""

    session_id: str
    phase: str
    iteration: int = 0
    role: str | None = None
    strategy: str | None = None
    artifact_id: str | None = None
    run_id: str | None = None
    input_summary: Any = None
    output_summary: Any = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "iteration": self.iteration,
            "role": self.role,
            "strategy": self.strategy,
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


@dataclass
class AuditResult:
    """What an audit action returns."""

    status: Literal["pass", "warn", "fail", "info"] = "info"
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    blocking: bool = False
    artifact_refs: list[str] = field(default_factory=list)
    # Filled in by the dispatcher so persisted results know their origin.
    name: str = ""
    phase: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "phase": self.phase,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
            "tags": self.tags,
            "blocking": self.blocking,
            "artifact_refs": self.artifact_refs,
        }


class AuditActionContract(ABC):
    """A package-provided observer bound to one lifecycle phase.

    Implementations declare ``name``, ``phase`` (one of :data:`AUDIT_PHASES`),
    an integer ``priority`` (lower runs first), and whether they are
    ``blocking``. ``audit`` is called with the phase's :class:`AuditEvent`
    and an ``AgentContext``-like object (so the action can reach
    ``context.memory`` extensions/registry).
    """

    name: str
    phase: str
    priority: int = 100
    blocking: bool = False

    @abstractmethod
    async def audit(self, event: AuditEvent, context: Any) -> AuditResult:
        raise NotImplementedError


class ReportSectionContract(ABC):
    """A package-provided contributor to the final research report.

    Packages register these (via ``provides.report_sections`` in
    ``package.yaml``) so the report assembler can collect domain sections —
    "domain validity", "negative results", "materials constraints", a
    Co-Scientist hypothesis-space overview, … — **without core hard-coding any
    package name** (design/todo.md item 7). ``contribute`` returns a JSON-able
    section value (or ``None`` to skip); ``assemble_report`` files it under
    ``section_name`` in the report's ``sections`` map.
    """

    name: str
    section_name: str

    @abstractmethod
    def contribute(self, context: Any) -> Any:
        """Return this package's report section, or ``None`` to contribute none."""
        raise NotImplementedError
