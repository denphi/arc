"""Strategy resolver — precedence + dynamic registration + /strategy command.

The resolver picks which class backs each research role using a fixed
precedence: runtime override > env var > ``arc.toml`` > bundled default.
These tests pin every level of that chain so future refactors can't
silently flip the order.
"""

from __future__ import annotations

import pytest

from arc.core import strategies
from arc.core.strategies import (
    StrategySpec,
    default_strategy,
    known_roles,
    list_strategies,
    parse_strategy_names,
    register_strategy,
    resolve_role,
    resolve_strategy_name,
)


pytestmark = pytest.mark.chat


# ── Catalogue + precedence ─────────────────────────────────────────────


def test_known_roles_include_core_loop_roles():
    expected = {"ideator", "planner", "reviewer", "reflector", "curator", "optimizer"}
    assert expected.issubset(set(known_roles()))


def test_default_strategy_returns_default_for_known_role():
    assert default_strategy("planner") == "default"


def test_default_strategy_returns_none_for_unknown_role():
    assert default_strategy("nonexistent") is None


def test_resolve_strategy_name_falls_back_to_default():
    assert resolve_strategy_name("planner") == "default"


def test_resolve_strategy_name_honors_config():
    config = {"strategies": {"planner": "mars_planner"}}
    assert resolve_strategy_name("planner", config=config) == "mars_planner"


def test_resolve_strategy_name_env_override_beats_config(monkeypatch):
    config = {"strategies": {"planner": "mars_planner"}}
    monkeypatch.setenv("ARC_STRATEGY_PLANNER", "default")
    assert resolve_strategy_name("planner", config=config) == "default"


def test_resolve_strategy_name_runtime_override_beats_env(monkeypatch):
    config = {"strategies": {"planner": "mars_planner"}}
    monkeypatch.setenv("ARC_STRATEGY_PLANNER", "default")
    assert resolve_strategy_name(
        "planner",
        overrides={"planner": "mars_planner"},
        config=config,
    ) == "mars_planner"


def test_resolve_strategy_name_unknown_role_returns_empty():
    assert resolve_strategy_name("not_a_role") == ""


def test_parse_strategy_names_accepts_friendly_separators():
    assert parse_strategy_names("default+embeddings materials_project,github") == [
        "default",
        "embeddings",
        "materials_project",
        "github",
    ]


# ── Class loading ──────────────────────────────────────────────────────


def test_resolve_role_returns_class_for_default():
    cls = resolve_role("ideator")
    assert cls.__name__ == "IdeatorAgent"


def test_resolve_role_returns_class_for_optimizer_default():
    cls = resolve_role("optimizer")
    assert cls.__name__ == "GeneticOptimizerAgent"


def test_resolve_role_raises_on_unknown_role():
    with pytest.raises(KeyError):
        resolve_role("not_a_role")


def test_resolve_role_falls_back_to_default_on_unknown_strategy(caplog):
    """An unknown override (typo) must warn + fall back, never crash."""
    cls = resolve_role("planner", overrides={"planner": "does_not_exist"})
    assert cls.__name__ == "PlannerAgent"
    assert any("falling back" in r.getMessage() for r in caplog.records)


def test_resolve_role_returns_composite_searcher_for_stack():
    cls = resolve_role(
        "searcher",
        overrides={"searcher": "default embeddings materials_project"},
    )
    assert cls.__name__.startswith("CompositeSearcher_")
    assert cls.strategy_names == ("default", "embeddings", "materials_project")


def test_resolve_role_non_searcher_stack_falls_back_to_default(caplog):
    cls = resolve_role("planner", overrides={"planner": "default mars_planner"})
    assert cls.__name__ == "PlannerAgent"
    assert any("Composite strategies are not supported" in r.getMessage()
               for r in caplog.records)


# ── Dynamic registration ──────────────────────────────────────────────


def test_register_strategy_appends_new_entry():
    spec = StrategySpec(
        name="test_planner_alt",
        package_dir="arc-sim2l",
        module_path="agents/planner.py",
        attr="PlannerAgent",
        description="testing alt registration",
    )
    register_strategy("planner", spec)
    names = {s.name for s in list_strategies("planner")}
    assert "test_planner_alt" in names
    assert default_strategy("planner") == "default"  # default not changed

    # Clean up so other tests aren't affected.
    role_catalogue = strategies._ROLE_CATALOGUE
    cur_default, current = role_catalogue["planner"]
    role_catalogue["planner"] = (
        cur_default,
        tuple(s for s in current if s.name != "test_planner_alt"),
    )


