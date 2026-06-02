"""Package-provided audit hooks across the research lifecycle
(design/todo.md item 7).

Covers the contract + registry + dispatcher + workflow wiring + report
assembly, and confirms a loop with no audit actions is a clean no-op.
"""

from __future__ import annotations

import asyncio

import pytest

from arc.contracts.audit import AUDIT_PHASES, AuditActionContract, AuditEvent, AuditResult
from arc.core.registry import ComponentRegistry
from arc.orchestrator.workflow import ResearchWorkflow
from arc.runtime.audit import AuditBlockedError, AuditDispatcher, assemble_report
from arc.schemas.research import ResearchGoal

pytestmark = pytest.mark.chat


class _RecordingAudit(AuditActionContract):
    name = "recording_audit"
    phase = "ideation.after"
    priority = 10

    def __init__(self):
        self.calls: list[AuditEvent] = []

    async def audit(self, event: AuditEvent, context) -> AuditResult:
        self.calls.append(event)
        return AuditResult(status="pass", summary=f"saw {event.phase}", tags=["t"])


class _BlockingAudit(AuditActionContract):
    name = "blocking_audit"
    phase = "ideation.after"
    priority = 5
    blocking = True

    async def audit(self, event: AuditEvent, context) -> AuditResult:
        return AuditResult(status="fail", summary="hard stop", blocking=True)


# ── registry ordering ───────────────────────────────────────────────────


def test_registry_orders_actions_by_priority():
    reg = ComponentRegistry()

    class _A(AuditActionContract):
        name, phase, priority = "a", "build.after", 50

        async def audit(self, event, context):
            return AuditResult()

    class _B(AuditActionContract):
        name, phase, priority = "b", "build.after", 10

        async def audit(self, event, context):
            return AuditResult()

    reg.register_audit_action(_A())
    reg.register_audit_action(_B())
    names = [a.name for a in reg.audit_actions_for_phase("build.after")]
    assert names == ["b", "a"]  # priority ascending
    assert reg.list_audit_actions() == ["a", "b"]


# ── dispatcher ──────────────────────────────────────────────────────────


def test_dispatcher_runs_actions_and_persists():
    reg = ComponentRegistry()
    action = _RecordingAudit()
    reg.register_audit_action(action)

    from types import SimpleNamespace
    ctx = SimpleNamespace(session_id="s1", memory={}, iteration=0)
    disp = AuditDispatcher(reg, ctx)
    assert disp.has_actions()

    results = asyncio.run(disp.dispatch("ideation.after", iteration=1))
    assert len(results) == 1
    assert results[0].status == "pass"
    assert results[0].name == "recording_audit"
    # Persisted to context memory for the report.
    assert ctx.memory["audit_results"][0]["phase"] == "ideation.after"
    assert action.calls[0].iteration == 1


def test_dispatcher_blocking_failure_raises():
    reg = ComponentRegistry()
    reg.register_audit_action(_BlockingAudit())
    from types import SimpleNamespace
    ctx = SimpleNamespace(session_id="s", memory={}, iteration=0)
    disp = AuditDispatcher(reg, ctx)
    with pytest.raises(AuditBlockedError):
        asyncio.run(disp.dispatch("ideation.after"))


def test_dispatcher_event_sink_receives_results():
    """A non-chat sink (e.g. the UI's per-job EventStream) gets every
    persisted audit result (item 7 / review finding 4)."""
    reg = ComponentRegistry()
    reg.register_audit_action(_RecordingAudit())
    from types import SimpleNamespace
    ctx = SimpleNamespace(session_id="s", memory={}, iteration=0)
    disp = AuditDispatcher(reg, ctx)
    seen: list[dict] = []
    disp.set_event_sink(seen.append)
    asyncio.run(disp.dispatch("ideation.after"))
    assert seen and seen[0]["phase"] == "ideation.after"
    assert seen[0]["name"] == "recording_audit"


def test_dispatcher_noop_without_actions():
    reg = ComponentRegistry()
    from types import SimpleNamespace
    ctx = SimpleNamespace(session_id="s", memory={}, iteration=0)
    disp = AuditDispatcher(reg, ctx)
    assert disp.has_actions() is False
    assert asyncio.run(disp.dispatch("ideation.after")) == []


