"""ARC interactive chat package.

Public surface
--------------

  * :func:`main`           — entry point invoked by ``arc chat`` (CLI).
  * :func:`chat_loop`      — async REPL coroutine.
  * :func:`print_banner`   — startup banner renderer.
  * :func:`chat_input`     — sync prompt with history (used by tests).
  * :func:`chat_input_async` — async prompt for the REPL.

Submodules (preferred imports for new code)
-------------------------------------------

  * :mod:`arc.chat.classifier` — heuristic + LLM intent classification.
  * :mod:`arc.chat.parsers`    — target / refinement / command parsers.
  * :mod:`arc.chat.router`     — v1 ``route_input`` (default).
  * :mod:`arc.chat.router_v2`  — tool-call router (``ARC_CHAT_V2=1``).
  * :mod:`arc.chat.state`      — :class:`ChatState` dataclass.
  * :mod:`arc.chat.events`     — structured event sinks (Ansi / JSONL).
  * :mod:`arc.chat.plan_mode`  — global plan-mode flag and gate helpers.
  * :mod:`arc.chat.commands`   — slash-command registry & handlers.
  * :mod:`arc.chat.tools`      — Phase 4 tool registry + routing tools.
  * :mod:`arc.chat.agents`     — declarative AgentDefinition loader.
  * :mod:`arc.chat.research`   — Phase / Pipeline + reusable hooks.
  * :mod:`arc.chat.skill_loader` — markdown skill discovery.
  * :mod:`arc.chat.check`      — ``--check`` dry-run report.
  * :mod:`arc.chat.io_utils`   — sim2l health probe + banner + sigint.
  * :mod:`arc.chat.ui`         — ANSI helpers (``c``, ``ok``, ``warn``…).
  * :mod:`arc.chat.input`      — prompt-toolkit wrappers.

The legacy underscored aliases (``_is_question``, ``_parse_target``, …)
remain importable from :mod:`arc.chat` so existing tests / external
call sites keep working. New code should import from the canonical
submodule.

Convention: ``@dataclass(frozen=True)``
---------------------------------------
Several public records in this package use ``frozen=True``
(``Route``, ``CheckItem``, ``SlashCommand``, ``SkillRecord``,
``Tool``, ``ToolDecision``). Frozen prevents reassignment of the
*attributes*, but Python's frozen flag does NOT freeze nested mutable
collections — ``route.args["x"] = "y"`` still works at runtime.

These records are intended as read-only value objects. Callers must
not mutate ``argv``, ``args``, or other inner collections after the
record is constructed. The contract is enforced by convention; the
``tests/test_chat_router.py::test_no_chat_handler_mutates_route_args``
test scans the chat loop for in-place mutation patterns to catch
regressions.
"""

# Public entry points ──────────────────────────────────────────────────────
from arc.chat.loop import (
    main,
    chat_loop,
    print_banner,
    chat_input,
    chat_input_async,
)

# Legacy re-exports (kept for backwards compatibility — prefer the
# canonical submodule imports listed in the docstring above).
from arc.chat.loop import (
    # Classifiers / parsers
    _is_question,
    _llm_classify_intent,
    _is_related_refinement,
    _refinement_needs_artifact_rebuild,
    _parse_target,
    _parse_refinement_target,
    _parse_target_command,
    _normalize_chat_command,
    _build_refined_goal,
    # Adapters / helpers
    _register_artifact_with_sim2l,
    _check_sim2l_services,
    _selected_coder,
    _set_selected_coder,
    _set_session_package_state,
    _available_coding_backends,
    _answer_question,
    _save_session,
    _restore_session,
    # Constants
    _QUESTION_STARTERS,
    _RESEARCH_STARTERS,
    _CONVERSATIONAL,
)

__all__ = [
    "main",
    "chat_loop",
    "print_banner",
    "chat_input",
    "chat_input_async",
]
