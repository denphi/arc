"""Research pipeline — phases, hooks, state.

Public surface:
  * ``PipelineState``    — mutable state passed through every phase
  * ``Phase``            — protocol for individual phases
  * ``Pipeline``         — runs a list of phases with hook dispatch
  * ``PipelineHook``     — before/after/on_error event subscribers
  * ``run_pipeline``     — convenience entry point

Phase classes live in ``arc.chat.research.phases``.
"""

from arc.chat.research.pipeline import (
    Phase,
    Pipeline,
    PipelineHook,
    PipelineState,
    PipelinePhaseError,
    run_pipeline,
)

__all__ = [
    "Phase",
    "Pipeline",
    "PipelineHook",
    "PipelineState",
    "PipelinePhaseError",
    "run_pipeline",
]
