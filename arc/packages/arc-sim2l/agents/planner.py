from __future__ import annotations

from arc.contracts.agent import AgentContract
from arc.schemas.research import ExperimentPlan, ResearchProposal


_REFINE_PROMPT = """\
You are a scientific experiment planner. Revise the experiment plan below based on the user's feedback.

Current plan:
  Parameters             : {parameters}
  Parameter constraints  : {parameter_constraints}
  Parameter sweep        : {parameter_sweep}
  Experimental design    : {experimental_design}
  Success criteria       : {success_criteria}

User feedback: {feedback}

Return a revised JSON object with exactly the same fields as before:
- "proposal": (copy unchanged from the current plan)
- "artifact_strategy": "create_new_sim2l"
- "parameters": revised dict
- "parameter_sweep": revised dict
- "parameter_constraints": revised dict
- "experimental_design": revised list
- "success_criteria": revised list

Apply the feedback precisely. Keep the plan detailed and actionable. No extra keys.
"""

_PLAN_PROMPT = """\
You are a senior computational science experiment planner.

Convert the following research proposal into a concrete experiment plan.

Hypothesis  : {hypothesis}
Objective   : {objective}
Variables   : {variables}
Methodology : {methodology}
Target      : {target}

Return a JSON object with exactly these fields:
- "proposal": (copy the proposal fields below unchanged)
- "artifact_strategy": "create_new_sim2l"
- "parameters": dict mapping EVERY physically meaningful input variable to its default numeric value
- "parameter_sweep": dict mapping each input variable to 5-7 numeric values spanning the useful range
- "parameter_constraints": dict mapping each parameter name to:
  {{"min": number, "max": number, "units": string, "role": string, "rationale": string}}
- "experimental_design": list of 5-8 detailed steps explaining the run strategy, controls, and how sweeps or optimization should proceed
- "success_criteria": list of 4-6 concrete criteria, including target proximity, validation, numerical sanity, and scientific interpretability

Rules:
- Use domain-appropriate parameter names (e.g. "effective_mass", "temperature", "strain"), not generic ones like "input_parameter"
- Prefer 3-6 tunable input variables when the objective has enough degrees of freedom
- Include fixed/control variables only when they are needed to make the simulation reproducible
- Every key in "parameters" must also appear in "parameter_sweep" and "parameter_constraints"
- Sweeps must respect the corresponding min/max constraints
- All parameter values must be numbers, not strings
- Constraint rationales should explain why the range is physically plausible
- Do not add extra top-level keys.
"""


def _target_from_context(context) -> dict:
    return context.memory.get("target", {}) if context else {}


def _clean_name(value: str) -> str:
    import re

    name = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
    if not name or name[0].isdigit():
        name = f"parameter_{name or 'value'}"
    return name


def _fallback_parameter_names(proposal: ResearchProposal) -> list[str]:
    names = [_clean_name(v) for v in proposal.variables if v and _clean_name(v)]
    generic = {"x", "y", "z", "input", "input_parameter", "output", "result"}
    names = [name for name in names if name not in generic]

    text = " ".join([
        proposal.objective,
        proposal.methodology,
        proposal.expected_outcomes,
        " ".join(proposal.evaluation_metrics),
    ]).lower()

    candidates: list[str] = []
    if any(term in text for term in ("bandgap", "band gap", "semiconductor", "effective mass")):
        candidates.extend(["effective_mass", "temperature", "strain", "doping_concentration"])
    if any(term in text for term in ("temperature", "thermal", "heat")):
        candidates.append("temperature")
    if any(term in text for term in ("strain", "pressure", "stress", "lattice")):
        candidates.extend(["strain", "pressure"])
    if any(term in text for term in ("frequency", "wave", "oscillat")):
        candidates.extend(["frequency", "amplitude", "damping"])
    if any(term in text for term in ("energy", "barrier", "activation")):
        candidates.extend(["activation_energy", "temperature"])

    if not candidates:
        candidates = ["control_parameter", "temperature", "scale_factor"]

    ordered = list(dict.fromkeys(names + candidates))
    return ordered[:6]


def _default_value_for(name: str) -> float:
    defaults = {
        "effective_mass": 0.25,
        "temperature": 300.0,
        "strain": 0.0,
        "doping_concentration": 1e16,
        "pressure": 1.0,
        "frequency": 1.0,
        "amplitude": 1.0,
        "damping": 0.1,
        "activation_energy": 0.5,
        "scale_factor": 1.0,
        "control_parameter": 1.0,
    }
    return defaults.get(name, 1.0)