def test_all_phases_are_known():
    # Every step-phase the workflow dispatches must be a declared phase.
    for phase in ("goal.received", "ideation.after", "planning.after",
                  "build.after", "validation.after", "register.after",
                  "execution.after", "review.after", "reflection.after",
                  "iteration.after", "workflow.error"):
        assert phase in AUDIT_PHASES


# ── full workflow integration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_workflow_fires_audits_at_phases():
    workflow = ResearchWorkflow()
    action = _RecordingAudit()
    workflow.registry.register_audit_action(action)
    # goal.received is also exercised.

    class _GoalAudit(AuditActionContract):
        name, phase, priority = "goal_audit", "goal.received", 1

        def __init__(self):
            self.seen = False

        async def audit(self, event, context):
            self.seen = True
            return AuditResult(status="info", summary="goal seen")

    goal_audit = _GoalAudit()
    workflow.registry.register_audit_action(goal_audit)

    result = await workflow.run_once(
        ResearchGoal(goal="parameter doubling", target={"result": 2.0})
    )
    assert result["status"] == "completed"
    assert goal_audit.seen is True
    assert action.calls, "ideation.after audit should have fired"
    # Results accumulated on the context for the report.
    report = workflow.assemble_report()
    assert report["summary"]["audit_count"] >= 2
    assert "ideation.after" in report["audits"]


@pytest.mark.asyncio
async def test_workflow_fires_before_and_after_phases():
    """The YAML engine fires the declared `.before` phases too, not only the
    `.after` ones (review recommendation / item 7 acceptance)."""
    workflow = ResearchWorkflow()
    fired: list[str] = []

    def _make(phase_name):
        class _P(AuditActionContract):
            name, phase, priority = f"rec_{phase_name}", phase_name, 1

            async def audit(self, event, context):
                fired.append(event.phase)
                return AuditResult(status="info", summary=phase_name)
        return _P()

    for ph in ("ideation.before", "ideation.after", "planning.after",
               "validation.before", "validation.after", "execution.before",
               "execution.after", "review.after", "iteration.after"):
        workflow.registry.register_audit_action(_make(ph))

    result = await workflow.run_once(
        ResearchGoal(goal="parameter doubling", target={"result": 2.0})
    )
    assert result["status"] == "completed"
    # Pre-flight phases must have fired.
    assert "ideation.before" in fired
    assert "validation.before" in fired
    assert "execution.before" in fired
    # …and their after counterparts.
    assert "execution.after" in fired
    assert "iteration.after" in fired


@pytest.mark.asyncio
async def test_workflow_clean_without_audits():
    workflow = ResearchWorkflow()
    assert workflow.audit.has_actions() is False
    result = await workflow.run_once(
        ResearchGoal(goal="parameter doubling", target={"result": 2.0})
    )
    assert result["status"] == "completed"


# ── report assembly ─────────────────────────────────────────────────────


def test_manifest_declares_audit_action(tmp_path):
    """A package can declare an audit action in package.yaml; loading it
    registers the action under its phase (acceptance for item 7)."""
    from arc.core.loader import load_package

    pkg = tmp_path / "arc-demo-audit"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: arc-demo-audit\n"
        "version: 0.0.1\n"
        "provides:\n"
        "  audit_actions:\n"
        "    - name: demo_audit\n"
        "      phase: validation.after\n"
        "      entrypoint: tests._audit_fixture:DemoAudit\n"
        "      blocking: false\n"
    )
    reg = ComponentRegistry()
    load_package(pkg, reg)
    actions = reg.audit_actions_for_phase("validation.after")
    assert [a.name for a in actions] == ["demo_audit"]


