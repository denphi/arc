"""Concrete pipeline phases.

This file holds Phase classes wrapping the post-build stages of the
research pipeline (validation → execution → review → reflection →
provenance). The pre-build stages (ideation, catalog reuse, planning,
build, curation, sim2l registration) live in ``arc.chat.loop.run_research``
for now — they have intertwined conditional flow that needs deeper
refactoring before they fit the Phase protocol cleanly.

A follow-up PR will split those too. The Phase abstraction is already
useful for the post-build sequence because that's where most of the
hook subscribers (auto-save, snapshot, JSON event sink) want to attach.
"""

from __future__ import annotations

import textwrap
from typing import Any

from arc.chat.research.pipeline import Phase, PipelineState
from arc.chat.ui import (
    BOLD, CYAN, DIM, GREEN, RED, YELLOW,
    c, err, header, ok, step, warn,
)
from arc.runtime.backend import safe_backend_action


async def _dispatch_audit(state: PipelineState, phase: str, **fields) -> None:
    """Fire a package audit phase from the chat path (item 7).

    Defensive: ``state.workflow`` is duck-typed (test stubs may lack an
    ``audit`` dispatcher) and audit failures must never break the chat loop.
    """
    dispatcher = getattr(state.workflow, "audit", None)
    if dispatcher is None or not getattr(dispatcher, "has_actions", lambda: False)():
        return
    from arc.runtime.audit import AuditBlockedError
    try:
        await dispatcher.dispatch(phase, **fields)
    except AuditBlockedError:
        # A blocking audit failure intentionally aborts the run.
        raise
    except Exception:  # noqa: BLE001 — non-blocking audit must never break the loop
        pass


# ── Validation ────────────────────────────────────────────────────────────


class ValidationPhase:
    """Run ``adapter.validate_artifact`` on the current artifact.

    Aborts the pipeline on validation failure (later phases would be
    pointless without a valid artifact).
    """

    name = "validation"

    def should_run(self, state: PipelineState) -> bool:
        return state.artifact is not None and state.is_new_artifact

    async def run(self, state: PipelineState) -> PipelineState:
        header("Validation")
        artifact_id = getattr(state.artifact, "artifact_id", None)
        await _dispatch_audit(state, "validation.before", artifact_id=artifact_id)
        validation = await state.workflow.adapter.validate_artifact(state.artifact)
        if validation.valid:
            ok("Artifact is valid")
        else:
            for e in validation.errors:
                err(e)
            for w in validation.warnings:
                warn(w)
            print()
            state.aborted = True
        await _dispatch_audit(
            state, "validation.after",
            artifact_id=artifact_id,
            output_summary={"valid": validation.valid, "errors": validation.errors},
        )
        return state


# ── Execution ─────────────────────────────────────────────────────────────


