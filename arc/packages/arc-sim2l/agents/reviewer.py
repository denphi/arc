from __future__ import annotations

from arc.contracts.agent import AgentContract
from arc.runtime.key_matching import (
    fuzzy_keys_match as _keys_match,
    registry_keys_match as _registry_keys_match,
)
from arc.schemas.execution import ExecutionResult
from arc.schemas.review import ReviewResult

# Approval threshold: outputs must be within this fraction of every target value.
APPROVAL_THRESHOLD = 0.05  # 5%

_REVIEW_PROMPT = """\
You are a scientific reviewer evaluating a simulation experiment.
The approval verdict has already been computed by code — do NOT change it.

Execution status : {status}
Inputs used      : {inputs}
Outputs          : {outputs}
Target           : {target}
Approved         : {approved}  ← FIXED, do not override
Target errors    : {target_errors}
Prior runs       : {history}

Tasks:
1. Write a brief summary explaining the result and the target errors shown above.
2. Suggest concrete next input parameters ("next_parameters") that would reduce the largest errors, or explore the most informative region if no target.
3. Choose a continuation strategy:
   - "step": you have a clear next set of parameters to try (fill next_parameters)
   - "explore": the search space needs broader exploration — recommend genetic algorithm
   - "stop": approved=true, or no further improvement is physically possible

Return a JSON object with exactly these fields:
- "approved": {approved}  ← copy this value exactly, do not change it
- "summary": one sentence (under 100 chars)
- "strengths": list of up to 3 short strings
- "weaknesses": list of up to 3 short strings
- "recommendations": list of up to 3 short strings
- "next_parameters": dict mapping ONLY simulation input parameter names (from "Inputs used" above) to suggested numeric values — no metrics like duration_seconds or squid_id
- "iteration_complete": {iteration_complete}  ← copy this value exactly
- "strategy": one of "step", "explore", or "stop"

Keep all string values concise. No extra keys. next_parameters keys must appear in the "Inputs used" list.
"""


def _check_target(
    outputs: dict,
    target: dict,
    threshold: float = APPROVAL_THRESHOLD,
    registry: dict | None = None,
) -> tuple[bool, dict[str, float]]:
    """Deterministically compute approval and per-key % errors.

    Returns (approved, {matched_output_key: pct_error}).
    approved=True only when ALL matched target keys are within threshold.
    If no target keys match any output, returns (False, {}).
    """
    import logging
    _log = logging.getLogger(__name__)

    errors: dict[str, float] = {}
    for tk, tv in target.items():
        matched_any = False
        for ok, ov in outputs.items():
            # Tier 0: schema registry canonical lookup (most reliable); fall back to fuzzy.
            if _registry_keys_match(tk, ok, registry or {}) or _keys_match(tk, ok):
                matched_any = True
                if isinstance(ov, (int, float)) and ov is not None:
                    pct = abs(ov - tv) / max(abs(tv), 1e-12)
                    errors[ok] = round(pct * 100, 2)
                break
        if not matched_any:
            _log.warning(
                "Target key %r did not match any output key %s — "
                "approval will fail. Add a synonym or rename the output.",
                tk, list(outputs.keys()),
            )

    if not errors:
        return False, {}

    approved = all(e <= threshold * 100 for e in errors.values())
    return approved, errors


def _build_context_acceptance(
    outputs: dict,
    contexts: list[dict],
    target: dict,
    registry: dict | None = None,
) -> tuple[bool | None, dict[str, float | str]]:
    """Evaluate generic build-context acceptance criteria.

    The schema is intentionally permissive for package authors:
    ``acceptance.metrics`` requires matching output keys; ``tolerance`` is
    applied when a reference value is available from ``reference_values``,
    ``targets``, or the normal research target.
    """
    checks: dict[str, float | str] = {}
    saw_criteria = False
    ok = True
    for context in contexts or []:
        acceptance = (context or {}).get("acceptance") or {}
        if not isinstance(acceptance, dict):
            continue
        metrics = acceptance.get("metrics") or acceptance.get("required_outputs") or []
        if isinstance(metrics, str):
            metrics = [metrics]
        if not isinstance(metrics, list):
            metrics = []
        tolerances = acceptance.get("tolerance") or {}
        if isinstance(tolerances, (int, float)):
            tolerances = {str(metric): tolerances for metric in metrics}
        if not isinstance(tolerances, dict):
            tolerances = {}
        references = (
            acceptance.get("reference_values")
            or acceptance.get("targets")
            or acceptance.get("expected")
            or {}
        )
        if not isinstance(references, dict):
            references = {}
        references = {**target, **references}

        for metric in metrics:
            metric = str(metric)
            saw_criteria = True
            matched_key = None
            for key in outputs:
                if _registry_keys_match(metric, key, registry or {}) or _keys_match(metric, key):
                    matched_key = key
                    break
            if matched_key is None:
                checks[metric] = "missing output"
                ok = False
                continue
            ref = references.get(metric, references.get(matched_key))
            tol = tolerances.get(metric, tolerances.get(matched_key))
            value = outputs.get(matched_key)
            if ref is None or tol is None:
                checks[matched_key] = "present"
                continue
            if not isinstance(value, (int, float)) or not isinstance(ref, (int, float)):
                checks[matched_key] = "non-numeric comparison"
                ok = False
                continue
            error = abs(value - ref) / max(abs(ref), 1e-12)
            checks[matched_key] = round(error * 100, 2)
            if error > float(tol):
                ok = False

    if not saw_criteria:
        return None, {}
    return ok, checks


