from arc.contracts.agent import AgentContract
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


import re as _re

# Each entry is a frozenset of tokens that all name the *same physical quantity*.
# A match requires BOTH keys to share tokens from the SAME group.
# Units (eV, nm, etc.) are intentionally excluded — they appear in many
# unrelated quantities and would cause false matches.
_PHYSICS_SYNONYMS: list[frozenset] = [
    frozenset({"bandgap", "band_gap", "bg", "gap"}),  # "eg" removed — too short/ambiguous
    frozenset({"strain", "eps", "epsilon", "deformation"}),
    frozenset({"temperature", "temp", "kelvin"}),
    frozenset({"effective_mass", "effectivemass", "ema"}),
    frozenset({"doping", "dopant", "carrier", "concentration"}),
    frozenset({"pressure", "stress"}),
    frozenset({"mobility", "mu"}),
    frozenset({"wavelength", "lambda"}),
    frozenset({"frequency", "freq"}),
]

# Short tokens to ignore during matching (units, articles, common suffixes).
_STOP_TOKENS: frozenset = frozenset({
    "ev", "mev", "gpa", "mpa", "nm", "pm", "cm", "k", "hz", "ghz", "thz",
    "per", "total", "avg", "mean", "min", "max", "value", "val",
    "the", "a", "an", "of", "in",
})


def _key_tokens(key: str) -> frozenset:
    """Normalised, stop-word-free token set for a key."""
    raw = _re.sub(r"[0-9]", "", key.lower())
    parts = frozenset(_re.split(r"[_\-\s]+", raw)) - {""} - _STOP_TOKENS
    return parts


def _keys_match(tk: str, ok: str) -> bool:
    """Return True when target key and output key name the same physical quantity.

    Tiers (in order):
    1. Flat exact match (case-insensitive, underscores ignored).
    2. One flat form is a substring of the other AND both are at least 5 chars
       (avoids spurious single-token matches like 'ev' in 'sensitivity_ev_per_nm').
    3. Both keys share a token from the same physics-synonym group.
    """
    tk_flat = tk.replace("_", "").lower()
    ok_flat = ok.replace("_", "").lower()

    # Tier 1
    if tk_flat == ok_flat:
        return True

    # Tier 2 — require meaningful length to avoid unit-suffix false positives
    min_len = 5
    if len(tk_flat) >= min_len and len(ok_flat) >= min_len:
        if tk_flat in ok_flat or ok_flat in tk_flat:
            return True

    # Tier 3 — physics synonyms (unit tokens already excluded)
    tk_toks = _key_tokens(tk)
    ok_toks = _key_tokens(ok)
    if tk_toks and ok_toks:
        for group in _PHYSICS_SYNONYMS:
            if tk_toks & group and ok_toks & group:
                return True

    return False


def _registry_keys_match(tk: str, ok: str, registry: dict) -> bool:
    """Return True if tk and ok both resolve to the same canonical key via the registry."""
    if not registry:
        return False
    tk_flat = tk.replace("_", "").lower()
    ok_flat = ok.replace("_", "").lower()
    for canon, entry in registry.items():
        canon_flat = canon.replace("_", "").lower()
        aliases_flat = [a.replace("_", "").lower() for a in entry.get("aliases", [])]
        tk_in = tk_flat in [canon_flat] + aliases_flat
        ok_in = ok_flat in [canon_flat] + aliases_flat
        if tk_in and ok_in:
            return True
    return False


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

        # ── Hard deterministic verdict ────────────────────────────────────
        if result.status != "completed" or not outputs:
            approved = False
            target_errors: dict[str, float] = {}
            iteration_complete = False
        elif target:
            approved, target_errors = _check_target(outputs, target, registry=registry)
            iteration_complete = approved
        else:
            # No target: treat a completed, non-empty execution as a successful iteration.
            approved = True
            target_errors = {}
            iteration_complete = True

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
                # If approved, force strategy to "stop"
                if approved:
                    llm_review.strategy = "stop"
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
            summary = "Execution completed with non-empty outputs"

        return ReviewResult(
            approved=approved,
            summary=summary,
            strengths=["Execution completed"] if result.status == "completed" else [],
            weaknesses=(
                [f"{k}: {v:.1f}% off" for k, v in target_errors.items() if v > APPROVAL_THRESHOLD * 100]
                if target_errors else (["No target output match"] if target else [])
            ),
            recommendations=["Adjust parameters to reduce target error"] if not approved else [],
            next_parameters={},
            iteration_complete=iteration_complete,
            strategy="stop" if approved else ("step" if target else "stop"),
        )
