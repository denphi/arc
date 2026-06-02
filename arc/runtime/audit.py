"""Audit dispatcher + report assembly (design/todo.md item 7).

The :class:`AuditDispatcher` is the single call site the research loop uses
to fire package-provided audit actions at a lifecycle phase. It:

  * resolves the actions registered for that phase (priority order),
  * runs each with the phase :class:`AuditEvent` + the agent context,
  * persists every :class:`AuditResult` to provenance + session memory,
  * emits a structured event so the UI/JSONL sink can show it,
  * raises :class:`AuditBlockedError` when a *blocking* action fails,
    so the run aborts with a clear, attributable message.

A disabled package contributes no actions (they're never registered), so a
loop with no audit packages is a clean no-op.

:func:`assemble_report` turns the accumulated audit results (plus reviews /
run history / artifacts) into a structured report dict that packages can
extend with their own sections — no package names are hard-coded in core.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from arc.contracts.audit import AUDIT_PHASES, AuditEvent, AuditResult

logger = logging.getLogger(__name__)


class AuditBlockedError(RuntimeError):
    """Raised when a blocking audit action returns ``status == 'fail'``."""

    def __init__(self, result: AuditResult):
        self.result = result
        super().__init__(
            f"Blocking audit {result.name!r} failed at {result.phase}: {result.summary}"
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditDispatcher:
    """Fire audit actions for lifecycle phases. Construct once per workflow."""

    def __init__(self, registry: Any, context: Any, provenance: Any = None):
        self._registry = registry
        self._context = context
        self._provenance = provenance
        # Optional non-chat sink, e.g. the browser UI's per-job EventStream.
        # Receives the persisted audit-result dict. Stays independent of
        # arc.chat (the UI must not import chat). Set via set_event_sink().
        self._event_sink = None

    def set_event_sink(self, sink) -> None:
        """Attach a ``callable(result_dict) -> None`` invoked for every
        persisted audit result, so non-chat consumers (the UI SSE stream)
        receive audit events alongside phase events (item 7 acceptance)."""
        self._event_sink = sink

    def _disabled_packages(self) -> set[str]:
        """The session's ``/package disable`` set, read from the context.

        A disabled package's audit actions must not run (review finding A).
        """
        try:
            mem = getattr(self._context, "memory", {}) or {}
            return set((mem.get("packages", {}) or {}).get("disabled", []) or [])
        except Exception:  # noqa: BLE001
            return set()

    def has_actions(self) -> bool:
        if self._registry is None:
            return False
        try:
            disabled = self._disabled_packages()
            return any(
                self._registry.audit_actions_for_phase(p, disabled_packages=disabled)
                for p in AUDIT_PHASES
            )
        except Exception:  # noqa: BLE001
            return False

    async def dispatch(self, phase: str, **event_fields: Any) -> list[AuditResult]:
        """Run every action registered for ``phase``; return their results.

        ``event_fields`` populate the :class:`AuditEvent` (iteration, role,
        artifact_id, run_id, input/output summaries, payload). A non-blocking
        action that raises is logged and skipped; a blocking action that
        raises or returns ``fail`` raises :class:`AuditBlockedError`. Actions
        from a session-disabled package are skipped (review finding A).
        """
        if self._registry is None:
            return []
        try:
            actions = self._registry.audit_actions_for_phase(
                phase, disabled_packages=self._disabled_packages(),
            )
        except Exception:  # noqa: BLE001
            return []
        if not actions:
            return []

        session_id = getattr(self._context, "session_id", None) or event_fields.pop(
            "session_id", "session"
        )
        event = AuditEvent(
            session_id=session_id,
            phase=phase,
            timestamp=_utcnow(),
            **event_fields,
        )

        results: list[AuditResult] = []
        for action in actions:
            blocking = bool(getattr(action, "blocking", False))
            name = getattr(action, "name", action.__class__.__name__)
            try:
                result = await action.audit(event, self._context)
            except Exception as exc:  # noqa: BLE001
                if blocking:
                    failed = AuditResult(
                        status="fail", summary=f"audit raised: {exc}",
                        blocking=True, name=name, phase=phase,
                    )
                    self._persist(failed)
                    raise AuditBlockedError(failed) from exc
                logger.warning("audit action %s failed at %s: %s", name, phase, exc)
                continue
            if not isinstance(result, AuditResult):
                continue
            result.name = result.name or name
            result.phase = phase
            result.blocking = result.blocking or blocking
            results.append(result)
            self._persist(result)
            if result.blocking and result.status == "fail":
                raise AuditBlockedError(result)
        return results

    def _persist(self, result: AuditResult) -> None:
        record = result.to_dict()
        # 1. Session memory — the report assembler reads from here.
        try:
            bucket = self._context.memory.setdefault("audit_results", [])
            bucket.append(record)
        except Exception:  # noqa: BLE001
            pass
        # 2. Provenance log.
        if self._provenance is not None:
            try:
                self._provenance.record(
                    session_id=getattr(self._context, "session_id", "session"),
                    action=f"audit:{result.phase}",
                    agent=result.name,
                    outputs=record,
                )
            except Exception:  # noqa: BLE001
                pass
        # 3. Pluggable event sink. ``arc.runtime`` does NOT depend on
        #    ``arc.chat`` (review finding B) — each front-end attaches its own
        #    sink: the chat loop wires an arc.chat.events bridge, the browser
        #    UI wires its per-job EventStream. No sink → provenance + session
        #    memory still captured above.
        if self._event_sink is not None:
            try:
                self._event_sink(record)
            except Exception:  # noqa: BLE001
                pass


def assemble_report(context: Any, *, extra_sections: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a structured research report from accumulated state.

    Consumes the audit results, review, and run history on ``context.memory``
    and produces a report dict with a generic skeleton plus an ``audits``
    section grouped by phase. Packages extend the report by passing
    ``extra_sections`` (e.g. a domain-validity or negative-results section)
    — no package name is hard-coded here.
    """
    memory = getattr(context, "memory", {}) or {}
    audit_results: list[dict[str, Any]] = memory.get("audit_results", []) or []

    by_phase: dict[str, list[dict[str, Any]]] = {}
    for r in audit_results:
        by_phase.setdefault(r.get("phase", "unknown"), []).append(r)

    statuses = [r.get("status") for r in audit_results]
    report: dict[str, Any] = {
        "session_id": getattr(context, "session_id", None),
        "iteration": getattr(context, "iteration", 0),
        "summary": {
            "audit_count": len(audit_results),
            "failed": statuses.count("fail"),
            "warnings": statuses.count("warn"),
            "passed": statuses.count("pass"),
        },
        "audits": by_phase,
        "ideator_candidates": memory.get("ideator_candidates", []),
        "ideator_selection_rationale": memory.get("ideator_selection_rationale", ""),
        "run_history": memory.get("run_history", []),
        "sections": {},
    }

    # Collect package-registered report-section contributors (item 7) so a
    # package contributes a section without core naming it. The component
    # registry is stashed on the context by ResearchWorkflow. Contributors
    # from a session-disabled package are excluded (review finding A).
    registry = memory.get("component_registry")
    disabled_packages = set((memory.get("packages", {}) or {}).get("disabled", []) or [])
    if registry is not None and hasattr(registry, "report_section_contributors"):
        try:
            for contributor in registry.report_section_contributors(
                disabled_packages=disabled_packages,
            ):
                try:
                    value = contributor.contribute(context)
                except Exception as exc:  # noqa: BLE001 — one section must not break the report
                    logger.debug("report section %s failed: %s",
                                 getattr(contributor, "name", contributor), exc)
                    continue
                if value is not None:
                    key = getattr(contributor, "section_name", None) \
                        or getattr(contributor, "name", "section")
                    report["sections"][key] = value
        except Exception as exc:  # noqa: BLE001
            logger.debug("report-section collection failed: %s", exc)

    # Manual sections (explicit caller-supplied) win on a key collision.
    if extra_sections:
        report["sections"].update(extra_sections)
    return report
