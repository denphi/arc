"""Recipe auto-suggest: triggers + the chat-loop prompt that surfaces them.

Two layers covered:

  * :func:`arc.core.recipe_suggest.suggest_recipe` — the pure picker.
    Given a ResearchGoal-shaped object and the session memory dict,
    returns the first matching ``Suggestion`` (or None).
  * :func:`arc.chat.loop._maybe_suggest_recipe` — the wiring that
    consults the picker, prints the offer, prompts y/N, and applies
    the recipe on accept.

Both layers must:

  * Match recipes only on declared ``triggers:`` (no triggers ⇒ never).
  * Never re-suggest a recipe already offered in this session.
  * Never suggest the recipe that's already active.
  * Be silent (return None / print nothing) when no recipe matches.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arc.schemas.research import ResearchGoal


pytestmark = pytest.mark.chat


# ── Test fixtures: a clean temporary recipes dir ────────────────────────


@pytest.fixture
def isolated_recipes(tmp_path, monkeypatch):
    """Replace bundled + user recipe dirs with a tmp dir so tests own
    the entire catalogue and don't depend on shipped recipes."""
    from arc.core import recipes as _recipes

    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()

    monkeypatch.setattr(_recipes, "_bundled_recipes_dir", lambda: bundled)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: user)

    def _write(name: str, body: str) -> None:
        (bundled / f"{name}.yaml").write_text(textwrap.dedent(body))

    return SimpleNamespace(bundled=bundled, user=user, write=_write)


# ── suggest_recipe: trigger matching ───────────────────────────────────