def test_manifest_declares_report_section(tmp_path):
    """A package can declare a report-section contributor in package.yaml;
    assemble_report() collects it from the registry without core naming the
    package (item 7 / review finding 5)."""
    from types import SimpleNamespace

    from arc.core.loader import load_package

    pkg = tmp_path / "arc-demo-report"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: arc-demo-report\n"
        "version: 0.0.1\n"
        "provides:\n"
        "  report_sections:\n"
        "    - name: demo_report_section\n"
        "      section_name: demo_domain\n"
        "      entrypoint: tests._audit_fixture:DemoReportSection\n"
    )
    reg = ComponentRegistry()
    load_package(pkg, reg)
    assert "demo_report_section" in reg.list_report_sections()

    ctx = SimpleNamespace(session_id="s9", iteration=0,
                          memory={"component_registry": reg})
    report = assemble_report(ctx)
    assert report["sections"]["demo_domain"] == {"checked": True, "session": "s9"}


def test_assemble_report_groups_by_phase_and_takes_sections():
    from types import SimpleNamespace
    ctx = SimpleNamespace(session_id="s", iteration=2, memory={
        "audit_results": [
            {"phase": "build.after", "status": "pass", "name": "x"},
            {"phase": "build.after", "status": "warn", "name": "y"},
            {"phase": "review.after", "status": "fail", "name": "z"},
        ],
    })
    report = assemble_report(ctx, extra_sections={"domain_validity": {"ok": True}})
    assert report["summary"] == {"audit_count": 3, "failed": 1, "warnings": 1, "passed": 1}
    assert set(report["audits"]) == {"build.after", "review.after"}
    assert report["sections"]["domain_validity"] == {"ok": True}


# ── Finding A: disabled packages must not contribute audits/report sections ──


def test_disabled_package_audit_action_does_not_run():
    from types import SimpleNamespace

    reg = ComponentRegistry()
    action = _RecordingAudit()
    reg.register_audit_action(action, package_name="arc-demo")

    # Enabled → fires.
    ctx_on = SimpleNamespace(session_id="s", memory={}, iteration=0)
    assert AuditDispatcher(reg, ctx_on).has_actions() is True
    asyncio.run(AuditDispatcher(reg, ctx_on).dispatch("ideation.after"))
    assert ctx_on.memory.get("audit_results")

    # Disabled in this session → no actions, no results.
    ctx_off = SimpleNamespace(
        session_id="s", iteration=0,
        memory={"packages": {"disabled": ["arc-demo"]}},
    )
    disp = AuditDispatcher(reg, ctx_off)
    assert disp.has_actions() is False
    assert asyncio.run(disp.dispatch("ideation.after")) == []
    assert not ctx_off.memory.get("audit_results")


def test_disabled_package_blocking_audit_cannot_abort_run():
    """A disabled package's *blocking* audit must not be able to abort."""
    from types import SimpleNamespace

    reg = ComponentRegistry()
    reg.register_audit_action(_BlockingAudit(), package_name="arc-demo")
    ctx = SimpleNamespace(
        session_id="s", iteration=0,
        memory={"packages": {"disabled": ["arc-demo"]}},
    )
    # No AuditBlockedError because the action is filtered out entirely.
    assert asyncio.run(AuditDispatcher(reg, ctx).dispatch("ideation.after")) == []


def test_disabled_package_report_section_excluded():
    from types import SimpleNamespace
    from tests._audit_fixture import DemoReportSection

    reg = ComponentRegistry()
    reg.register_report_section("demo_report_section", DemoReportSection(),
                                package_name="arc-demo")

    ctx_on = SimpleNamespace(session_id="s", iteration=0,
                             memory={"component_registry": reg})
    assert "demo_domain" in assemble_report(ctx_on)["sections"]

    ctx_off = SimpleNamespace(
        session_id="s", iteration=0,
        memory={"component_registry": reg, "packages": {"disabled": ["arc-demo"]}},
    )
    assert "demo_domain" not in assemble_report(ctx_off)["sections"]


# ── Finding B: arc.runtime.audit must not depend on arc.chat ─────────────


def test_runtime_audit_has_no_chat_import():
    """Static check: the runtime audit module must not import arc.chat, so
    non-chat deployments (UI/API) don't pull chat into the runtime layer."""
    import ast
    from pathlib import Path

    src = Path(__import__("arc.runtime.audit", fromlist=["__file__"]).__file__).read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not any(m.startswith("arc.chat") for m in imported), \
        f"arc.runtime.audit must not import arc.chat; found {imported}"
