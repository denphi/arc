"""ChatState (Phase 1) — single state object backing the chat REPL."""

import pytest

from arc.chat.state import ChatState
from tests.fakes import make_workflow, make_artifact


pytestmark = pytest.mark.chat


def test_state_reads_through_to_workflow_memory():
    wf = make_workflow(memory={"primary_goal": "g1", "target": {"k": 1.0}})
    state = ChatState(workflow=wf)
    assert state.primary_goal == "g1"
    assert state.target == {"k": 1.0}
    assert state.refinements == []


def test_state_writes_through_to_workflow_memory():
    wf = make_workflow(memory={})
    state = ChatState(workflow=wf)
    state.primary_goal = "new goal"
    assert wf._context.memory["primary_goal"] == "new goal"


def test_state_setting_none_removes_key():
    wf = make_workflow(memory={"primary_goal": "old"})
    state = ChatState(workflow=wf)
    state.primary_goal = None
    assert "primary_goal" not in wf._context.memory


def test_add_refinement_appends_in_order():
    wf = make_workflow(memory={})
    state = ChatState(workflow=wf)
    state.add_refinement("first")
    state.add_refinement("second")
    assert state.refinements == ["first", "second"]


def test_clear_refinements_removes_list():
    wf = make_workflow(memory={"refinements": ["a", "b"]})
    state = ChatState(workflow=wf)
    state.clear_refinements()
    assert state.refinements == []
    assert "refinements" not in wf._context.memory


def test_target_setter_handles_empty_dict_as_clear():
    wf = make_workflow(memory={"target": {"k": 1.0}})
    state = ChatState(workflow=wf)
    state.target = {}
    assert "target" not in wf._context.memory


def test_reset_for_new_goal_clears_session_state():
    wf = make_workflow(memory={
        "primary_goal": "old",
        "current_artifact": "X",
        "current_plan": "P",
        "run_history": ["a"],
        "next_parameters": {"k": 1},
        "refinements": ["r"],
        "schema_registry": {"s": "preserved"},  # should NOT be cleared
    })
    state = ChatState(workflow=wf)
    state.reset_for_new_goal("brand new")

    assert state.primary_goal == "brand new"
    for key in ("current_artifact", "current_plan", "run_history",
                "next_parameters", "refinements"):
        assert key not in wf._context.memory
    # schema_registry is intentionally preserved across goal resets
    assert wf._context.memory["schema_registry"] == {"s": "preserved"}


def test_has_active_goal_true_only_when_both_set():
    wf = make_workflow(memory={})
    state = ChatState(workflow=wf)
    assert not state.has_active_goal()

    state.primary_goal = "g"
    assert not state.has_active_goal()  # no artifact yet

    state.current_artifact = make_artifact()
    assert state.has_active_goal()


def test_default_permission_mode_is_default():
    wf = make_workflow()
    state = ChatState(workflow=wf)
    assert state.permission_mode == "default"


def test_default_router_call_budget_zero():
    wf = make_workflow()
    state = ChatState(workflow=wf)
    assert state.router_calls == 0