class ReviewerAgent(AgentContract):
    name = "reviewer"
    description = "Evaluates execution results and provides structured feedback."

    async def run(self, input_data: ExecutionResult) -> ReviewResult:
        result = (
            input_data
            if isinstance(input_data, ExecutionResult)
            else ExecutionResult(**input_data)
        )

        target = self.context.memory.get("target", {})
        outputs = result.outputs or {}
        registry = self.context.memory.get("schema_registry", {})
        build_contexts = self.context.memory.get("build_contexts", []) or []

        # ── Hard deterministic verdict ────────────────────────────────────
        if result.status != "completed" or not outputs:
            approved = False
            target_errors: dict[str, float] = {}
            context_approved = None
            context_errors: dict[str, float | str] = {}
            iteration_complete = False
        elif target:
            approved, target_errors = _check_target(outputs, target, registry=registry)
            context_approved, context_errors = _build_context_acceptance(
                outputs, build_contexts, target, registry=registry,
            )
            if context_approved is not None:
                approved = approved and context_approved
            iteration_complete = approved
        else:
            target_errors = {}
            context_approved, context_errors = _build_context_acceptance(
                outputs, build_contexts, target, registry=registry,
            )
            # A context with explicit acceptance criteria can serve as the
            # goal-achievement condition. Without it, keep the loop alive.
            approved = bool(context_approved) if context_approved is not None else False
            iteration_complete = approved

        if result.status != "completed":
            failure_classification = "runtime_failure"
        elif approved:
            failure_classification = "none"
        elif context_errors:
            failure_classification = "acceptance_failure"
        elif target_errors or target:
            failure_classification = "implementation_failure"
        else:
            failure_classification = "implementation_failure"

        provider = self.context.memory.get("provider")
        if provider:
            history = self.context.memory.get("run_history", [])
            history_summary = [
                {"inputs": r.get("inputs", {}), "outputs": r.get("outputs", {})}
                for r in history[-5:]
            ]
            errors_display = (
                {k: f"{v:.1f}%" for k, v in target_errors.items()}
                if target_errors else "no target matches found"
            )
            if context_errors:
                errors_display = {
                    "target": errors_display,
                    "build_context_acceptance": context_errors,
                }
            # Send only actual simulation inputs — strip metrics noise
            schema_keys: set = set(
                self.context.memory.get("current_artifact", None)
                and self.context.memory["current_artifact"].metadata.get("sim2l_inputs", {}).keys()
                or []
            )
            _NON_INPUT = {"duration_seconds", "squid_id", "execution_id", "status", "cache_hit"}
            if schema_keys:
                sim_inputs = {k: v for k, v in (result.metrics or {}).items() if k in schema_keys}
            else:
                sim_inputs = {k: v for k, v in (result.metrics or {}).items()
                              if k not in _NON_INPUT and isinstance(v, (int, float))}
            prompt = _REVIEW_PROMPT.format(
                status=result.status,
                inputs=sim_inputs,
                outputs=outputs,
                target=target or "none specified",
                approved=approved,
                target_errors=errors_display,
                iteration_complete=iteration_complete,
                history=history_summary or "none",
            )
            try:
                llm_review = await provider.complete_structured(prompt, ReviewResult)
                # Override the LLM's verdict with our computed one
                llm_review.approved = approved
                llm_review.iteration_complete = iteration_complete
                llm_review.failure_classification = failure_classification
                # If approved, force strategy to "stop"; otherwise never let a
                # no-target review stop the loop as if the goal were achieved.
                if approved:
                    llm_review.strategy = "stop"
                elif not target and llm_review.strategy == "stop":
                    llm_review.strategy = "explore"
                return llm_review
            except Exception:
                pass

        # Stub fallback
        if approved:
            summary = (
                "Target met: " +
                ", ".join(f"{k} within {v:.1f}%" for k, v in target_errors.items())
            )[:100]
        elif target_errors:
            worst = max(target_errors, key=lambda k: target_errors[k])
            summary = f"Not yet: {worst} is {target_errors[worst]:.1f}% off target"
        elif target:
            summary = "No output matched target keys — check parameter names"
        else:
            summary = "Execution completed but no target was specified"

        return ReviewResult(
            approved=approved,
            summary=summary,
            strengths=["Execution completed"] if result.status == "completed" else [],
            weaknesses=(
                [f"{k}: {v:.1f}% off" for k, v in target_errors.items() if v > APPROVAL_THRESHOLD * 100]
                if target_errors else (["No target output match"] if target else ["No target specified"])
            ),
            recommendations=(
                ["Adjust parameters to reduce target error"]
                if target else ["Define a numeric target before approving the goal"]
            ) if not approved else [],
            next_parameters={},
            iteration_complete=iteration_complete,
            strategy="stop" if approved else ("step" if target else "explore"),
            failure_classification=failure_classification,
        )