class ExecutionPhase:
    """Prepare inputs, run the adapter, record outputs.

    Reads ``state.plan.parameters`` for new-artifact runs; honours
    ``state.extras["run_inputs"]`` (set by the pre-build leg of
    run_research) when reusing an artifact.
    """

    name = "execution"

    def should_run(self, state: PipelineState) -> bool:
        return state.artifact is not None and not state.aborted

    async def run(self, state: PipelineState) -> PipelineState:
        workflow = state.workflow
        adapter_name = type(workflow.adapter).__name__
        header(f"Execution  {c(f'[{adapter_name}]', DIM)}")

        # Choose inputs: explicit override > plan parameters > {}
        run_inputs = state.extras.get("run_inputs")
        if run_inputs is None:
            run_inputs = (state.plan.parameters
                          if state.plan and state.is_new_artifact else {})

        inputs = await workflow.adapter.prepare_inputs(state.artifact, run_inputs)
        step("Inputs", inputs)
        await _dispatch_audit(
            state, "execution.before",
            artifact_id=getattr(state.artifact, "artifact_id", None),
            input_summary=inputs,
        )
        execution = await workflow.adapter.run(state.artifact, inputs)
        workflow.results.save(execution)
        _memory_hooks = getattr(workflow, "memory_hooks", None)
        if _memory_hooks is not None:
            _memory_hooks.on_result_saved(state.artifact, execution, inputs)
        await _dispatch_audit(
            state, "execution.after",
            artifact_id=getattr(state.artifact, "artifact_id", None),
            run_id=execution.run_id,
            output_summary={"status": execution.status, "outputs": execution.outputs},
        )
        step("Run ID",  execution.run_id[:8] + "...")
        step("Status",  c(execution.status,
                          GREEN if execution.status == "completed" else RED))
        step("Outputs", execution.outputs)
        for log_line in execution.logs:
            print(f"    {c(log_line, DIM)}")

        state.execution = execution

        backend = getattr(workflow, "backend", None)
        if backend is not None:
            state.extras["backend_persist"] = await safe_backend_action(
                backend, "persist_result", state.artifact, execution, inputs,
            )
            state.extras["backend_record"] = await safe_backend_action(
                backend, "record_execution", state.artifact, execution, inputs, execution.outputs,
            )

        # Post-execution validation (separate from adapter.validate_artifact,
        # which only checks the artifact's schema/imports before the run).
        # The resolved Validator strategy grades the numerical outputs
        # against domain-specific physical ranges. Errors and warnings
        # are surfaced inline; the report lands on state.extras so the
        # reviewer can read it on the next pipeline step.
        if execution.outputs:
            try:
                from arc.packages import resolve_role
                ValidatorCls = resolve_role("validator", state.workflow)
                validator = ValidatorCls(context=state.workflow._context)
                report = await validator.validate(
                    execution.outputs, target=state.target or None,
                )
                state.extras["validator_report"] = report
                if report.errors:
                    for msg in report.errors:
                        err(f"validator: {msg}")
                if report.warnings:
                    for msg in report.warnings:
                        warn(f"validator: {msg}")
                if report.evaluations:
                    # One compact line per evaluator so the user can see
                    # the per-quantity verdict without scrolling.
                    pretty_names = {
                        "BandgapEvaluator": "band_gap",
                        "FormationEnergyEvaluator": "formation_energy",
                        "StructureStabilityEvaluator": "stability",
                        "PropertyPredictionEvaluator": "property",
                    }
                    for name, verdict in report.evaluations.items():
                        label = pretty_names.get(name, name)
                        val = verdict.get("value", "?")
                        unit = verdict.get("unit") or ""
                        ok_mark = c("✓", GREEN) if verdict.get("passed") else c("✗", RED)
                        extra = ""
                        cls = verdict.get("classification") or verdict.get("stable")
                        if cls is not None:
                            extra = f"  ({cls})" if cls is not True and cls is not False \
                                    else f"  ({'stable' if cls else 'unstable'})"
                        print(f"    {ok_mark}  {label}={val}{unit}{extra}")
            except Exception as exc:  # noqa: BLE001 — validation is best-effort
                import logging as _logging
                _logging.getLogger(__name__).debug(
                    "Validator step failed (continuing): %s", exc,
                )

        # Distance-to-target diagnostic (kept here because it depends on
        # the freshly-computed outputs).
        if state.target and execution.outputs:
            from arc.packages import load_reviewer
            _keys_match = load_reviewer()._keys_match
            diffs = []
            unmatched_targets = []
            for tk, tv in state.target.items():
                matched = False
                for ok_name, ov in execution.outputs.items():
                    if _keys_match(tk, ok_name):
                        matched = True
                        # ``bool`` is an int subclass — exclude it so a
                        # boolean output (e.g. converged) isn't treated as a
                        # number. Guard the arithmetic too: the target value
                        # may be a string the user typed.
                        if (
                            isinstance(ov, (int, float)) and not isinstance(ov, bool)
                            and isinstance(tv, (int, float)) and not isinstance(tv, bool)
                        ):
                            pct = abs(ov - tv) / max(abs(tv), 1e-12) * 100
                            diffs.append(
                                f"{ok_name}={ov:.4g}  target={tv}  ({pct:.1f}% off)"
                            )
                if not matched:
                    unmatched_targets.append(tk)
            if diffs:
                step("vs target", "")
                for d in diffs:
                    print(f"    {c(d, YELLOW)}")
            if unmatched_targets:
                state.unmatched_target_keys = list(unmatched_targets)
                warn(
                    f"Target key(s) {unmatched_targets} did not match any output "
                    f"{list(execution.outputs.keys())}"
                )
                warn("Schema mismatch — the chat will request an artifact "
                     "rebuild that produces these keys.")
                # Emit a structured event so non-terminal consumers (the
                # JSONL sink, a future TUI) can react too.
                from arc.chat.events import emit
                emit(
                    "warn",
                    f"schema_mismatch: targets {list(unmatched_targets)} not in "
                    f"outputs {list(execution.outputs.keys())}",
                    targets=list(unmatched_targets),
                    outputs=list(execution.outputs.keys()),
                    artifact_id=getattr(state.artifact, "artifact_id", None),
                )

        return state