def _constraints_for(name: str, default: float) -> dict:
    if name == "effective_mass":
        return {"min": 0.05, "max": 2.0, "units": "electron_mass", "role": "material response", "rationale": "Covers light to heavy carrier effective masses."}
    if name == "temperature":
        return {"min": 50.0, "max": 800.0, "units": "K", "role": "environment", "rationale": "Spans cryogenic through elevated operating temperatures."}
    if name == "strain":
        return {"min": -0.05, "max": 0.05, "units": "fraction", "role": "structural perturbation", "rationale": "Keeps elastic strain within a small-deformation regime."}
    if name == "doping_concentration":
        return {"min": 1e14, "max": 1e19, "units": "cm^-3", "role": "composition/control", "rationale": "Covers dilute to heavily doped semiconductor regimes."}
    if name == "pressure":
        return {"min": 0.0, "max": 20.0, "units": "GPa", "role": "environment", "rationale": "Explores ambient to high-pressure response."}
    if name == "frequency":
        return {"min": 0.1, "max": 10.0, "units": "THz", "role": "excitation", "rationale": "Samples low to high excitation frequencies."}
    if name == "amplitude":
        return {"min": 0.0, "max": 5.0, "units": "arb.", "role": "excitation strength", "rationale": "Avoids negative amplitudes and includes strong forcing."}
    if name == "damping":
        return {"min": 0.0, "max": 1.0, "units": "dimensionless", "role": "loss parameter", "rationale": "Covers no damping through strong damping."}
    if name == "activation_energy":
        return {"min": 0.01, "max": 5.0, "units": "eV", "role": "energy scale", "rationale": "Covers small barriers through strongly activated processes."}
    # Lattice parameters from Materials Project: bound around the
    # reference value so the sweep stays inside a physically plausible
    # neighbourhood. ±10% on lengths is the standard "small-strain"
    # band; ±5° on angles keeps us in the same crystal system without
    # forcing collinearity.
    if name in ("lattice_a", "lattice_b", "lattice_c"):
        span = max(abs(default) * 0.1, 1e-3)
        return {"min": default - span, "max": default + span,
                "units": "angstrom", "role": "lattice length",
                "rationale": "±10% around the Materials Project reference length."}
    if name in ("lattice_alpha", "lattice_beta", "lattice_gamma"):
        return {"min": max(default - 5.0, 1.0), "max": min(default + 5.0, 179.0),
                "units": "degrees", "role": "lattice angle",
                "rationale": "±5° around the Materials Project reference angle."}
    if name == "lattice_volume":
        span = max(abs(default) * 0.2, 1e-3)
        return {"min": default - span, "max": default + span,
                "units": "angstrom^3", "role": "cell volume",
                "rationale": "±20% around the Materials Project reference volume."}
    # ── MP thermodynamics ─────────────────────────────────────────────
    if name == "energy_above_hull":
        # The reference is on-hull (0) or just above; allow a small
        # exploration window into clearly metastable territory.
        return {"min": max(default - 0.1, 0.0), "max": default + 0.2,
                "units": "eV/atom", "role": "thermodynamic stability margin",
                "rationale": "Probes from on-hull through clearly metastable "
                             "(≈ 0.2 eV/atom above the convex hull)."}
    if name == "decomposition_enthalpy":
        span = max(abs(default) * 0.5, 0.05)
        return {"min": default - span, "max": default + span,
                "units": "eV/atom", "role": "decomposition cost",
                "rationale": "±50% around the Materials Project reference "
                             "decomposition enthalpy."}
    # ── MP elasticity (GPa) ───────────────────────────────────────────
    if name in ("bulk_modulus", "shear_modulus"):
        span = max(abs(default) * 0.3, 1.0)
        # Moduli are non-negative; clamp the lower bound at 0.
        return {"min": max(default - span, 0.0), "max": default + span,
                "units": "GPa", "role": "elastic modulus",
                "rationale": "±30% around the Materials Project Voigt-Reuss-Hill "
                             "average; lower bound clamped at 0 GPa."}
    # ── MP electronic structure (eV) ──────────────────────────────────
    if name in ("efermi", "dos_energy_up", "dos_energy_down"):
        span = max(abs(default) * 0.1, 0.5)
        return {"min": default - span, "max": default + span,
                "units": "eV", "role": "electronic-structure energy",
                "rationale": "±10% (≥ 0.5 eV) around the Materials Project "
                             "reference Fermi level / DOS edge."}
    span = abs(default) or 1.0
    return {"min": default - 2 * span, "max": default + 2 * span, "units": "dimensionless", "role": "model input", "rationale": "Broad local range around the nominal value."}


