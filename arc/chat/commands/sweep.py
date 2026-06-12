"""``/sweep [id]`` — run the parameter sweep for the current or named artifact."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import CYAN, DIM, GREEN, RED, c, err, header, hr, step


async def run(state: ChatState, argv: list[str]) -> None:
    workflow = state.workflow
    art_id = argv[0] if argv else None
    if not art_id:
        cur = state.current_artifact
        if cur is not None:
            art_id = cur.artifact_id
        else:
            err("Usage: /sweep <artifact_id_or_name>  (or set a goal first)")
            return

    records = workflow.artifacts.list_all()
    artifact = next(
        (r for r in records if r.artifact_id == art_id
         or r.artifact_id.startswith(art_id) or r.name == art_id),
        None,
    )
    if artifact is None:
        err(f"No artifact found matching '{art_id}'.")
        return

    plan = workflow._context.memory.get("current_plan")
    sweep = getattr(plan, "parameter_sweep", {}) if plan else {}
    if not sweep:
        err("No parameter sweep defined for this artifact.")
        return

    header(f"Sweep  {c(artifact.name, CYAN)}")
    # Provenance: which planner designed this sweep (design/todo.md item 8).
    prov = workflow._context.memory.get("planner_provenance") or {}
    planner_key = prov.get("planner", "unknown")
    design = ", ".join(prov.get("experimental_design", [])) or "unspecified"
    step("Sweep source",
         f"planner={c(planner_key, CYAN)}, design={c(design, DIM)}, "
         f"artifact={c(artifact.name, CYAN)}")
    # Sweep points get the same bookkeeping as single runs: audit phases,
    # results save + run_history + memory hooks + backend publish
    # (workflow.record_run), and a provenance entry for the sweep. A
    # *blocking* execution.before audit aborts the sweep, same as it
    # would abort a normal run.
    from arc.runtime.audit import AuditBlockedError

    async def _audit(phase: str, **fields) -> None:
        dispatcher = getattr(workflow, "audit", None)
        if dispatcher is None or not dispatcher.has_actions():
            return
        await dispatcher.dispatch(phase, **fields)

    all_results = []
    run_ids: list[str] = []
    try:
        for param, values in sweep.items():
            for v in values:
                inputs = {param: v}
                await _audit(
                    "execution.before",
                    artifact_id=artifact.artifact_id,
                    input_summary=inputs,
                    payload={"sweep": True},
                )
                execution = await workflow.adapter.run(artifact, inputs)
                await workflow.record_run(artifact, execution, inputs)
                run_ids.append(execution.run_id)
                await _audit(
                    "execution.after",
                    artifact_id=artifact.artifact_id,
                    run_id=execution.run_id,
                    output_summary={"status": execution.status,
                                    "outputs": execution.outputs},
                    payload={"sweep": True},
                )
                row = {param: v, **execution.outputs}
                all_results.append(row)
                cols = "  ".join(
                    f"{k}={val:.4g}" for k, val in row.items()
                    if isinstance(val, (int, float))
                )
                status_c = GREEN if execution.status == "completed" else RED
                log_tail = execution.logs[1] if len(execution.logs) > 1 else ""
                print(f"  {c('●', status_c)} {cols}  {c(log_tail, DIM)}")
    except AuditBlockedError as exc:
        err(f"Sweep blocked by audit: {exc}")
    finally:
        if run_ids:
            workflow.provenance.record(
                workflow.session_id,
                "sweep",
                type(workflow.adapter).__name__,
                artifact_id=artifact.artifact_id,
                inputs={"parameter_sweep": sweep},
                outputs={"runs": len(run_ids), "run_ids": run_ids[:50]},
            )
            try:
                await workflow.publish_provenance()
            except Exception:  # noqa: BLE001 — publishing is best-effort
                pass
    step("Total runs", len(all_results))
    hr()
    print()


COMMANDS = [
    SlashCommand(
        name="sweep",
        summary="Run the parameter sweep for current or named artifact",
        handler=run,
        args_help="[artifact_id_or_name]",
    ),
]