# ── Review ────────────────────────────────────────────────────────────────


class ReviewPhase:
    """Run the reviewer agent. Stores its decision on ``state.review``."""

    name = "review"

    def should_run(self, state: PipelineState) -> bool:
        return state.execution is not None and not state.aborted

    async def run(self, state: PipelineState) -> PipelineState:
        from arc.packages import resolve_role

        ctx = state.workflow._context
        ReviewerAgent = resolve_role("reviewer", state.workflow)

        header("Review")
        print(f"  {c('evaluating...', DIM)}", end="\r")
        review = await ReviewerAgent(context=ctx).run(state.execution)
        print(" " * 40, end="\r")
        approved_str = (
            c("approved ✓", GREEN, BOLD)
            if review.approved
            else c("not approved ✗", YELLOW, BOLD)
        )
        step("Decision", approved_str)
        step("Summary", textwrap.fill(review.summary, 60, subsequent_indent=" " * 16))
        if review.strengths:
            step("Strengths", "")
            for s in review.strengths:
                print(f"    {c('+', GREEN)} {s}")
        if review.weaknesses:
            step("Weaknesses", "")
            for w in review.weaknesses:
                print(f"    {c('-', YELLOW)} {w}")
        if review.recommendations:
            step("Next steps", "")
            for r in review.recommendations:
                print(f"    {c('→', CYAN)} {r}")
        if review.next_parameters:
            step("Next params", review.next_parameters)

        _memory_hooks = getattr(state.workflow, "memory_hooks", None)
        if _memory_hooks is not None:
            run_id = getattr(state.execution, "run_id", None)
            _memory_hooks.on_review_completed(state.artifact, review, run_id)

        await _dispatch_audit(
            state, "review.after",
            artifact_id=getattr(state.artifact, "artifact_id", None),
            run_id=getattr(state.execution, "run_id", None),
            output_summary={"approved": review.approved, "summary": review.summary},
        )

        state.review = review
        return state


# ── Reflection ────────────────────────────────────────────────────────────


class ReflectionPhase:
    """Run the reflector to extract lessons from the review + execution."""

    name = "reflection"

    def should_run(self, state: PipelineState) -> bool:
        return state.review is not None and not state.aborted

    async def run(self, state: PipelineState) -> PipelineState:
        from arc.packages import resolve_role
        ctx = state.workflow._context
        Reflector = resolve_role("reflector", state.workflow)
        lessons = await Reflector(context=ctx).run(
            state.review, execution=state.execution
        )
        state.reflection = lessons
        await _dispatch_audit(
            state, "reflection.after",
            artifact_id=getattr(state.artifact, "artifact_id", None),
            run_id=getattr(state.execution, "run_id", None),
        )
        return state


# ── Provenance ────────────────────────────────────────────────────────────


class ProvenancePhase:
    """Record the run in the provenance log + tick the iteration counter."""

    name = "provenance"

    def should_run(self, state: PipelineState) -> bool:
        return (state.execution is not None
                and state.review is not None
                and state.artifact is not None
                and not state.aborted)

    async def run(self, state: PipelineState) -> PipelineState:
        workflow = state.workflow
        ctx = workflow._context
        from arc.schemas.research import ResearchGoal
        goal = ResearchGoal(
            goal=state.goal_text,
            domain=state.domain or "computational science",
            target=state.target,
        )
        workflow.provenance.record(
            session_id=ctx.session_id,
            action="chat_run",
            agent="orchestrator",
            artifact_id=state.artifact.artifact_id,
            run_id=state.execution.run_id,
            inputs=goal.model_dump(),
            outputs=state.review.model_dump(),
        )
        ctx.iteration += 1
        await _dispatch_audit(
            state, "iteration.after",
            artifact_id=state.artifact.artifact_id,
            run_id=state.execution.run_id,
            output_summary={"approved": state.review.approved},
        )
        return state


# ── Public catalogue ──────────────────────────────────────────────────────


POST_BUILD_PHASES: list[Phase] = [
    ValidationPhase(),
    ExecutionPhase(),
    ReviewPhase(),
    ReflectionPhase(),
    ProvenancePhase(),
]
