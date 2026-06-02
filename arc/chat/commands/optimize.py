"""``/optimize [G] [P]`` — genetic algorithm optimization."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, YELLOW, c, err, header, hr, ok, step, warn


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.chat.research.targets import pct_off as _pct_off
    from arc.chat.session_io import save_session as _save_session
    from arc.packages import resolve_role

    workflow = state.workflow
    n_gen = int(argv[0]) if len(argv) > 0 and argv[0].isdigit() else 10
    pop = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 8

    current_artifact = state.current_artifact
    if current_artifact is None:
        err("No artifact. Set a goal and run at least one iteration first.")
        return

    target = state.target
    if not target:
        warn("No target defined — GA will maximise total output magnitude.")

    GeneticOptimizerAgent = resolve_role("optimizer", workflow)
    workflow._context.memory["adapter"] = workflow.adapter

    # Provenance: which optimizer is running + where its search space comes
    # from (design/todo.md item 8).
    from arc.core.strategies import resolve_strategy_name
    overrides = workflow._context.memory.get("strategy_overrides") or {}
    try:
        from arc.core.config import load_arc_toml
        _p, _config = load_arc_toml()
    except Exception:  # noqa: BLE001
        _config = {}
    optimizer_key = resolve_strategy_name("optimizer", overrides=overrides, config=_config)
    planner_prov = workflow._context.memory.get("planner_provenance") or {}
    planner_key = planner_prov.get("planner", "unknown")

    header(f"Optimisation  {c(current_artifact.name, CYAN)}")
    step("Optimizer", f"{c(optimizer_key, CYAN)}; search space from "
                      f"planner={c(planner_key, DIM)}, "
                      f"target={c(str(target or 'magnitude'), DIM)}")
    step("Generations", n_gen)
    step("Pop size",    pop)
    if target:
        step("Target", target)
    hr()

    schema_registry = workflow._context.memory.get("schema_registry", {})

    async def _on_gen(gen, best_ind, best_out, fitness):
        fit_str = f"{fitness:.4g}" if fitness != float("inf") else "∞"
        pct = _pct_off(best_out, target, schema_registry) if target else ""
        print(f"  {c(f'gen {gen:>2}', DIM)}  fit={c(fit_str, CYAN)}  {c(pct, YELLOW)}")

    opt_agent = GeneticOptimizerAgent(context=workflow._context)
    ga_result = await opt_agent.run(
        current_artifact, target=target,
        max_generations=n_gen, pop_size=pop,
        on_generation=_on_gen,
    )
    # Record optimizer provenance on the result + session memory.
    optimizer_provenance = {
        "optimizer": optimizer_key,
        "generations": n_gen,
        "population": pop,
        "target": target or None,
        "search_space_from_planner": planner_key,
    }
    if isinstance(ga_result, dict):
        ga_result.setdefault("provenance", optimizer_provenance)
    workflow._context.memory["optimizer_provenance"] = optimizer_provenance
    hr()
    ok(f"Done — {ga_result['generations_run']} generations")
    step("Best inputs",  {k: round(v, 6) for k, v in ga_result["best_inputs"].items()})
    step("Best outputs", {k: round(v, 6) if isinstance(v, float) else v
                          for k, v in ga_result["best_outputs"].items()})
    if target:
        pct = _pct_off(ga_result["best_outputs"], target, schema_registry)
        if pct:
            step("vs target", c(pct, YELLOW))
    if ga_result.get("converged"):
        ok(c("Converged within threshold!", GREEN, BOLD))
    else:
        fit_val = ga_result["best_fitness"]
        step("Best fitness",
             f"{fit_val:.4g}" if fit_val != float("inf") else "∞ (no target key matched)")
    _save_session(workflow, state.primary_goal)
    print()


COMMANDS = [
    SlashCommand(
        name="optimize",
        summary="Run a genetic-algorithm optimizer for the current artifact",
        handler=run,
        args_help="[generations] [pop_size]",
    ),
]