def test_register_strategy_with_make_default_shifts_default():
    spec = StrategySpec(
        name="test_default_alt",
        package_dir="arc-sim2l",
        module_path="agents/planner.py",
        attr="PlannerAgent",
    )
    original = default_strategy("planner")
    register_strategy("planner", spec, make_default=True)
    assert default_strategy("planner") == "test_default_alt"

    # Restore.
    role_catalogue = strategies._ROLE_CATALOGUE
    _cur_default, current = role_catalogue["planner"]
    role_catalogue["planner"] = (
        original,
        tuple(s for s in current if s.name != "test_default_alt"),
    )


# ── packages.resolve_role wrapper ─────────────────────────────────────


def test_packages_resolve_role_reads_workflow_overrides():
    """The arc.packages wrapper pulls overrides off ``workflow.memory``."""
    from types import SimpleNamespace

    from arc.packages import resolve_role as pkg_resolve_role

    workflow = SimpleNamespace(
        _context=SimpleNamespace(
            memory={"strategy_overrides": {"planner": "default"}},
        ),
    )
    cls = pkg_resolve_role("planner", workflow)
    assert cls.__name__ == "PlannerAgent"


def test_packages_resolve_role_works_without_workflow():
    from arc.packages import resolve_role as pkg_resolve_role
    cls = pkg_resolve_role("reviewer")
    assert cls.__name__ == "ReviewerAgent"


# ── /strategy slash command ───────────────────────────────────────────


def test_strategy_command_list_all_does_not_crash():
    """``/strategy`` with no args prints role table — must not raise."""
    import asyncio
    from arc.chat.commands.strategy import run
    from tests.fakes import make_workflow

    workflow = make_workflow()
    from arc.chat.state import ChatState
    state = ChatState(workflow=workflow)
    asyncio.run(run(state, []))


def test_strategy_command_lists_strategies_for_role():
    import asyncio
    from arc.chat.commands.strategy import run
    from tests.fakes import make_workflow
    from arc.chat.state import ChatState

    state = ChatState(workflow=make_workflow())
    asyncio.run(run(state, ["planner"]))


def test_strategy_command_sets_session_override(monkeypatch):
    """``/strategy planner default`` records the override in memory."""
    import asyncio
    from arc.chat.commands.strategy import run
    from tests.fakes import make_workflow
    from arc.chat.state import ChatState

    workflow = make_workflow()
    state = ChatState(workflow=workflow)
    # Suppress disk write — persist() reaches into arc.chat.loop._save_session
    monkeypatch.setattr(ChatState, "persist", lambda self: None)

    asyncio.run(run(state, ["planner", "default"]))
    assert state.memory["strategy_overrides"]["planner"] == "default"


def test_strategy_command_sets_space_separated_stack(monkeypatch):
    import asyncio
    from arc.chat.commands.strategy import run
    from tests.fakes import make_workflow
    from arc.chat.state import ChatState

    workflow = make_workflow()
    state = ChatState(workflow=workflow)
    monkeypatch.setattr(ChatState, "persist", lambda self: None)

    asyncio.run(run(state, ["searcher", "default", "embeddings", "materials_project"]))
    assert (
        state.memory["strategy_overrides"]["searcher"]
        == "default embeddings materials_project"
    )


def test_strategy_command_reset_clears_override(monkeypatch):
    import asyncio
    from arc.chat.commands.strategy import run
    from tests.fakes import make_workflow
    from arc.chat.state import ChatState

    workflow = make_workflow(memory={"strategy_overrides": {"planner": "default"}})
    state = ChatState(workflow=workflow)
    monkeypatch.setattr(ChatState, "persist", lambda self: None)

    asyncio.run(run(state, ["planner", "reset"]))
    assert "strategy_overrides" not in state.memory


def test_strategy_command_rejects_unknown_strategy(monkeypatch):
    """A typo in the impl name must not silently set a bogus override."""
    import asyncio
    from arc.chat.commands.strategy import run
    from tests.fakes import make_workflow
    from arc.chat.state import ChatState

    workflow = make_workflow()
    state = ChatState(workflow=workflow)
    monkeypatch.setattr(ChatState, "persist", lambda self: None)

    asyncio.run(run(state, ["planner", "no_such_strategy"]))
    assert "strategy_overrides" not in state.memory


def test_strategy_command_rejects_unknown_role(monkeypatch):
    import asyncio
    from arc.chat.commands.strategy import run
    from tests.fakes import make_workflow
    from arc.chat.state import ChatState

    workflow = make_workflow()
    state = ChatState(workflow=workflow)
    monkeypatch.setattr(ChatState, "persist", lambda self: None)

    asyncio.run(run(state, ["not_a_role"]))
    assert "strategy_overrides" not in state.memory
