"""Report sections contributed by the ARC Co-Scientist adapter."""

from __future__ import annotations

from typing import Any

from arc.contracts.audit import ReportSectionContract


class CoScientistHypothesisSection(ReportSectionContract):
    name = "coscientist_hypotheses"
    section_name = "coscientist_hypotheses"

    def contribute(self, context: Any) -> Any:
        memory = getattr(context, "memory", {}) or {}
        pool = memory.get("coscientist_hypothesis_pool")
        sessions = memory.get("coscientist_sessions") or []
        if not pool and not sessions:
            return None
        return {
            "hypothesis_pool": pool,
            "sessions": sessions,
        }

