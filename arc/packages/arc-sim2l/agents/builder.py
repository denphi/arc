import json
import re

from arc.contracts.agent import AgentContract
from arc.providers.utils import strip_code_fences as _strip_fences
from arc.runtime.workflow_safety import (
    check_workflow_source_safe,
    run_simulate_with_timeout,
)
from arc.schemas.artifact import ArtifactDraft
from arc.schemas.research import ExperimentPlan


# ── sim2l.yaml template ──────────────────────────────────────────────────────

_SIM2L_YAML_TEMPLATE = """\
name: {name}
description: {description}

inputs:
{inputs_yaml}

outputs:
{outputs_yaml}
"""

# ── Fallback workflow when no LLM ────────────────────────────────────────────

_WORKFLOW_FALLBACK = '''\
"""ARC-generated Sim2L workflow."""


def simulate(**inputs):
    """
    Sim2L workflow entry point.
    Receives validated inputs, returns a dict of outputs.
    """
    input_parameter = inputs.get("input_parameter", 1.0)
    result = input_parameter * 2
    return {"result": result}
'''

# ── LLM prompt for workflow code ─────────────────────────────────────────────

_WORKFLOW_PROMPT = """\
Write a self-contained Python simulate() function for a Sim2L simulation workflow.

Objective   : {objective}
Methodology : {methodology}
Parameters  : {parameters}
Target      : {target}

Requirements:
- Define exactly one function: def simulate(**inputs) -> dict
- Read every input via inputs.get("param_name", default_value)
- Return a dict with ONLY numeric values (float or int) — no lists, no strings
- You may import from Python stdlib (math, cmath, itertools) but NOT numpy/scipy/external packages
- All output names must be valid Python identifiers (snake_case)
- Keep it under 40 lines
- CRITICAL: if a target is given (e.g. "bandgap_ev: 1.1"), the primary output key
  MUST be named exactly as the target key (e.g. "bandgap_ev"). Do not rename it
  to "Eg_total", "energy_gap", or any other variant. The approval check does an
  exact key match — a differently-named output will never be approved.

Output ONLY the Python code. No explanation, no markdown fences.
"""


_SCHEMA_PROMPT = """\
Given this Python simulate() function, extract its input and output parameter schemas.

```python
{code}
```

Return a JSON object with exactly:
{{
  "inputs": {{
    "<param_name>": {{"type": "Number", "default": <number>, "description": "<short text>"}}
  }},
  "outputs": {{
    "<output_name>": {{"type": "Number", "description": "<short text>"}}
  }}
}}

Rules:
- Only include parameters that appear in inputs.get() calls
- Only include outputs that appear in the returned dict
- Defaults must be numbers matching the code
- No extra keys
"""


# Static-analysis safety + the safe-eval globals used by the in-process probe
# of generated simulate() now live in arc.runtime.workflow_safety so that
# `multiprocessing.get_context("spawn")` can pickle the worker globals (which
# requires every callable inside them to live in an importable module — this
# package directory has a hyphen and cannot be imported via the dotted form).

def _check_code_safety(code: str) -> tuple[bool, str]:
    """Static safety check delegated to the shared workflow checker."""
    return check_workflow_source_safe(code)


def _run_simulate_with_timeout(code: str, calls: list[dict]) -> dict:
    """Run generated simulate() in a spawned subprocess.

    Delegates to ``arc.runtime.workflow_safety.run_simulate_with_timeout``
    which uses ``multiprocessing.get_context("spawn")`` (fork is unsafe on
    macOS and being phased out). The default allow-list (BUILDER_ALLOWED_IMPORTS)
    matches the static-safety check used by the builder agent.
    """
    return run_simulate_with_timeout(code, calls)


def _validate_simulate(code: str) -> tuple[bool, str]:
    """Run simulate() with defaults; return (ok, reason).

    Fails if: syntax error, simulate() not defined, or returns {} / non-dict.
    """
    ok, reason = _check_code_safety(code)
    if not ok:
        return False, reason

    inputs_spec = _parse_inputs_from_source(code)
    calls = [{}]
    if inputs_spec:
        calls.append({k: v["default"] for k, v in inputs_spec.items()})
        calls.append({k: v["default"] * 2 if v["default"] != 0 else 1.0
                      for k, v in inputs_spec.items()})

    result = _run_simulate_with_timeout(code, calls)
    return (True, "ok") if result.get("ok") else (False, result.get("reason", "invalid simulate()"))