def test_suggest_returns_none_when_no_recipes_match(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("no-triggers", """
    name: no-triggers
    description: a recipe with no triggers block
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="anything", domain="materials")
    assert suggest_recipe(goal, {}) is None


def test_suggest_matches_on_domain(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("mat-only", """
    name: mat-only
    description: ""
    triggers:
      - domains: [materials]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    s = suggest_recipe(goal, {})
    assert s is not None
    assert s.recipe.name == "mat-only"
    assert "materials" in s.reason


def test_suggest_does_not_match_when_domain_differs(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("mat-only", """
    name: mat-only
    description: ""
    triggers:
      - domains: [materials]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="chemistry")
    assert suggest_recipe(goal, {}) is None


def test_suggest_matches_on_goal_keyword(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("bayes", """
    name: bayes
    description: ""
    triggers:
      - goal_keywords: [bayesian]
    strategies:
      optimizer: bayesopt
    """)
    goal = ResearchGoal(goal="run a Bayesian sweep", domain="materials")
    s = suggest_recipe(goal, {})
    assert s is not None
    assert s.recipe.name == "bayes"


def test_suggest_matches_on_target_key(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("with-target", """
    name: with-target
    description: ""
    triggers:
      - target_keys: [bandgap_ev]
    strategies:
      optimizer: bayesopt
    """)
    goal = ResearchGoal(goal="x", domain="materials", target={"bandgap_ev": 1.1})
    s = suggest_recipe(goal, {})
    assert s is not None
    assert s.recipe.name == "with-target"


def test_suggest_matches_on_element(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("si-only", """
    name: si-only
    description: ""
    triggers:
      - elements: [Si]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="design a silicon nanowire", domain="materials")
    s = suggest_recipe(goal, {})
    assert s is not None


def test_suggest_requires_all_conditions_in_one_trigger(isolated_recipes):
    """All keys in a single trigger entry are AND-ed."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("strict", """
    name: strict
    description: ""
    triggers:
      - domains: [materials]
        target_keys: [bandgap_ev]
    strategies:
      optimizer: bayesopt
    """)
    # Domain matches but target missing.
    g1 = ResearchGoal(goal="x", domain="materials")
    assert suggest_recipe(g1, {}) is None
    # Target matches but domain wrong.
    g2 = ResearchGoal(goal="x", domain="chemistry", target={"bandgap_ev": 1.1})
    assert suggest_recipe(g2, {}) is None
    # Both → match.
    g3 = ResearchGoal(goal="x", domain="materials", target={"bandgap_ev": 1.1})
    assert suggest_recipe(g3, {}) is not None


def test_suggest_or_across_trigger_list(isolated_recipes):
    """Trigger entries are OR-ed: matching any one fires the suggestion."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("either", """
    name: either
    description: ""
    triggers:
      - domains: [chemistry]
      - domains: [materials]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    assert suggest_recipe(goal, {}) is not None


def test_suggest_skips_already_suggested(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("once", """
    name: once
    description: ""
    triggers:
      - domains: [materials]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    memory = {"recipe_suggested": ["once"]}
    assert suggest_recipe(goal, memory) is None


def test_suggest_skips_active_recipe(isolated_recipes):
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("active-one", """
    name: active-one
    description: ""
    triggers:
      - domains: [materials]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    assert suggest_recipe(goal, {"active_recipe": "active-one"}) is None


def test_suggest_picks_first_match_in_catalogue_order(isolated_recipes):
    """When multiple recipes could match, return the first one
    (sorted alphabetically by list_recipes)."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("aaa", """
    name: aaa
    description: ""
    triggers:
      - domains: [materials]
    strategies:
      optimizer: default
    """)
    isolated_recipes.write("zzz", """
    name: zzz
    description: ""
    triggers:
      - domains: [materials]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    s = suggest_recipe(goal, {})
    assert s is not None
    assert s.recipe.name == "aaa"


def test_remember_suggestion_appends_uniquely():
    from arc.core.recipe_suggest import remember_suggestion
    memory: dict = {}
    remember_suggestion(memory, "foo")
    remember_suggestion(memory, "bar")
    remember_suggestion(memory, "foo")  # dup
    assert memory["recipe_suggested"] == ["foo", "bar"]


# ── Bundled recipes' triggers ─────────────────────────────────────────


def test_bundled_mp_discovery_suggests_on_materials_with_target():
    """End-to-end against the real bundled mp-discovery recipe."""
    from arc.core.recipe_suggest import suggest_recipe

    goal = ResearchGoal(
        goal="design a silicon nanowire", domain="materials",
        target={"bandgap_ev": 1.1},
    )
    s = suggest_recipe(goal, {})
    assert s is not None
    # mp-discovery should win over bayesian-materials here because it
    # matches the target_keys trigger which is stronger evidence.
    assert s.recipe.name == "mp-discovery"


def test_bundled_recipes_do_not_suggest_outside_materials():
    """An ornithology goal must not get a materials recipe."""
    from arc.core.recipe_suggest import suggest_recipe

    goal = ResearchGoal(goal="study bird flight", domain="ornithology")
    assert suggest_recipe(goal, {}) is None


# ── _maybe_suggest_recipe loop wiring ──────────────────────────────────


def _run_maybe_suggest(answer: str, *, ctx_memory: dict, goal: ResearchGoal):
    """Drive _maybe_suggest_recipe with a canned chat input."""
    from arc.chat.loop import _maybe_suggest_recipe

    async def _fake_input(prompt: str) -> str:
        return answer

    with patch("arc.chat.loop.chat_input_async", _fake_input):
        ctx = SimpleNamespace(memory=ctx_memory)
        workflow = SimpleNamespace(_context=ctx)
        asyncio.run(_maybe_suggest_recipe(workflow, goal, ctx))


def test_maybe_suggest_applies_recipe_on_yes():
    goal = ResearchGoal(
        goal="design a silicon bandgap material", domain="materials",
        target={"bandgap_ev": 1.1},
    )
    memory: dict = {}
    _run_maybe_suggest("y", ctx_memory=memory, goal=goal)

    assert memory.get("active_recipe") == "mp-discovery"
    assert "strategy_overrides" in memory
    # mp-discovery sets the searcher to materials_project.
    assert memory["strategy_overrides"]["searcher"] == "materials_project"
    # And remembers we offered it.
    assert "mp-discovery" in memory["recipe_suggested"]


def test_maybe_suggest_skips_on_no():
    goal = ResearchGoal(
        goal="design a silicon bandgap material", domain="materials",
        target={"bandgap_ev": 1.1},
    )
    memory: dict = {}
    _run_maybe_suggest("n", ctx_memory=memory, goal=goal)

    assert "active_recipe" not in memory
    assert "strategy_overrides" not in memory
    # But we still remember not to ask again.
    assert "mp-discovery" in memory["recipe_suggested"]


def test_maybe_suggest_empty_answer_treated_as_no():
    """Pressing Enter without typing anything declines the suggestion."""
    goal = ResearchGoal(
        goal="design a silicon bandgap material", domain="materials",
        target={"bandgap_ev": 1.1},
    )
    memory: dict = {}
    _run_maybe_suggest("", ctx_memory=memory, goal=goal)
    assert "active_recipe" not in memory
    assert "mp-discovery" in memory["recipe_suggested"]


def test_maybe_suggest_is_noop_when_no_match():
    """A goal that matches no recipe leaves memory untouched."""
    from arc.chat.loop import _maybe_suggest_recipe

    async def _fake_input(prompt: str) -> str:
        pytest.fail("must not prompt when there's no suggestion")

    with patch("arc.chat.loop.chat_input_async", _fake_input):
        goal = ResearchGoal(goal="bird study", domain="ornithology")
        memory: dict = {}
        ctx = SimpleNamespace(memory=memory)
        workflow = SimpleNamespace(_context=ctx)
        asyncio.run(_maybe_suggest_recipe(workflow, goal, ctx))

    assert memory == {}


def test_maybe_suggest_never_repeats_in_one_session():
    """Two run_research calls with the same goal → one prompt only."""
    goal = ResearchGoal(
        goal="design a silicon bandgap material", domain="materials",
        target={"bandgap_ev": 1.1},
    )
    memory: dict = {}
    prompts_seen: list[int] = []

    async def _fake_input(prompt: str) -> str:
        prompts_seen.append(1)
        return "n"

    from arc.chat.loop import _maybe_suggest_recipe

    with patch("arc.chat.loop.chat_input_async", _fake_input):
        ctx = SimpleNamespace(memory=memory)
        workflow = SimpleNamespace(_context=ctx)
        asyncio.run(_maybe_suggest_recipe(workflow, goal, ctx))
        asyncio.run(_maybe_suggest_recipe(workflow, goal, ctx))
        asyncio.run(_maybe_suggest_recipe(workflow, goal, ctx))

    assert len(prompts_seen) == 1


def test_maybe_suggest_skips_when_recipe_already_active():
    """If the user already applied a recipe, we don't pester."""
    goal = ResearchGoal(
        goal="design a silicon bandgap material", domain="materials",
        target={"bandgap_ev": 1.1},
    )
    memory = {"active_recipe": "mp-discovery"}

    async def _fake_input(prompt: str) -> str:
        pytest.fail("must not prompt when the recipe is already active")

    from arc.chat.loop import _maybe_suggest_recipe

    with patch("arc.chat.loop.chat_input_async", _fake_input):
        ctx = SimpleNamespace(memory=memory)
        workflow = SimpleNamespace(_context=ctx)
        asyncio.run(_maybe_suggest_recipe(workflow, goal, ctx))


def test_maybe_suggest_does_not_overwrite_manual_strategy_override():
    """If the user already manually picked a strategy, accepting the
    suggestion preserves their choice (per apply_recipe semantics)."""
    goal = ResearchGoal(
        goal="design a silicon bandgap material", domain="materials",
        target={"bandgap_ev": 1.1},
    )
    # User already pinned searcher=embeddings before any suggestion fired.
    memory = {"strategy_overrides": {"searcher": "embeddings"}}
    _run_maybe_suggest("y", ctx_memory=memory, goal=goal)

    # Recipe applied, but the manual searcher choice stuck around.
    assert memory["strategy_overrides"]["searcher"] == "embeddings"
    # Other roles the recipe declared still went through.
    assert memory["strategy_overrides"]["validator"] == "materials_evaluators"


# ── New triggers: output_keys / min_iteration / requires_provider ─────


def test_suggest_matches_on_output_key_in_last_run(isolated_recipes):
    """``output_keys`` fires when the most recent run produced any
    listed key — even if the goal's ``target`` dict is empty."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("on-output", """
    name: on-output
    description: ""
    triggers:
      - output_keys: [bandgap_ev]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="study x", domain="materials")
    memory = {
        "run_history": [
            {"inputs": {"x": 1}, "outputs": {"bandgap_ev": 1.1}, "status": "completed"},
        ],
    }
    s = suggest_recipe(goal, memory)
    assert s is not None
    assert s.recipe.name == "on-output"
    assert "bandgap_ev" in s.reason


def test_suggest_output_keys_does_not_fire_when_history_empty(isolated_recipes):
    """First iteration → ``output_keys`` produces no match."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("on-output", """
    name: on-output
    description: ""
    triggers:
      - output_keys: [bandgap_ev]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    assert suggest_recipe(goal, {}) is None
    assert suggest_recipe(goal, {"run_history": []}) is None


def test_suggest_output_keys_only_consults_latest_run(isolated_recipes):
    """A historic run shouldn't fire after later runs cleared the key."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("on-output", """
    name: on-output
    description: ""
    triggers:
      - output_keys: [bandgap_ev]
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    memory = {
        "run_history": [
            {"inputs": {}, "outputs": {"bandgap_ev": 1.1}, "status": "completed"},
            # Latest run produced a different output — trigger shouldn't fire.
            {"inputs": {}, "outputs": {"glass_transition_k": 450}, "status": "completed"},
        ],
    }
    assert suggest_recipe(goal, memory) is None


def test_suggest_min_iteration_below_threshold(isolated_recipes):
    """``min_iteration: 3`` must not fire at iteration 2."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("late", """
    name: late
    description: ""
    triggers:
      - domains: [materials]
        min_iteration: 3
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    assert suggest_recipe(goal, {"iteration": 2}) is None


def test_suggest_min_iteration_at_threshold(isolated_recipes):
    """At iteration == threshold, the trigger fires."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("late", """
    name: late
    description: ""
    triggers:
      - domains: [materials]
        min_iteration: 3
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    s = suggest_recipe(goal, {"iteration": 3})
    assert s is not None
    assert s.recipe.name == "late"
    assert "iteration" in s.reason


def test_suggest_min_iteration_falls_back_to_history_length(isolated_recipes):
    """When ``iteration`` isn't explicitly set, fall back to
    len(run_history)."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("late", """
    name: late
    description: ""
    triggers:
      - domains: [materials]
        min_iteration: 2
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    memory = {"run_history": [{}, {}]}  # length 2 → at threshold
    assert suggest_recipe(goal, memory) is not None


def test_suggest_min_iteration_with_non_integer_value(isolated_recipes):
    """A garbage ``min_iteration`` YAML value should not suppress the
    suggestion — treat it as no threshold."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("gross", """
    name: gross
    description: ""
    triggers:
      - domains: [materials]
        min_iteration: "not a number"
    strategies:
      optimizer: default
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    # min_iteration=0 effectively disables the threshold, but we still
    # need at least one other condition (domains) to fire.
    assert suggest_recipe(goal, {"iteration": 0}) is not None


def test_suggest_requires_provider_with_provider(isolated_recipes):
    """``requires_provider: true`` + provider configured → fires."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("llm", """
    name: llm
    description: ""
    triggers:
      - domains: [materials]
        requires_provider: true
    strategies:
      reviewer: reflective
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    s = suggest_recipe(goal, {"provider": object()})
    assert s is not None
    assert s.recipe.name == "llm"
    assert "LLM provider" in s.reason


def test_suggest_requires_provider_without_provider(isolated_recipes):
    """No provider → trigger is suppressed even when other conditions match."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("llm", """
    name: llm
    description: ""
    triggers:
      - domains: [materials]
        requires_provider: true
    strategies:
      reviewer: reflective
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    assert suggest_recipe(goal, {}) is None
    assert suggest_recipe(goal, {"provider": None}) is None


def test_suggest_combined_new_and_existing_triggers(isolated_recipes):
    """All conditions in one trigger entry AND together."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("combo", """
    name: combo
    description: ""
    triggers:
      - domains: [materials]
        output_keys: [bandgap_ev]
        min_iteration: 2
        requires_provider: true
    strategies:
      optimizer: bayesopt
    """)
    goal = ResearchGoal(goal="x", domain="materials")
    history_with_bandgap = [
        {"inputs": {}, "outputs": {"bandgap_ev": 1.0}, "status": "completed"},
        {"inputs": {}, "outputs": {"bandgap_ev": 1.05}, "status": "completed"},
    ]

    # Missing provider → no match.
    assert suggest_recipe(goal, {
        "iteration": 2,
        "run_history": history_with_bandgap,
    }) is None

    # Missing output key → no match.
    assert suggest_recipe(goal, {
        "iteration": 2,
        "provider": object(),
        "run_history": [{"inputs": {}, "outputs": {"x": 1}, "status": "completed"}] * 2,
    }) is None

    # All four conditions satisfied → match.
    s = suggest_recipe(goal, {
        "iteration": 2,
        "provider": object(),
        "run_history": history_with_bandgap,
    })
    assert s is not None
    assert s.recipe.name == "combo"


def test_existing_recipes_keep_their_behaviour(isolated_recipes):
    """No-regression for recipes that don't use the new keys: the
    new code path is invisible to them."""
    from arc.core.recipe_suggest import suggest_recipe

    isolated_recipes.write("legacy", """
    name: legacy
    description: ""
    triggers:
      - domains: [materials]
        target_keys: [bandgap_ev]
    strategies:
      planner: default
    """)
    goal = ResearchGoal(goal="x", domain="materials", target={"bandgap_ev": 1.1})
    s = suggest_recipe(goal, {})
    assert s is not None
    assert s.recipe.name == "legacy"


# ── Bundled mp-discovery now also matches via output_keys ─────────────


def test_bundled_mp_discovery_fires_on_output_key_after_two_iterations():
    """A user without a target but whose runs produced ``band_gap``
    twice should see the mp-discovery suggestion (the new output_keys
    trigger). Uses the actually-shipped recipe."""
    from arc.core.recipe_suggest import suggest_recipe

    goal = ResearchGoal(goal="design something", domain="materials")
    memory = {
        "iteration": 2,
        "run_history": [
            {"inputs": {}, "outputs": {"band_gap": 0.6}, "status": "completed"},
            {"inputs": {}, "outputs": {"band_gap": 0.7}, "status": "completed"},
        ],
    }
    s = suggest_recipe(goal, memory)
    assert s is not None
    assert s.recipe.name == "mp-discovery"
    assert "output 'band_gap'" in s.reason


def test_bundled_mp_discovery_output_trigger_does_not_fire_first_iteration():
    """Same recipe, but iteration<2 → the new trigger waits."""
    from arc.core.recipe_suggest import suggest_recipe

    goal = ResearchGoal(goal="design something", domain="materials")
    memory = {
        "iteration": 1,
        "run_history": [
            {"inputs": {}, "outputs": {"band_gap": 0.6}, "status": "completed"},
        ],
    }
    # Should not fire: no target_keys/keywords, and min_iteration=2 not met.
    assert suggest_recipe(goal, memory) is None
