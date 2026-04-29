#!/usr/bin/env python3
"""
ARC interactive chat — runs in the terminal like Claude Code.

Usage
-----
python3 examples/chat.py \
    --token  YOUR_BEARER_TOKEN \
    --model  gpt-oss:120b \
    --url    https://genai.rcac.purdue.edu/api

Resume a previous session:
    python3 examples/chat.py --token ... --session bandgap-abc123

Without a token (stub mode):
    python3 examples/chat.py --stub

All session data is stored in ~/.sim2l/<session_id>/

Commands inside the chat:
    /run [goal]           — run one research iteration (or use the goal from context)
    /iterate N            — run N iterations on the last goal
    /optimize [G] [P]     — genetic algorithm: G generations, P population size
    /exec <id> [k=v ...]  — run an existing artifact by name/id with given params
    /sweep [id]           — run the full parameter sweep for current (or named) artifact
    /artifacts            — list registered artifacts
    /results              — list saved runs
    /sessions             — list all past sessions
    /clear                — forget current goal (keeps artifact)
    /quit                 — exit
    (anything else is treated as a new research goal)
"""

import argparse
import asyncio
import json
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from arc.orchestrator.workflow import ResearchWorkflow
from arc.schemas.research import ResearchGoal
from arc.session import (
    new_session_id, list_sessions,
    save_session_meta, load_session_meta,
    delete_session, delete_all_sessions,
)


def _pct_off(outputs: dict, target: dict, registry: dict | None = None) -> str:
    from arc.packages import load_reviewer
    _km = load_reviewer()._keys_match
    parts = []
    for tk, tv in target.items():
        for ok_name, ov in outputs.items():
            if _km(tk, ok_name) or (registry and _registry_km(tk, ok_name, registry)):
                if isinstance(ov, (int, float)) and ov is not None:
                    pct = abs(ov - tv) / max(abs(tv), 1e-12) * 100
                    parts.append(f"{ok_name}={ov:.4g} ({pct:.1f}% off)")
                break
    return "  ".join(parts) if parts else "(no target key match)" if target else ""


def _registry_km(tk: str, ok: str, registry: dict) -> bool:
    tk_flat = tk.replace("_", "").lower()
    ok_flat = ok.replace("_", "").lower()
    for canon, entry in registry.items():
        canon_flat = canon.replace("_", "").lower()
        aliases_flat = [a.replace("_", "").lower() for a in entry.get("aliases", [])]
        all_forms = [canon_flat] + aliases_flat
        if tk_flat in all_forms and ok_flat in all_forms:
            return True
    return False


# ── ANSI colours ────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BLUE   = "\033[34m"
GREY   = "\033[90m"


def c(text, *codes):
    return "".join(codes) + str(text) + RESET


def header(text):
    print(f"\n{c('●', BOLD, CYAN)} {c(text, BOLD)}")


def step(label, value):
    label_str = c(f"  {label:<14}", DIM)
    print(f"{label_str} {value}")


def ok(text):
    print(f"  {c('✓', GREEN, BOLD)} {text}")


def warn(text):
    print(f"  {c('!', YELLOW, BOLD)} {text}")


def err(text):
    print(f"  {c('✗', RED, BOLD)} {text}")


def hr():
    print(c("  " + "─" * 56, DIM))


_PLAN_ACCEPT = {"ok", "yes", "y", "accept", "looks good", "good", "done", "go", "proceed", ""}


