"""Importable audit-action fixture for the loader test (item 7).

Lives in the test package so a temp ``package.yaml`` can name it via an
ordinary dotted entrypoint (``tests._audit_fixture:DemoAudit``) without the
filesystem-import dance the bundled hyphenated packages need.
"""

from __future__ import annotations

from arc.contracts.audit import (
    AuditActionContract,
    AuditEvent,
    AuditResult,
    ReportSectionContract,
)


class DemoAudit(AuditActionContract):
    name = "demo_audit"
    phase = "validation.after"
    priority = 20

    async def audit(self, event: AuditEvent, context) -> AuditResult:
        return AuditResult(status="pass", summary="demo audit ran")


class DemoReportSection(ReportSectionContract):
    name = "demo_report_section"
    section_name = "demo_domain"

    def contribute(self, context) -> dict:
        return {"checked": True, "session": getattr(context, "session_id", None)}


class FailingReportSection(ReportSectionContract):
    """Imports fine but raises at instantiation — exercises the loader's
    swallow-on-failure path so `arc package validate` can catch it."""

    name = "failing_report_section"
    section_name = "failing"

    def __init__(self):
        raise RuntimeError("intentional instantiation failure")

    def contribute(self, context):  # pragma: no cover - never constructed
        return {}