def _sweep_for(default: float, constraint: dict) -> list[float]:
    lo = float(constraint["min"])
    hi = float(constraint["max"])
    if lo == hi:
        return [lo]
    points = [lo, (lo + default) / 2, default, (default + hi) / 2, hi]
    return [float(f"{p:.6g}") for p in points]


def _top_mp_hit(context) -> dict | None:
    """Return the top ``materials_project``-sourced hit, if any.

    Walks ``context.memory['catalog_hits']`` in catalogue order and
    picks the first one whose ``metadata.source == 'materials_project'``.
    Returns ``None`` when no MP-sourced hit is present so callers can
    short-circuit cleanly.
    """
    if context is None:
        return None
    hits = (context.memory or {}).get("catalog_hits") or []
    if not isinstance(hits, list):
        return None
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        meta = hit.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("source") == "materials_project":
            return hit
    return None


# Metadata blocks (under ``hit['metadata']``) that the planner reads
# for builder defaults. Adding a new MP axis is one line here plus
# matching ``_constraints_for`` entries.
_MP_PROPERTY_BLOCKS: tuple[tuple[str, str], ...] = (
    ("lattice",    "Lattice"),
    ("thermo",     "Thermodynamic"),
    ("elasticity", "Elastic"),
    ("dos",        "Electronic-structure"),
)


def _apply_mp_property_defaults(plan: ExperimentPlan, context) -> None:
    """Splice MP reference values from the top hit into ``plan.parameters``.

    Walks every metadata block the searcher attaches (lattice / thermo
    / elasticity / DOS) and inserts each numeric value as a builder
    default. Constraints + sweeps are minted via the existing helpers
    so downstream sweep/optimisation code never sees a parameter
    without bounds.

    Conservative on conflicts: never overwrites an existing parameter
    (the LLM's plan wins on collision), only adds new keys. Each block
    that contributes at least one new parameter logs an attribution
    line in ``plan.experimental_design`` so the user can see which MP
    record seeded which defaults.
    """
    hit = _top_mp_hit(context)
    if hit is None:
        return
    meta = hit.get("metadata") or {}
    formula = meta.get("formula") or meta.get("mp_id") or ""

    notes: list[str] = []
    for block_key, label in _MP_PROPERTY_BLOCKS:
        block = meta.get(block_key)
        if not isinstance(block, dict) or not block:
            continue
        added: list[str] = []
        for name, value in block.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if name in plan.parameters:
                continue
            plan.parameters[name] = float(value)
            plan.parameter_constraints.setdefault(
                name, _constraints_for(name, float(value)),
            )
            plan.parameter_sweep.setdefault(
                name,
                _sweep_for(float(value), plan.parameter_constraints[name]),
            )
            added.append(name)
        if added:
            notes.append(
                f"{label} defaults sourced from Materials Project {formula} "
                f"(parameters: {', '.join(added)})."
            )

    if notes:
        plan.experimental_design = list(plan.experimental_design) + notes


# Backwards-compatible alias: the chat layer and tests reference the
# original name. New code should call ``_apply_mp_property_defaults``.
_apply_mp_lattice_defaults = _apply_mp_property_defaults


def _fallback_plan(proposal: ResearchProposal) -> ExperimentPlan:
    names = _fallback_parameter_names(proposal)
    parameters = {name: _default_value_for(name) for name in names}
    constraints = {name: _constraints_for(name, value) for name, value in parameters.items()}
    sweep = {name: _sweep_for(parameters[name], constraints[name]) for name in parameters}
    target_text = ", ".join(proposal.evaluation_metrics) or "requested outputs"

    return ExperimentPlan(
        proposal=proposal,
        artifact_strategy="create_new_sim2l",
        parameters=parameters,
        parameter_sweep=sweep,
        parameter_constraints=constraints,
        experimental_design=[
            "Validate the generated artifact schema before running any simulations.",
            "Run the nominal parameter set to establish a reproducible baseline.",
            "Sweep one parameter at a time across its constrained range to estimate sensitivity.",
            "Prioritize parameters with the largest output sensitivity for follow-up optimization.",
            "Reject runs with non-finite outputs or values outside physically plausible bounds.",
            "Compare accepted outputs against the target quantities and update next parameters.",
        ],
        success_criteria=[
            "Artifact validates without schema or runtime errors.",
            "Nominal run completes and returns all required numeric outputs.",
            f"Outputs support evaluation of {target_text}.",
            "Sweeps stay within declared parameter constraints.",
            "Recommended next parameters are reproducible from run history.",
        ],
    )