def _probe_simulate(code: str) -> tuple[dict, dict] | None:
    """Execute simulate() to discover real output keys and input defaults.

    Tries three call strategies to handle functions that return {} on trivial inputs
    (e.g. optimizers that return empty when no solution found at defaults).
    Returns (inputs_spec, outputs_spec) with exact key names from the function.
    """
    try:
        ok, _reason = _check_code_safety(code)
        if not ok:
            return None

        inputs_spec = _parse_inputs_from_source(code)

        # Try calling with defaults, then with some perturbed values, to get a non-empty result.
        result = None
        calls = [
            {},  # all defaults
            {k: v["default"] for k, v in inputs_spec.items()} if inputs_spec else {},
        ]
        # Also try widening numeric defaults by 2x to avoid edge-case empty returns.
        if inputs_spec:
            calls.append({k: v["default"] * 2 if v["default"] != 0 else 1.0
                          for k, v in inputs_spec.items()})

        probe = _run_simulate_with_timeout(code, calls)
        if probe.get("ok"):
            result = probe.get("result")

        if not result:
            return None

        outputs_spec = {
            k: {"type": "Number", "description": k}
            for k, v in result.items()
            if isinstance(v, (int, float))
        }
        if not outputs_spec:
            return None

        return inputs_spec or None, outputs_spec
    except Exception:
        return None


def _parse_inputs_from_source(code: str) -> dict:
    """Parse inputs.get('name', default) calls to build input spec."""
    inputs_spec = {}
    for m in re.finditer(
        r'inputs\.get\(\s*["\'](\w+)["\']\s*,\s*([0-9]+\.?[0-9]*(?:e[+-]?[0-9]+)?)\s*\)',
        code,
    ):
        name = m.group(1)
        try:
            default = float(m.group(2))
        except ValueError:
            default = 1.0
        inputs_spec[name] = {"type": "Number", "default": default, "description": name}
    return inputs_spec


def _parse_outputs_from_source(code: str) -> dict:
    """Parse output key names from the return dict literal in the source.

    Handles both:
      return {"key": value, ...}
      return {'key': value, ...}
    This is the ground-truth for key names — no LLM hallucination, no case mismatch.
    """
    outputs_spec = {}
    # Find the last return statement with a dict literal.
    for m in re.finditer(r'return\s*\{([^}]+)\}', code, re.DOTALL):
        body = m.group(1)
        for km in re.finditer(r'["\'](\w+)["\']\s*:', body):
            key = km.group(1)
            outputs_spec[key] = {"type": "Number", "description": key}
    return outputs_spec


async def _extract_schema(provider, code: str) -> tuple[dict, dict]:
    """Get ground-truth schema by static analysis + execution, with LLM fallback."""
    # Strategy 1: static parsing — works even when function has bugs/returns {}
    inputs_spec = _parse_inputs_from_source(code)
    outputs_spec = _parse_outputs_from_source(code)

    if inputs_spec and outputs_spec:
        return inputs_spec, outputs_spec

    # Strategy 2: execute the function and observe real output keys.
    probed = _probe_simulate(code)
    if probed and probed[0] and probed[1]:
        return probed

    # Strategy 3: LLM extraction (least reliable — only for inputs/outputs names).
    try:
        prompt = _SCHEMA_PROMPT.format(code=code)
        raw = await provider.complete(prompt)
        raw = _strip_fences(raw)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            llm_inputs = {
                k: {"type": "Number", "default": v.get("default", 1.0), "description": v.get("description", k)}
                for k, v in parsed.get("inputs", {}).items()
            }
            llm_outputs = {
                k: {"type": "Number", "description": v.get("description", k)}
                for k, v in parsed.get("outputs", {}).items()
            }
            if llm_inputs and llm_outputs:
                return llm_inputs, llm_outputs
    except Exception:
        pass
    return _default_inputs({}), _default_outputs()


def _build_yaml_section(fields: dict) -> str:
    lines = []
    for name, meta in fields.items():
        lines.append(f"  {name}:")
        lines.append(f"    type: {meta.get('type', 'Number')}")
        if "default" in meta:
            lines.append(f"    default: {meta['default']}")
        if "description" in meta:
            lines.append(f"    description: {meta['description']}")
    return "\n".join(lines)


def _default_inputs(parameters: dict) -> dict:
    return {
        k: {"type": "Number", "default": v, "description": f"Input parameter {k}"}
        for k, v in parameters.items()
        if isinstance(v, (int, float))
    } or {"input_parameter": {"type": "Number", "default": 1.0}}


def _default_outputs() -> dict:
    return {"result": {"type": "Number", "description": "Computed result"}}


