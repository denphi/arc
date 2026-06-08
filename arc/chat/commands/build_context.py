"""``/build-context`` — inspect or override pre-build context workflows."""

from __future__ import annotations

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, c, err, header, ok, step


async def run(state: ChatState, argv: list[str]) -> None:
    from arc.core.config import build_context_workflow_specs, load_arc_toml

    if not argv:
        runtime = state.memory.get("build_context_workflows")
        try:
            _path, config = load_arc_toml()
        except Exception:
            config = {}
        configured = build_context_workflow_specs(config)
        header("Build-context workflows")
        if runtime is not None:
            step("Active", c(", ".join(runtime) or "none", CYAN) + c("  [session override]", GREEN))
        elif configured:
            step("Active", c(", ".join(spec["name"] for spec in configured), CYAN) + c("  [arc.toml]", DIM))
        else:
            step("Active", c("none", DIM))
        available = state.workflow.registry.list_workflows()
        step("Available", ", ".join(available) if available else "none")
        print(c("  /build-context <workflow> [workflow...] to override, /build-context reset to use arc.toml.", DIM))
        return

    if len(argv) == 1 and argv[0].lower() in {"reset", "clear"}:
        state.memory.pop("build_context_workflows", None)
        state.memory.pop("_build_context_cache", None)
        state.memory.pop("build_context_cache_invalidated", None)
        state.persist()
        ok("Build-context workflow override cleared; using arc.toml.")
        return

    requested = [item for item in argv if item]
    available = set(state.workflow.registry.list_workflows())
    unknown = [name for name in requested if name not in available]
    if unknown:
        err(f"Unknown workflow(s): {', '.join(unknown)}. Available: {', '.join(sorted(available))}")
        return
    state.memory["build_context_workflows"] = requested
    state.memory.pop("_build_context_cache", None)
    state.memory.pop("build_context_cache_invalidated", None)
    state.persist()
    ok(f"Build-context workflows set to {', '.join(requested)}")


COMMANDS = [
    SlashCommand(
        name="build-context",
        summary="Show or set pre-build context workflows.",
        handler=run,
        args_help="[workflow...|reset]",
        aliases=("context",),
    ),
]