async def _review_plan_with_user(planner, plan, target: dict, max_rounds: int = 5):
    """Show the plan and loop until the user accepts or max_rounds reached.

    On the first round, warns if the plan's required_outputs don't cover target
    keys, and asks the user to confirm or redirect before building.

    Returns the (possibly revised) ExperimentPlan.
    """
    import asyncio

    # Derive which output key names the builder will be asked to produce.
    # These come from the target (canonical keys) + schema registry if available.
    required_outputs: list[str] = list(target.keys()) if target else []

    for round_num in range(max_rounds):
        # Display current plan.
        print()
        print(f"  {c('Parameters', BOLD)}     {plan.parameters}")
        print(f"  {c('Sweep', BOLD)}          {plan.parameter_sweep}")
        print(f"  {c('Criteria', BOLD)}       {plan.success_criteria}")
        if required_outputs:
            print(f"  {c('Must output', BOLD)}   {c(required_outputs, YELLOW)}")

        if round_num == 0:
            print(f"  {c('─' * 56, DIM)}")
            if required_outputs:
                print(f"  {c('The workflow MUST produce output keys:', DIM)} "
                      f"{c(str(required_outputs), YELLOW)}")
                print(f"  {c('Confirm this is correct, or tell me what outputs you need.', DIM)}")
            else:
                print(f"  {c('No target set — accept the plan or describe changes.', DIM)}")
            print(f"  {c('Type ok/yes/enter to accept, or describe what to change.', DIM)}")

        try:
            raw = input(c("  plan> ", BOLD, CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if raw.lower() in _PLAN_ACCEPT:
            ok("Plan accepted.")
            break

        # User gave feedback — check if they're specifying output keys.
        # Pattern: "output must be X" / "must produce X, Y" → update required_outputs.
        import re as _re
        output_override = _re.findall(
            r'(?:output|produce|return|compute|calculate)\s+([a-z][a-z0-9_]*(?:\s*,\s*[a-z][a-z0-9_]*)*)',
            raw.lower()
        )
        if output_override:
            new_keys = [k.strip() for part in output_override for k in part.split(",") if k.strip()]
            if new_keys:
                required_outputs = new_keys
                warn(f"Required outputs updated: {required_outputs}")

        # Refine the plan with LLM.
        print(f"  {c('refining...', DIM)}", end="\r")
        feedback = raw
        if required_outputs:
            feedback += f"\n\nIMPORTANT: the simulation workflow MUST produce output keys: {required_outputs}"
        try:
            plan = await planner.refine(plan, feedback)
        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{c('  Refinement interrupted — keeping current plan.', YELLOW)}")
            break
        print(" " * 40, end="\r")
        ok(f"Plan revised  {c(f'(round {round_num + 1}/{max_rounds})', DIM)}")

    # Store confirmed required outputs on the plan so the builder can use them.
    if required_outputs:
        plan.proposal.variables = list(
            dict.fromkeys(list(plan.proposal.variables or []) + required_outputs)
        )
        if not hasattr(plan, '_required_outputs'):
            object.__setattr__(plan, '_required_outputs', required_outputs) if hasattr(plan, '__setattr__') else None
        try:
            plan._required_outputs = required_outputs
        except Exception:
            pass

    return plan


# ── Post-approval exploration menu ──────────────────────────────────────────

async def _post_approval_menu(workflow, artifact, result, target: dict):
    """Offer exploration options after the goal is approved."""
    import csv, io, asyncio

    ctx = workflow._context
    run_history = ctx.memory.get("run_history", [])
    plan = ctx.memory.get("current_plan")

    print()
    print(f"{c('●', BOLD, GREEN)} {c('Goal achieved! What would you like to do next?', BOLD)}")
    print(f"  {c('1', CYAN)}  Show all session runs ranked by fitness")
    print(f"  {c('2', CYAN)}  Run a parameter sensitivity sweep")
    print(f"  {c('3', CYAN)}  Export results to CSV")
    print(f"  {c('4', CYAN)}  Run with custom parameters  (key=val ...)")
    print(f"  {c('5', CYAN)}  Set a new goal")
    print(f"  {c('Enter', DIM)}  Continue to prompt")

    try:
        choice = input(c("  explore> ", BOLD, GREEN)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if choice == "1":
        # Rank all runs by distance to target.
        from arc.packages import load_reviewer
        _km = load_reviewer()._keys_match
        registry = ctx.memory.get("schema_registry", {})
        rows = []
        for r in run_history:
            outs = r.get("outputs", {})
            ins  = r.get("inputs", {})
            if target:
                errors = []
                for tk, tv in target.items():
                    for ok_k, ov in outs.items():
                        if _km(tk, ok_k) or (registry and _registry_km(tk, ok_k, registry)):
                            if isinstance(ov, (int, float)):
                                errors.append(abs(ov - tv) / max(abs(tv), 1e-12) * 100)
                            break
                fitness = sum(errors) / len(errors) if errors else float("inf")
            else:
                fitness = -sum(v for v in outs.values() if isinstance(v, (int, float)))
            rows.append((fitness, ins, outs))
        rows.sort(key=lambda x: x[0])
        print()
        header(f"Session runs ranked  {c(f'({len(rows)} total)', DIM)}")
        for i, (fit, ins, outs) in enumerate(rows[:10], 1):
            fit_str = f"{fit:.2f}%" if fit != float("inf") else "∞"
            print(f"  {c(f'{i:>2}.', DIM)} fit={c(fit_str, CYAN)}  inputs={ins}  outputs={outs}")

    elif choice == "2":
        # Sensitivity sweep over the artifact's parameter_sweep definition.
        sweep = plan.parameter_sweep if plan else {}
        if not sweep:
            warn("No parameter sweep defined — use /sweep or /exec to explore manually.")
            return
        header(f"Sensitivity sweep  {c(artifact.name, CYAN)}")
        try:
            for param, values in sweep.items():
                print(f"  {c(param, BOLD)}")
                for v in values:
                    execution = await workflow.adapter.run(artifact, {param: v})
                    workflow.results.save(execution)
                    outs = execution.outputs or {}
                    pct = _pct_off(outs, target, ctx.memory.get("schema_registry", {})) if target else ""
                    cols = "  ".join(f"{k}={val:.4g}" for k, val in outs.items() if isinstance(val, (int, float)))
                    status_c = GREEN if execution.status == "completed" else RED
                    print(f"    {c('●', status_c)} {param}={v}  {cols}  {c(pct, YELLOW)}")
        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{c('  Sweep interrupted.', YELLOW)}")

    elif choice == "3":
        # Export run history to CSV string and print it.
        if not run_history:
            warn("No run history to export.")
            return
        all_keys = set()
        for r in run_history:
            all_keys.update(r.get("inputs", {}).keys())
            all_keys.update(r.get("outputs", {}).keys())
        fieldnames = sorted(all_keys)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in run_history:
            row = {**r.get("inputs", {}), **r.get("outputs", {})}
            writer.writerow(row)
        csv_text = buf.getvalue()
        # Save to file.
        import os
        from pathlib import Path
        out_path = Path(os.environ.get("SIM2L_HOME", Path.home() / ".sim2l" / "code")) \
                   / workflow.session_id / "results.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(csv_text)
        ok(f"Exported {len(run_history)} runs to {c(str(out_path), CYAN)}")
        print(f"  {c('Columns:', DIM)} {fieldnames}")

    elif choice == "4":
        # Custom params.
        raw = input(c("  Params (key=val ...) > ", BOLD)).strip()
        params = {}
        for kv in raw.split():
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    params[k] = float(v)
                except ValueError:
                    params[k] = v
        if params:
            await run_artifact(workflow, artifact.artifact_id, params)
        else:
            warn("No parameters parsed. Use format: key=value key2=value2")

    elif choice == "5":
        # Signal caller to reset — set a flag the continuation loop can check.
        ctx.memory["_reset_goal"] = True

    # choice == "" or anything else → fall through to prompt


# ── Streaming workflow runner ────────────────────────────────────────────────

async def run_artifact(workflow: ResearchWorkflow, artifact_id: str, params: dict):
    """Directly run an already-registered artifact with given parameters."""
    # Try exact ID first, then match by name prefix.
    records = workflow.artifacts.list_all()
    artifact = None
    for r in records:
        if r.artifact_id == artifact_id or r.artifact_id.startswith(artifact_id) or r.name == artifact_id:
            artifact = r
            break

    if artifact is None:
        err(f"No artifact found matching '{artifact_id}'. Use /artifacts to list them.")
        return

    adapter_name = type(workflow.adapter).__name__
    header(f"Executing  {c(artifact.name, CYAN)}  {c(f'[{adapter_name}]', DIM)}")

    # Validate first.
    validation = await workflow.adapter.validate_artifact(artifact)
    if not validation.valid:
        for e in validation.errors:
            err(e)
        return

    inputs = await workflow.adapter.prepare_inputs(artifact, params)
    step("Inputs", inputs)
    execution = await workflow.adapter.run(artifact, inputs)
    workflow.results.save(execution)

    step("Run ID",  execution.run_id[:8] + "...")
    step("Status",  c(execution.status, GREEN if execution.status == "completed" else RED))
    step("Outputs", execution.outputs)
    if execution.metrics:
        step("Metrics", {k: v for k, v in execution.metrics.items()
                         if k not in ("squid_id",) and not isinstance(v, str)})
    for log_line in execution.logs:
        print(f"    {c(log_line, DIM)}")
    hr()
    print()


_NOISE = frozenset({
    "to", "of", "a", "an", "the", "for", "and", "or", "in", "at", "is", "be",
    "v", "version", "around", "about", "approximately", "roughly", "near",
})

_UNITS = frozenset({"eV", "meV", "GPa", "MPa", "K", "nm", "pm", "angstrom",
                    "percent", "%", "GHz", "THz", "MHz", "cm", "m", "s"})


def _save_session(workflow: ResearchWorkflow, current_goal: str | None) -> None:
    """Persist session state to ~/.sim2l/<session_id>/session.json."""
    if workflow.session_id == "default":
        return
    ctx = workflow._context
    artifact = ctx.memory.get("current_artifact")
    save_session_meta(
        session_id=workflow.session_id,
        goal=current_goal,
        iteration=ctx.iteration,
        current_artifact_id=artifact.artifact_id if artifact else None,
        current_artifact_name=artifact.name if artifact else None,
        run_history=ctx.memory.get("run_history", []),
        target=ctx.memory.get("target", {}),
        next_parameters=ctx.memory.get("next_parameters", {}),
        schema_registry=ctx.memory.get("schema_registry", {}),
        primary_goal=ctx.memory.get("primary_goal"),
        refinements=ctx.memory.get("refinements", []),
    )


def _restore_session(workflow: ResearchWorkflow) -> str | None:
    """Reload session state from disk. Returns the saved goal, or None."""
    if workflow.session_id == "default":
        return None
    meta = load_session_meta(workflow.session_id)
    if not meta:
        return None
    ctx = workflow._context
    ctx.iteration = meta.get("iteration", 0)
    ctx.memory["run_history"] = meta.get("run_history", [])
    ctx.memory["target"] = meta.get("target", {})
    ctx.memory["next_parameters"] = meta.get("next_parameters", {})
    ctx.memory["schema_registry"] = meta.get("schema_registry", {})
    ctx.memory["primary_goal"] = meta.get("primary_goal")
    ctx.memory["refinements"] = meta.get("refinements", [])

    # Try to reload the current artifact object.
    artifact_id = meta.get("current_artifact_id")
    if artifact_id:
        try:
            artifact = workflow.artifacts.get(artifact_id)
            ctx.memory["current_artifact"] = artifact
            # Reload the plan from artifact metadata (best-effort).
        except Exception:
            pass

    return meta.get("goal")


def _parse_target(goal_text: str) -> dict:
    """Extract numeric targets from free text.

    Handles:
      bandgap = 1.1 eV        →  bandgap_ev: 1.1
      bandgap=1.12eV           →  bandgap_ev: 1.12
      to 1.1 eV bandgap        →  bandgap_ev: 1.1
      target bandgap_ev=1.1    →  bandgap_ev: 1.1
    """
    import re
    text = goal_text

    # Explicit key=value (highest priority)
    targets = {}
    for m in re.finditer(
        r"(\w+)\s*[=:]\s*([0-9]+\.?[0-9]*)\s*(eV|meV|GPa|MPa|K|nm|%|GHz|THz)?",
        text, re.IGNORECASE
    ):
        key = m.group(1).lower()
        if key in _NOISE:
            continue
        val = float(m.group(2))
        unit = (m.group(3) or "").lower()
        targets[f"{key}_{unit}" if unit else key] = val

    if targets:
        return targets

    # Natural language: find "NUMBER UNIT" then look left/right for a physics noun
    PHYSICS = re.compile(
        r"(bandgap|band.gap|band_gap|effective.mass|temperature|strain|"
        r"energy|frequency|wavelength|pressure|stress|fermi|mobility|"
        r"conductivity|resistivity|dielectric|refractive|absorption)",
        re.IGNORECASE
    )
    NUM_UNIT = re.compile(
        r"([0-9]+\.?[0-9]*)\s*(eV|meV|GPa|MPa|K|nm|%|GHz|THz|angstrom)?",
        re.IGNORECASE
    )
    for nm in NUM_UNIT.finditer(text):
        val = float(nm.group(1))
        unit = (nm.group(2) or "").lower()
        start, end = nm.start(), nm.end()
        window = text[max(0, start - 30): end + 30]
        pm = PHYSICS.search(window)
        if pm:
            key = re.sub(r"[\s\-]", "_", pm.group(0).lower())
            targets[f"{key}_{unit}" if unit else key] = val

    return targets


def _build_refined_goal(primary_goal: str, refinements: list[str]) -> str:
    """Compose the full goal text from primary goal + accumulated refinements."""
    if not refinements:
        return primary_goal
    ref_block = "; ".join(refinements)
    return (
        f"{primary_goal}\n\n"
        f"Additional constraints and refinements:\n{ref_block}"
    )


async def run_research(
    workflow: ResearchWorkflow,
    goal_text: str,
    domain: str = None,
    artifact: "ArtifactRecord | None" = None,
    refinement: str | None = None,
):
    from arc.schemas.artifact import ArtifactRecord as _AR  # noqa: F401

    from arc.packages import (
        load_ideator, load_planner, load_builder, load_reviewer, load_reflector, load_curator
    )

    ctx = workflow._context

    IdeatorAgent   = load_ideator().IdeatorAgent
    PlannerAgent   = load_planner().PlannerAgent
    BuilderAgent   = load_builder().Sim2LBuilderAgent
    ReviewerAgent  = load_reviewer().ReviewerAgent
    ReflectorAgent = load_reflector().ReflectorAgent
    CuratorAgent   = load_curator().CuratorAgent

    def agent(cls):
        return cls(context=ctx)

    # If this is a refinement of the primary goal, merge it into the goal text.
    if refinement:
        refinements = ctx.memory.get("refinements", [])
        refinements.append(refinement)
        ctx.memory["refinements"] = refinements
        goal_text = _build_refined_goal(ctx.memory.get("primary_goal", goal_text), refinements)

    target = _parse_target(goal_text) or ctx.memory.get("target", {})
    goal = ResearchGoal(
        goal=goal_text,
        domain=domain or "computational science",
        target=target,
    )
    if target:
        ctx.memory["target"] = target

    is_new_artifact = artifact is None

    if is_new_artifact:
        # ── Ideation ──────────────────────────────────────────────────────
        header("Ideation")
        print(f"  {c('searching catalog + thinking...', DIM)}", end="\r")
        proposal = await agent(IdeatorAgent).run(goal)
        print(" " * 40, end="\r")
        step("Hypothesis", textwrap.fill(proposal.hypothesis, 60, subsequent_indent=" " * 16))
        step("Objective",  textwrap.fill(proposal.objective,  60, subsequent_indent=" " * 16))

        # ── Catalog reuse check ───────────────────────────────────────────
        catalog_hits = ctx.memory.pop("catalog_hits", [])
        prior_results = ctx.memory.pop("catalog_prior_results", [])
        catalog_artifact = None

        if catalog_hits:
            best = catalog_hits[0]
            best_name = best.get("name", "")
            best_desc = (best.get("description") or "")[:80]
            step("Catalog hit", f"{c(best_name, CYAN)}  {c(best_desc, DIM)}")
            if prior_results:
                step("Prior runs", f"{len(prior_results)} result(s) found")
                for r in prior_results[:2]:
                    print(f"    {c('inputs', DIM)}={r.get('input_params', {})}  "
                          f"{c('outputs', DIM)}={r.get('output_params', {})}")

            # Ask user whether to reuse the catalog artifact.
            raw = input(
                c(f"  Reuse catalog artifact '{best_name}'? [Y / n] > ", BOLD)
            ).strip().lower()
            if raw not in ("n", "no"):
                # Wrap the catalog hit as a local artifact so the rest of the
                # pipeline (validate → execute → review) works unchanged.
                from arc.schemas.artifact import ArtifactDraft
                input_schema  = best.get("input_schema") or {}
                output_schema = best.get("output_schema") or {}
                # Build default parameters from schema defaults.
                default_params = {
                    k: v.get("default", 1.0) if isinstance(v, dict) else 1.0
                    for k, v in input_schema.items()
                }
                catalog_draft = ArtifactDraft(
                    name=best_name,
                    description=best_desc,
                    files={},          # no local files — will be fetched via sim2l repo
                    metadata={
                        "created_by": "catalog",
                        "strategy": "reuse_catalog",
                        "hypothesis": proposal.hypothesis,
                        "success_criteria": "",
                        "sim2l_inputs": {
                            k: {"type": "Number",
                                "default": v.get("default", 1.0) if isinstance(v, dict) else 1.0,
                                "description": k}
                            for k, v in input_schema.items()
                        },
                        "sim2l_outputs": {
                            k: {"type": "Number", "description": k}
                            for k in output_schema
                        },
                        "catalog_id": best.get("id"),
                        "workflow_source": best.get("metadata", {}).get("workflow_source", ""),
                    },
                )
                catalog_artifact = workflow.artifacts.register(catalog_draft)
                ok(f"Reusing catalog artifact  {c(best_name, CYAN)}")
                if prior_results:
                    ok(f"Prior results available: "
                       f"{c(str(len(prior_results)) + ' runs', DIM)}")

                # Seed next_parameters from prior results if we have a target.
                if target and prior_results:
                    best_prior = min(
                        prior_results,
                        key=lambda r: sum(
                            abs(r.get("output_params", {}).get(ok_k, float("inf")) - tv)
                            for ok_k, tv in target.items()
                            if ok_k in r.get("output_params", {})
                        ),
                    )
                    seed_params = {
                        k: v for k, v in best_prior.get("input_params", {}).items()
                        if k in input_schema
                    }
                    if seed_params:
                        ctx.memory["next_parameters"] = seed_params
                        step("Seed params", c(str(seed_params), YELLOW))

                ctx.memory["current_artifact"] = catalog_artifact
                ctx.memory["current_plan"] = None
                artifact = catalog_artifact
                # Skip build — jump straight to validation.
                is_new_artifact = False  # prevents double-validation below

        if catalog_artifact is None:
            # ── Planning ──────────────────────────────────────────────────
            header("Planning")
            print(f"  {c('thinking...', DIM)}", end="\r")
            plan = await agent(PlannerAgent).run(proposal)
            print(" " * 40, end="\r")
            if target:
                step("Target", target)
            plan = await _review_plan_with_user(agent(PlannerAgent), plan, target=target)
            # Inject confirmed required outputs into context for the builder.
            required_outputs = getattr(plan, '_required_outputs', list(target.keys()) if target else [])
            if required_outputs:
                ctx.memory["required_outputs"] = required_outputs

            # ── Building artifact ──────────────────────────────────────────
            header("Building artifact")
            print(f"  {c('generating workflow...', DIM)}", end="\r")
            draft = await agent(BuilderAgent).run(plan)
            print(" " * 40, end="\r")
            artifact = workflow.artifacts.register(draft)
            ok(f"Artifact registered  {c(artifact.artifact_id[:8] + '...', DIM)}")
            ok(f"Path: {c(artifact.path, DIM)}")

            # ── Curation ──────────────────────────────────────────────────
            print(f"  {c('curating schema...', DIM)}", end="\r")
            artifact = await agent(CuratorAgent).run(artifact)
            print(" " * 40, end="\r")
            registry = ctx.memory.get("schema_registry", {})
            if registry:
                ok(f"Schema registry: {c(list(registry.keys()), DIM)}")

            # Save for later iterations
            ctx.memory["current_artifact"] = artifact
            ctx.memory["current_plan"] = plan
    else:
        # ── Reuse existing artifact, optionally re-plan for a refinement ───
        plan = ctx.memory.get("current_plan")

        if refinement and plan is None:
            # Refinement with no current plan — re-plan against the updated goal.
            header("Re-planning  (refinement)")
            print(f"  {c('Refinement:', YELLOW)} {refinement}")
            print(f"  {c('thinking...', DIM)}", end="\r")
            from arc.packages import load_planner as _lp
            _PlannerCls = _lp().PlannerAgent
            from arc.schemas.research import ResearchProposal
            # Build a minimal proposal from context + current artifact to re-plan.
            _proposal = ResearchProposal(
                hypothesis=artifact.metadata.get("hypothesis", goal_text),
                objective=goal_text,
                approach=artifact.metadata.get("approach", ""),
                variables=list(artifact.metadata.get("sim2l_inputs", {}).keys()),
                expected_outputs=list(artifact.metadata.get("sim2l_outputs", {}).keys()),
                success_criteria="",
                computational_requirements="",
            )
            plan = await agent(_PlannerCls).run(_proposal)
            print(" " * 40, end="\r")
            plan = await _review_plan_with_user(agent(_PlannerCls), plan, target=target)
            ctx.memory["current_plan"] = plan
            required_outputs = getattr(plan, '_required_outputs', list(target.keys()) if target else [])
            if required_outputs:
                ctx.memory["required_outputs"] = required_outputs

        next_params = ctx.memory.pop("next_parameters", {})

        # Strip keys that don't belong to the artifact's input schema
        # (e.g. duration_seconds, squid_id that sneak in via metrics).
        if next_params and artifact is not None:
            schema_keys = set(artifact.metadata.get("sim2l_inputs", {}).keys())
            if schema_keys:
                next_params = {k: v for k, v in next_params.items() if k in schema_keys}

        if next_params:
            header(f"Iteration {ctx.iteration + 1}  {c('(reusing artifact, new params)', DIM)}")
            step("Next params", next_params)
            run_inputs = next_params
        else:
            header(f"Iteration {ctx.iteration + 1}  {c('(reusing artifact, same params)', DIM)}")
            run_inputs = plan.parameters if plan else {}

    # ── Validation ────────────────────────────────────────────────────────
    if is_new_artifact:
        header("Validation")
        validation = await workflow.adapter.validate_artifact(artifact)
        if validation.valid:
            ok("Artifact is valid")
        else:
            for e in validation.errors:
                err(e)
            for w in validation.warnings:
                warn(w)
            print()
            return None

    # ── Execution ─────────────────────────────────────────────────────────
    adapter_name = type(workflow.adapter).__name__
    header(f"Execution  {c(f'[{adapter_name}]', DIM)}")
    run_inputs = run_inputs if not is_new_artifact else plan.parameters
    inputs = await workflow.adapter.prepare_inputs(artifact, run_inputs)
    step("Inputs", inputs)
    execution = await workflow.adapter.run(artifact, inputs)
    workflow.results.save(execution)
    step("Run ID",  execution.run_id[:8] + "...")
    step("Status",  c(execution.status, GREEN if execution.status == "completed" else RED))
    step("Outputs", execution.outputs)
    for log_line in execution.logs:
        print(f"    {c(log_line, DIM)}")

    # Show distance to target if set
    if target and execution.outputs:
        from arc.packages import load_reviewer
        _keys_match = load_reviewer()._keys_match
        diffs = []
        unmatched_targets = []
        for tk, tv in target.items():
            matched = False
            for ok_name, ov in execution.outputs.items():
                if _keys_match(tk, ok_name):
                    matched = True
                    if isinstance(ov, (int, float)):
                        pct = abs(ov - tv) / max(abs(tv), 1e-12) * 100
                        diffs.append(f"{ok_name}={ov:.4g}  target={tv}  ({pct:.1f}% off)")
            if not matched:
                unmatched_targets.append(tk)
        if diffs:
            step("vs target", "")
            for d in diffs:
                print(f"    {c(d, YELLOW)}")
        if unmatched_targets:
            warn(f"Target key(s) {unmatched_targets} did not match any output {list(execution.outputs.keys())}")
            warn("Approval will always fail until keys match. Check sim output names.")

    # ── Review ────────────────────────────────────────────────────────────
    header("Review")
    print(f"  {c('evaluating...', DIM)}", end="\r")
    review = await agent(ReviewerAgent).run(execution)
    print(" " * 40, end="\r")
    approved_str = c("approved ✓", GREEN, BOLD) if review.approved else c("not approved ✗", YELLOW, BOLD)
    step("Decision",  approved_str)
    step("Summary",   textwrap.fill(review.summary, 60, subsequent_indent=" " * 16))
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

    # ── Reflection ────────────────────────────────────────────────────────
    ReflectorCls = load_reflector().ReflectorAgent
    reflection_lessons = await agent(ReflectorCls).run(review, execution=execution)

    # ── Provenance ────────────────────────────────────────────────────────
    workflow.provenance.record(
        session_id=ctx.session_id,
        action="chat_run",
        agent="orchestrator",
        artifact_id=artifact.artifact_id,
        run_id=execution.run_id,
        inputs=goal.model_dump(),
        outputs=review.model_dump(),
    )
    ctx.iteration += 1
    _save_session(workflow, goal_text)
    hr()
    print()
    return {"artifact": artifact, "execution": execution, "review": review, "reflection": reflection_lessons}


# ── Autonomous continuation loop ─────────────────────────────────────────────

async def _run_with_continuation(
    workflow: ResearchWorkflow,
    goal_text: str,
    max_iterations: int,
    start_artifact=None,
    refinement: str | None = None,
):
    """Run the first iteration, ask the user once to confirm the continuation
    budget, then loop autonomously until approved / budget exhausted / stopped.

    First-run confirmation prompt:
      - Enter / y / yes      → run up to max_iterations more steps
      - n / no               → stop after the first result
      - a number N           → run up to N more steps
      - "explore" / "ga"     → switch immediately to GA mode
      - "explore N" / "ga N" → GA with N generations

    Mid-run (only when reviewer is uncertain and has no next_parameters):
      Shows a mini-survey with 4 choices.  Otherwise runs silently.
    """
    ctx = workflow._context
    artifact = start_artifact
    iterations_done = 0
    # Budget confirmed by user after first iteration; None = not yet asked
    confirmed_budget: int | None = None

    # Only pass refinement on the first iteration; subsequent iterations reuse artifact.
    _first_refinement = refinement

    while True:
        result = await run_research(workflow, goal_text, artifact=artifact, refinement=_first_refinement)
        _first_refinement = None  # consumed after first call
        iterations_done += 1

        if result is None:
            break  # validation failed

        review = result["review"]
        artifact = result["artifact"]  # reuse on next pass

        if review.approved:
            ok(c("Goal achieved — approved by reviewer.", GREEN, BOLD))
            target = ctx.memory.get("target", {})
            await _post_approval_menu(workflow, artifact, result, target)
            break

        if review.iteration_complete or review.strategy == "stop":
            print(c("  Reviewer: no further improvement possible.", DIM))
            break

        # ── Compute next_p and strategy once ──────────────────────────────
        strategy = review.strategy or "step"
        next_p = dict(review.next_parameters or {})
        schema_keys = set(artifact.metadata.get("sim2l_inputs", {}).keys())
        if schema_keys:
            next_p = {k: v for k, v in next_p.items() if k in schema_keys}

        # ── First-run: ask user once to confirm the continuation budget ────
        if confirmed_budget is None:
            remaining_display = max_iterations
            if strategy == "explore":
                print(f"\n{c('●', BOLD, CYAN)} {c('Reviewer suggests:', BOLD)} broad GA exploration")
                print(f"   {c(review.summary, DIM)}")
                print(f"   {c(f'Default: 10 generations × pop 8  |  or type N to change  |  n to stop', DIM)}")
                raw = input(c("  Confirm? [Y / N / <gens> <pop>] > ", BOLD)).strip()
            else:
                print(f"\n{c('●', BOLD, CYAN)} {c('Reviewer suggests:', BOLD)} continue iterating")
                print(f"   {c(review.summary, DIM)}")
                if next_p:
                    print(f"   Next params: {c(str(next_p), YELLOW)}")
                print(f"   {c(f'Will run up to {remaining_display} more iterations automatically', DIM)}")
                raw = input(c("  Confirm? [Y / N / <iterations> / explore] > ", BOLD)).strip()

            rl = raw.lower()
            if rl in ("n", "no"):
                print(c("  Stopping.", DIM))
                break
            elif rl in ("explore", "ga") or (strategy == "explore" and rl in ("y", "yes", "")):
                strategy = "explore"
                confirmed_budget = 0  # GA handles its own budget
            elif rl.split() and rl.split()[0].isdigit():
                parts = rl.split()
                if strategy == "explore":
                    confirmed_budget = 0
                    # Treat as gens [pop] override — handled below in explore block
                else:
                    confirmed_budget = int(parts[0])
                    max_iterations = iterations_done + confirmed_budget
            else:
                # y / yes / Enter → accept defaults
                confirmed_budget = max_iterations
                if strategy == "explore":
                    confirmed_budget = 0

        else:
            # Subsequent iterations — only pause for a mini-survey when the
            # reviewer is stuck (step strategy but no next_parameters).
            if strategy == "step" and not next_p:
                print(f"\n{c('●', BOLD, YELLOW)} {c('Reviewer uncertain — what next?', BOLD)}")
                print(f"   {c(review.summary, DIM)}")
                print(f"    {c('1', CYAN)} Same parameters again")
                print(f"    {c('2', CYAN)} Switch to genetic algorithm")
                print(f"    {c('3', CYAN)} Stop")
                print(f"    {c('4', CYAN)} Custom params  (key=val ...)")
                raw = input(c("  Choice > ", BOLD)).strip()
                if raw == "2":
                    strategy = "explore"
                elif raw == "3" or raw.lower() in ("n", "no"):
                    break
                elif raw == "4" or "=" in raw:
                    for kv in raw.lstrip("4").strip().split():
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            try:
                                next_p[k] = float(v)
                            except ValueError:
                                pass
                # 1 / Enter → fall through, reuse params

        # Enforce budget (GA has its own)
        if strategy != "explore" and iterations_done >= max_iterations:
            warn(f"Reached iteration limit ({max_iterations}).")
            break

        # ── Execute strategy ───────────────────────────────────────────────
        if strategy == "explore":
            gens, pop = 10, 8
            # Allow override from first-run raw input
            if confirmed_budget == 0 and 'raw' in dir():
                parts = raw.split()
                if len(parts) >= 1 and parts[0].isdigit():
                    gens = int(parts[0])
                if len(parts) >= 2 and parts[1].isdigit():
                    pop = int(parts[1])

            from arc.packages import load_optimizer
            GeneticOptimizerAgent = load_optimizer().GeneticOptimizerAgent
            ctx.memory["adapter"] = workflow.adapter
            target = ctx.memory.get("target", {})

            header(f"Genetic Optimisation  {c(artifact.name, CYAN)}  {c(f'[{gens} gen × pop {pop}]', DIM)}")
            if target:
                step("Target", target)
            hr()

            _reg = ctx.memory.get("schema_registry", {})

            async def _on_gen(gen, best_ind, best_out, fitness):
                fit_str = f"{fitness:.4g}" if fitness != float("inf") else "∞"
                pct = _pct_off(best_out, target, _reg) if target else ""
                print(f"  {c(f'gen {gen:>2}', DIM)}  fit={c(fit_str, CYAN)}  {c(pct, YELLOW)}")

            ga_result = await GeneticOptimizerAgent(context=ctx).run(
                artifact, target=target,
                max_generations=gens, pop_size=pop,
                on_generation=_on_gen,
            )
            hr()
            ok(f"GA done — {ga_result['generations_run']} generations")
            step("Best inputs",  {k: round(v, 6) for k, v in ga_result["best_inputs"].items()})
            step("Best outputs", {k: round(v, 6) if isinstance(v, float) else v
                                   for k, v in ga_result["best_outputs"].items()})
            if target:
                pct = _pct_off(ga_result["best_outputs"], target, _reg)
                if pct:
                    step("vs target", c(pct, YELLOW))
            if ga_result.get("converged"):
                ok(c("Converged within threshold!", GREEN, BOLD))
                _save_session(workflow, goal_text)
                break

            _save_session(workflow, goal_text)
            # Switch back to step mode with GA's best params for final verification
            strategy = "step"
            confirmed_budget = confirmed_budget if confirmed_budget else 1
            max_iterations = iterations_done + 1  # one final verification run
            # next_parameters already set in context by optimizer

        else:
            # step: put suggested params into context for next run_research
            if next_p:
                ctx.memory["next_parameters"] = next_p

    _save_session(workflow, goal_text)


# ── Chat loop ────────────────────────────────────────────────────────────────

def print_banner(provider, model, base_url, session_id):
    print(f"""
{c('ARC', BOLD, CYAN)} {c('Autonomous Research Coder', DIM)}
{c('━' * 60, CYAN)}
  {c('Session', BOLD)}   {c(session_id, BOLD + CYAN)}
  {c('Data', DIM)}      {c(f'~/.sim2l/code/{session_id}/', DIM)}
{c('─' * 60, DIM)}
  Provider : {c(provider or 'stub (no LLM)', YELLOW)}
  Model    : {c(model or 'auto', YELLOW)}
  Endpoint : {c(base_url or 'n/a', DIM)}
{c('─' * 60, DIM)}
Type your research goal — subsequent inputs refine it (add constraints, boundaries, etc.)
Use /run <new goal> to start a completely fresh goal.
""")



def _install_sigint_handler():
    """Make Ctrl+C cancel the current asyncio task instead of killing the process."""
    import signal, asyncio

    def _handler(sig, frame):
        loop = asyncio.get_event_loop()
        for task in asyncio.all_tasks(loop):
            if not task.done() and task != asyncio.current_task():
                task.cancel()

    signal.signal(signal.SIGINT, _handler)


async def chat_loop(workflow: ResearchWorkflow, provider, model, base_url, max_iterations: int = 20):
    import asyncio
    _install_sigint_handler()

    # Restore any previously saved session state.
    saved_goal = _restore_session(workflow)
    print_banner(provider, model, base_url, workflow.session_id)
    current_goal = None

    if saved_goal:
        ok(f"Resumed session  {c(workflow.session_id, CYAN)}")
        primary_goal = workflow._context.memory.get("primary_goal") or saved_goal
        step("Primary goal", primary_goal)
        refinements_so_far = workflow._context.memory.get("refinements", [])
        if refinements_so_far:
            step("Refinements", f"{len(refinements_so_far)}: " + "; ".join(refinements_so_far))
        artifact = workflow._context.memory.get("current_artifact")
        if artifact:
            step("Artifact", f"{artifact.name}  {c(artifact.artifact_id[:8] + '...', DIM)}")
        step("Iteration", workflow._context.iteration)
        current_goal = saved_goal
        print()

    while True:
        try:
            raw = input(c("you> ", BOLD, CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{c('Goodbye.', DIM)}")
            break

        if not raw:
            continue

        # ── Commands ──────────────────────────────────────────────────────
        if raw.lower() in ("/quit", "/exit", "/q"):
            print(c("Goodbye.", DIM))
            break

        if raw.lower() in ("/help", "help", "?", "/?"):
            ctx = workflow._context
            artifact = ctx.memory.get("current_artifact")
            print(f"""
{c('ARC Commands', BOLD, CYAN)}
{c('─' * 56, DIM)}
  {c('<goal text>', BOLD)}        Set primary goal (or refine it if one already exists)
  {c('/run [goal]', BOLD)}        Run fresh with a new primary goal (resets everything)
  {c('/iterate [N]', BOLD)}       Continue iterating on current goal (default: 3 steps)
  {c('/optimize [G] [P]', BOLD)}  Genetic algorithm: G generations, P population (defaults: 10 8)
  {c('/exec <id> [k=v]', BOLD)}   Run artifact directly with given parameters
  {c('/sweep [id]', BOLD)}        Run the parameter sweep for current or named artifact
  {c('/artifacts', BOLD)}         List registered artifacts in this session
  {c('/results', BOLD)}           List saved runs in this session
  {c('/sessions', BOLD)}          List all past sessions
  {c('/clear', BOLD)}             Forget current goal (keeps artifact and history)
  {c('/help', BOLD)}              Show this help
  {c('/quit', BOLD)}              Exit
{c('─' * 56, DIM)}
{c('Current session:', BOLD)} {c(workflow.session_id, CYAN)}
{c('Primary goal:   ', BOLD)} {c(ctx.memory.get('primary_goal') or current_goal or 'none', DIM)}
{c('Refinements:    ', BOLD)} {c(str(len(ctx.memory.get('refinements', []))) + ' constraint(s)', DIM)}
{c('Current artifact:', BOLD)} {c(f"{artifact.name}  ({artifact.artifact_id[:8]}...)" if artifact else 'none', DIM)}
{c('Iteration:      ', BOLD)} {c(ctx.iteration, DIM)}
""")
            continue

        if raw.lower() == "/clear":
            current_goal = None
            workflow._context.memory.pop("primary_goal", None)
            workflow._context.memory.pop("refinements", None)
            print(c("  Goal cleared. Next input will set a new primary goal.", DIM))
            continue

        if raw.lower() == "/sessions":
            sessions = list_sessions()
            if not sessions:
                print(c("  No sessions yet.", DIM))
            for s in sessions:
                active = c(" ◀ current", GREEN) if s["session_id"] == workflow.session_id else ""
                goal_preview = (s["goal"] or "")[:50]
                print(f"  {c(s['session_id'], CYAN)}  iter={s['iteration']}  {c(goal_preview, DIM)}{active}")
            print(c(f"\n  Resume with:     python3 examples/chat.py --session <id>", DIM))
            print(c(f"  Delete one:      python3 examples/chat.py --delete-session <id>", DIM))
            print(c(f"  Delete all:      python3 examples/chat.py --delete-all-sessions", DIM))
            continue

        if raw.lower() == "/artifacts":
            records = workflow.artifacts.list_all()
            if not records:
                print(c("  No artifacts registered yet.", DIM))
            for r in records:
                print(f"  {c(r.artifact_id[:8], CYAN)}  {r.name}  {c(r.state, DIM)}")
            continue

        if raw.lower() == "/results":
            results = workflow.results.list_all()
            if not results:
                print(c("  No runs yet.", DIM))
            for r in results:
                status_col = GREEN if r.status == "completed" else RED
                print(f"  {c(r.run_id[:8], CYAN)}  {c(r.status, status_col)}  outputs={r.outputs}")
            continue

        # ── Long-running commands — wrap with Ctrl+C handling ──────────────
        try:
            if raw.lower().startswith("/exec"):
                parts = raw.split()
                if len(parts) < 2:
                    err("Usage: /exec <artifact_id_or_name> [key=value ...]")
                    continue
                art_id = parts[1]
                params = {}
                for kv in parts[2:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        try:
                            params[k] = float(v)
                        except ValueError:
                            params[k] = v
                await run_artifact(workflow, art_id, params)

            elif raw.lower().startswith("/sweep"):
                parts = raw.split()
                art_id = parts[1] if len(parts) > 1 else None
                if not art_id:
                    cur = workflow._context.memory.get("current_artifact")
                    if cur:
                        art_id = cur.artifact_id
                    else:
                        err("Usage: /sweep <artifact_id_or_name>  (or set a goal first)")
                        continue
                records = workflow.artifacts.list_all()
                artifact = next(
                    (r for r in records if r.artifact_id == art_id
                     or r.artifact_id.startswith(art_id) or r.name == art_id),
                    None,
                )
                if artifact is None:
                    err(f"No artifact found matching '{art_id}'.")
                    continue
                plan = workflow._context.memory.get("current_plan")
                sweep = plan.parameter_sweep if plan else {}
                if not sweep:
                    err("No parameter sweep defined for this artifact.")
                    continue
                header(f"Sweep  {c(artifact.name, CYAN)}")
                all_results = []
                for param, values in sweep.items():
                    for v in values:
                        inputs = {param: v}
                        execution = await workflow.adapter.run(artifact, inputs)
                        workflow.results.save(execution)
                        row = {param: v, **execution.outputs}
                        all_results.append(row)
                        cols = "  ".join(f"{k}={val:.4g}" for k, val in row.items() if isinstance(val, (int, float)))
                        status_c = GREEN if execution.status == "completed" else RED
                        print(f"  {c('●', status_c)} {cols}  {c(execution.logs[1] if len(execution.logs) > 1 else '', DIM)}")
                step("Total runs", len(all_results))
                hr()
                print()

            elif raw.lower().startswith("/optimize"):
                parts = raw.split()
                n_gen = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
                pop = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 8
                current_artifact = workflow._context.memory.get("current_artifact")
                if current_artifact is None:
                    err("No artifact. Set a goal and run at least one iteration first.")
                    continue
                target = workflow._context.memory.get("target", {})
                if not target:
                    warn("No target defined — GA will maximise total output magnitude.")

                from arc.packages import load_optimizer
                GeneticOptimizerAgent = load_optimizer().GeneticOptimizerAgent
                workflow._context.memory["adapter"] = workflow.adapter

                header(f"Genetic Optimisation  {c(current_artifact.name, CYAN)}")
                step("Generations", n_gen)
                step("Pop size",    pop)
                if target:
                    step("Target", target)
                hr()

                _reg = workflow._context.memory.get("schema_registry", {})

                async def _on_gen(gen, best_ind, best_out, fitness):
                    fit_str = f"{fitness:.4g}" if fitness != float("inf") else "∞"
                    pct = _pct_off(best_out, target, _reg) if target else ""
                    print(f"  {c(f'gen {gen:>2}', DIM)}  fit={c(fit_str, CYAN)}  {c(pct, YELLOW)}")

                opt_agent = GeneticOptimizerAgent(context=workflow._context)
                ga_result = await opt_agent.run(
                    current_artifact, target=target,
                    max_generations=n_gen, pop_size=pop,
                    on_generation=_on_gen,
                )
                hr()
                ok(f"Done — {ga_result['generations_run']} generations")
                step("Best inputs",  {k: round(v, 6) for k, v in ga_result["best_inputs"].items()})
                step("Best outputs", {k: round(v, 6) if isinstance(v, float) else v
                                       for k, v in ga_result["best_outputs"].items()})
                if target:
                    pct = _pct_off(ga_result["best_outputs"], target, _reg)
                    if pct:
                        step("vs target", c(pct, YELLOW))
                if ga_result.get("converged"):
                    ok(c("Converged within threshold!", GREEN, BOLD))
                else:
                    fit_val = ga_result["best_fitness"]
                    step("Best fitness", f"{fit_val:.4g}" if fit_val != float("inf") else "∞ (no target key matched)")
                _save_session(workflow, current_goal)
                print()

            elif raw.lower().startswith("/iterate"):
                parts = raw.split()
                n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
                if not current_goal:
                    err("No goal set. Type a goal first, or use /run <goal>.")
                    continue
                current_artifact = workflow._context.memory.get("current_artifact")
                if current_artifact is None and workflow.provider is None:
                    err("No artifact and no LLM provider — set a goal first with a provider configured.")
                    continue
                await _run_with_continuation(
                    workflow, current_goal,
                    max_iterations=n,
                    start_artifact=current_artifact,
                )

            elif raw.lower().startswith("/run"):
                goal_text = raw[4:].strip() or current_goal
                if not goal_text:
                    err("No goal. Provide one after /run or type a goal first.")
                    continue
                current_goal = goal_text
                # /run always starts fresh (new artifact, new primary goal)
                ctx = workflow._context
                ctx.memory.pop("current_artifact", None)
                ctx.memory.pop("current_plan", None)
                ctx.memory.pop("run_history", None)
                ctx.memory.pop("next_parameters", None)
                ctx.memory.pop("refinements", None)
                ctx.memory["primary_goal"] = goal_text
                await _run_with_continuation(workflow, current_goal, max_iterations=max_iterations)

            else:
                # ── Free text: first input sets primary goal; subsequent inputs
                # ── are treated as refinements that constrain the primary goal.
                ctx = workflow._context
                primary_goal = ctx.memory.get("primary_goal")
                current_artifact = ctx.memory.get("current_artifact")

                if primary_goal is None or current_artifact is None:
                    # No primary goal yet — this IS the primary goal. Start fresh.
                    current_goal = raw
                    ctx.memory["primary_goal"] = raw
                    ctx.memory.pop("refinements", None)
                    ctx.memory.pop("current_artifact", None)
                    ctx.memory.pop("current_plan", None)
                    ctx.memory.pop("run_history", None)
                    ctx.memory.pop("next_parameters", None)
                    await _run_with_continuation(workflow, current_goal, max_iterations=max_iterations)
                else:
                    # Primary goal exists — treat this input as a refinement.
                    refinement = raw
                    print(f"  {c('Refining goal:', DIM)} {c(primary_goal[:60], DIM)}")
                    print(f"  {c('Constraint:   ', DIM)} {c(refinement, YELLOW)}")
                    # Reuse existing artifact but re-plan with the refinement.
                    # Clear plan so planner sees the updated goal, but keep artifact.
                    ctx.memory.pop("current_plan", None)
                    ctx.memory.pop("next_parameters", None)
                    await _run_with_continuation(
                        workflow, primary_goal,
                        max_iterations=max_iterations,
                        start_artifact=current_artifact,
                        refinement=refinement,
                    )

        except (asyncio.CancelledError, KeyboardInterrupt):
            print(f"\n{c('  Interrupted.', YELLOW)}  Session saved. Type a new goal or /quit.")
            _save_session(workflow, current_goal)


def main():
    parser = argparse.ArgumentParser(description="ARC interactive chat")
    parser.add_argument("--token",    default=None)
    parser.add_argument("--model",    default=None)
    parser.add_argument("--url",      default="https://genai.rcac.purdue.edu/api")
    parser.add_argument("--stub",     action="store_true")
    parser.add_argument("--session",  default=None,
                        help="Resume an existing session ID, or omit to start a new one.")
    parser.add_argument("--list-sessions", action="store_true",
                        help="List all sessions and exit.")
    parser.add_argument("--delete-session", metavar="SESSION_ID",
                        help="Delete a specific session and exit.")
    parser.add_argument("--delete-all-sessions", action="store_true",
                        help="Delete ALL sessions and exit (asks for confirmation).")
    parser.add_argument("--max-iterations", type=int, default=20,
                        help="Max auto-iterations per goal before stopping (default 20).")
    args = parser.parse_args()

    if args.list_sessions:
        sessions = list_sessions()
        if not sessions:
            print("No sessions found.")
        for s in sessions:
            print(f"  {s['session_id']}  iter={s['iteration']}  {(s['goal'] or '')[:60]}")
        return

    if args.delete_session:
        sid = args.delete_session
        meta = load_session_meta(sid)
        if not meta:
            print(f"ERROR: session '{sid}' not found.", file=sys.stderr)
            sys.exit(1)
        confirm = input(f"Delete session '{sid}' and all its data? [y/N] ").strip().lower()
        if confirm in ("y", "yes"):
            if delete_session(sid):
                print(f"Deleted session '{sid}'.")
            else:
                print(f"ERROR: could not delete '{sid}'.", file=sys.stderr)
                sys.exit(1)
        else:
            print("Aborted.")
        return

    if args.delete_all_sessions:
        sessions = list_sessions()
        if not sessions:
            print("No sessions to delete.")
            return
        print(f"Found {len(sessions)} session(s):")
        for s in sessions:
            print(f"  {s['session_id']}  iter={s['iteration']}  {(s['goal'] or '')[:50]}")
        confirm = input("Delete ALL sessions and their data? [y/N] ").strip().lower()
        if confirm in ("y", "yes"):
            deleted = delete_all_sessions()
            print(f"Deleted {len(deleted)} session(s).")
        else:
            print("Aborted.")
        return

    if args.stub:
        provider = token = model = base_url = None
    else:
        if not args.token:
            print("ERROR: --token required (or use --stub)", file=sys.stderr)
            sys.exit(1)
        provider = "openwebui"
        token    = args.token
        model    = args.model
        base_url = args.url

    # Always use an explicit session ID — resume with --session, otherwise create fresh.
    if args.session:
        session_id = args.session
        meta = load_session_meta(session_id)
        if not meta:
            print(f"ERROR: session '{session_id}' not found. Use --list-sessions to see available sessions.", file=sys.stderr)
            sys.exit(1)
    else:
        session_id = new_session_id()
        save_session_meta(
            session_id=session_id,
            goal=None,
            iteration=0,
            current_artifact_id=None,
            current_artifact_name=None,
            run_history=[],
            target={},
            next_parameters={},
            created=datetime.now(timezone.utc).isoformat(),
        )

    workflow = ResearchWorkflow(
        provider_name=provider,
        token=token,
        model=model,
        base_url=base_url,
        session_id=session_id,
    )

    asyncio.run(chat_loop(workflow, provider, model, base_url, max_iterations=args.max_iterations))


if __name__ == "__main__":
    main()