class PlannerAgent(AgentContract):
    name = "planner"
    description = "Converts a research proposal into a concrete experiment plan."

    async def run(self, input_data: ResearchProposal) -> ExperimentPlan:
        proposal = (
            input_data
            if isinstance(input_data, ResearchProposal)
            else ResearchProposal(**input_data)
        )

        provider = self.context.memory.get("provider")
        if provider:
            target = _target_from_context(self.context)
            prompt = _PLAN_PROMPT.format(
                hypothesis=proposal.hypothesis[:300],
                objective=proposal.objective[:200],
                variables=proposal.variables,
                methodology=proposal.methodology[:300],
                target=target or "none specified",
            )
            if self.context.memory.get("goal_mode") == "artifact_generation":
                prompt += (
                    "\n\nArtifact/code-generation mode:\n"
                    "- Treat the task as building a requested code artifact, not "
                    "as inventing an optimization target.\n"
                    "- Parameters should be real solver/runtime inputs explicitly "
                    "supported by the request or source material; if the source "
                    "does not provide numeric inputs, keep the parameter set small "
                    "and use implementation controls such as mesh resolution only "
                    "when they are required.\n"
                    "- The experimental_design must include extracting source facts, "
                    "implementing the requested solver, validating against the paper, "
                    "and producing requested plots/results.\n"
                    "- Do not require output keys derived from instruction words "
                    "such as workflow, package, skill, or chain.\n"
                    "- Do not ask the builder for a surrogate model when the user "
                    "asked for a concrete solver implementation.\n"
                )
            try:
                plan = await provider.complete_structured(prompt, ExperimentPlan, max_tokens=2048)
                # Ensure artifact_strategy is always a short safe string.
                plan.artifact_strategy = "create_new_sim2l"
                self._complete_plan(plan)
                _apply_mp_lattice_defaults(plan, self.context)
                return plan
            except Exception:
                pass  # fall through to stub

        plan = _fallback_plan(proposal)
        _apply_mp_lattice_defaults(plan, self.context)
        return plan

    def _complete_plan(self, plan: ExperimentPlan) -> None:
        for name, value in list(plan.parameters.items()):
            if not isinstance(value, (int, float)):
                continue
            plan.parameter_constraints.setdefault(name, _constraints_for(name, float(value)))
            plan.parameter_sweep.setdefault(name, _sweep_for(float(value), plan.parameter_constraints[name]))
        if not plan.experimental_design:
            plan.experimental_design = _fallback_plan(plan.proposal).experimental_design
        if len(plan.success_criteria) < 4:
            plan.success_criteria.extend(_fallback_plan(plan.proposal).success_criteria)
            plan.success_criteria = list(dict.fromkeys(plan.success_criteria))[:6]

    async def refine(self, plan: ExperimentPlan, feedback: str) -> ExperimentPlan:
        """Revise an existing plan based on free-text user feedback."""
        provider = self.context.memory.get("provider")
        if provider:
            prompt = _REFINE_PROMPT.format(
                parameters=plan.parameters,
                parameter_sweep=plan.parameter_sweep,
                parameter_constraints=plan.parameter_constraints,
                experimental_design=plan.experimental_design,
                success_criteria=plan.success_criteria,
                feedback=feedback[:500],
            )
            try:
                revised = await provider.complete_structured(prompt, ExperimentPlan, max_tokens=1024)
                revised.artifact_strategy = "create_new_sim2l"
                # Keep original proposal — refinement only touches parameters/sweep/criteria.
                revised.proposal = plan.proposal
                self._complete_plan(revised)
                return revised
            except Exception:
                pass
        return plan  # fall back to unchanged plan if no provider