def _artifact_description(plan: ExperimentPlan, inputs_spec: dict, outputs_spec: dict) -> str:
    objective = " ".join(plan.proposal.objective.split())
    methodology = " ".join(plan.proposal.methodology.split())
    inputs = list((inputs_spec or {}).keys())
    outputs = list((outputs_spec or {}).keys())

    parts = [objective[:180]]
    if inputs:
        parts.append("Inputs: " + ", ".join(inputs[:6]))
    if outputs:
        parts.append("Outputs: " + ", ".join(outputs[:8]))
    elif methodology:
        parts.append(methodology[:120])
    return " | ".join(part for part in parts if part)[:500]


class Sim2LBuilderAgent(AgentContract):
    name = "builder"
    description = "Creates a Sim2L artifact with a real simulate() workflow function."

    async def run(self, input_data: ExperimentPlan) -> ArtifactDraft:
        plan = (
            input_data
            if isinstance(input_data, ExperimentPlan)
            else ExperimentPlan(**input_data)
        )

        # Short slug: first 4 meaningful words of the objective, max 40 chars.
        words = re.sub(r"[^a-z0-9 ]", " ", plan.proposal.objective.lower()).split()
        stop = {"a", "an", "the", "of", "to", "for", "and", "or", "in", "at", "by", "via"}
        slug_words = [w for w in words if w not in stop][:4]
        artifact_name = "_".join(slug_words)[:40] or "sim2l_artifact"
        target = getattr(plan.proposal, "target", {}) or {}

        # ── Generate workflow code ─────────────────────────────────────────
        provider = self.context.memory.get("provider")

        # Pull canonical output names from the schema registry so the LLM reuses them.
        schema_registry: dict = self.context.memory.get("schema_registry", {})
        canonical_hint = ""
        if schema_registry:
            canonical_hint = (
                "\nCanonical output key names already in use (you MUST reuse these exact names):\n"
                + "\n".join(f"  - {k}" for k in schema_registry)
                + "\n"
            )

        # Hard output key constraint confirmed by the user during plan review.
        required_outputs: list = self.context.memory.get("required_outputs", [])
        if not required_outputs and target:
            required_outputs = list(target.keys())
        required_hint = ""
        if required_outputs:
            required_hint = (
                "\nREQUIRED OUTPUT KEYS — the return dict MUST contain EXACTLY these keys "
                "(user-confirmed, non-negotiable):\n"
                + "\n".join(f"  - {k}" for k in required_outputs)
                + "\nDo not rename them, do not add extra keys not in this list.\n"
            )

        if provider:
            prompt = _WORKFLOW_PROMPT.format(
                objective=plan.proposal.objective[:300],
                methodology=plan.proposal.methodology[:300],
                parameters=plan.parameters,
                target=target or "none specified",
            ) + required_hint + canonical_hint
            try:
                code = await provider.complete(prompt)
                code = _strip_fences(code)
                if "def simulate" not in code:
                    code = _WORKFLOW_FALLBACK
                else:
                    valid, reason = _validate_simulate(code)
                    if not valid:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Generated simulate() failed pre-validation (%s) — using fallback", reason
                        )
                        code = _WORKFLOW_FALLBACK
            except Exception:
                code = _WORKFLOW_FALLBACK

            # Ask the LLM to extract the schema from the generated code.
            inputs_spec, outputs_spec = await _extract_schema(provider, code)
        else:
            code = _WORKFLOW_FALLBACK
            inputs_spec = _default_inputs(plan.parameters)
            outputs_spec = _default_outputs()

        sim2l_yaml = _SIM2L_YAML_TEMPLATE.format(
            name=artifact_name,
            description=plan.proposal.objective[:200],
            inputs_yaml=_build_yaml_section(inputs_spec),
            outputs_yaml=_build_yaml_section(outputs_spec),
        )
        description = _artifact_description(plan, inputs_spec, outputs_spec)

        return ArtifactDraft(
            name=artifact_name,
            description=description,
            files={
                "workflow.py": code,
                "sim2l.yaml": sim2l_yaml,
            },
            metadata={
                "created_by": self.name,
                "description": description,
                "strategy": "create_new_sim2l",
                "hypothesis": plan.proposal.hypothesis[:200],
                "methodology": plan.proposal.methodology[:500],
                "success_criteria": plan.success_criteria,
                "parameter_constraints": plan.parameter_constraints,
                "experimental_design": plan.experimental_design,
                "sim2l_inputs": inputs_spec,
                "sim2l_outputs": outputs_spec,
            },
        )
