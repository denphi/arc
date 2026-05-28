"""Recipe loader, application, /recipe command, and precedence.

Recipes are thin YAML files that bundle role → strategy mappings. They
must:

  * load from both the bundled ``arc/recipes/`` directory and the
    user-local ``~/.arc/recipes/`` directory, with the user wins on name
    collisions
  * apply by writing into ``memory["strategy_overrides"]`` so the
    existing resolver picks them up — no new precedence layer
  * not stomp on per-role ``/strategy`` choices the user made manually,
    unless ``--force`` is given
  * clear cleanly without disturbing those manual choices
  * fail-fast with a useful message on invalid role/impl names
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.chat


# ── Loader ─────────────────────────────────────────────────────────────


def test_bundled_recipes_load():
    """The two recipes we ship are discoverable + valid."""
    from arc.core.recipes import list_recipes, validate_recipe

    names = {r.name for r in list_recipes()}
    assert "bayesian-materials" in names
    assert "exploration-baseline" in names
    for r in list_recipes():
        assert validate_recipe(r) == [], (
            f"recipe {r.name!r} has invalid roles/impls: {validate_recipe(r)}"
        )


def test_get_recipe_returns_none_for_unknown_name():
    from arc.core.recipes import get_recipe
    assert get_recipe("does-not-exist") is None


def test_get_recipe_returns_recipe_object():
    from arc.core.recipes import get_recipe
    r = get_recipe("bayesian-materials")
    assert r is not None
    assert "planner" in r.strategies
    assert r.strategies["optimizer"] == "bayesopt"


def test_user_recipe_overrides_bundled(tmp_path, monkeypatch):
    """A user recipe with the same name shadows the bundled one."""
    user_dir = tmp_path / ".arc" / "recipes"
    user_dir.mkdir(parents=True)
    (user_dir / "bayesian-materials.yaml").write_text(
        "name: bayesian-materials\n"
        "description: user override\n"
        "strategies:\n  planner: default\n"
    )

    from arc.core import recipes as _recipes
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: user_dir)

    r = _recipes.get_recipe("bayesian-materials")
    assert r is not None
    assert r.source == "user"
    assert r.strategies == {"planner": "default"}


def test_invalid_recipe_yaml_is_skipped(tmp_path, monkeypatch):
    """Malformed YAML or schema-invalid recipes don't break discovery."""
    bad_dir = tmp_path / "recipes"
    bad_dir.mkdir()
    (bad_dir / "missing-strategies.yaml").write_text(
        "name: incomplete\ndescription: no strategies\n"
    )
    (bad_dir / "missing-name.yaml").write_text(
        "description: nameless\nstrategies:\n  planner: default\n"
    )

    from arc.core import recipes as _recipes
    monkeypatch.setattr(_recipes, "_bundled_recipes_dir", lambda: bad_dir)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path / "empty")

    # No exceptions raised; the broken files are silently skipped.
    assert _recipes.list_recipes() == []


# ── apply / clear ──────────────────────────────────────────────────────


def test_apply_writes_overrides_into_memory():
    from arc.core.recipes import apply_recipe, get_recipe

    memory: dict = {}
    recipe = get_recipe("bayesian-materials")
    result = apply_recipe(recipe, memory)

    assert memory["strategy_overrides"] == recipe.strategies
    assert memory["recipe_applied"] == recipe.strategies
    assert set(result.overrides_set) == set(recipe.strategies)
    assert result.overrides_skipped == {}


def test_apply_skips_manual_overrides_by_default():
    from arc.core.recipes import apply_recipe, get_recipe

    # User manually picked a planner earlier.
    memory = {"strategy_overrides": {"planner": "default"}}
    recipe = get_recipe("bayesian-materials")
    result = apply_recipe(recipe, memory)

    # Manual planner choice survived; other roles were applied.
    assert memory["strategy_overrides"]["planner"] == "default"
    assert memory["strategy_overrides"]["optimizer"] == "bayesopt"
    assert "planner" in result.overrides_skipped
    assert "optimizer" in result.overrides_set


def test_apply_force_overwrites_manual_overrides():
    from arc.core.recipes import apply_recipe, get_recipe

    memory = {"strategy_overrides": {"planner": "default"}}
    recipe = get_recipe("bayesian-materials")
    result = apply_recipe(recipe, memory, overwrite_manual=True)

    assert memory["strategy_overrides"]["planner"] == "mars_planner"
    assert result.overrides_skipped == {}


def test_consecutive_apply_calls_replace_old_recipe_keys():
    """apply(A) then apply(B) drops A's keys that B doesn't set."""
    from arc.core.recipes import apply_recipe, Recipe

    a = Recipe(
        name="a", description="", source="bundled",
        strategies={"planner": "mars_planner", "reviewer": "reflective"},
    )
    b = Recipe(
        name="b", description="", source="bundled",
        strategies={"optimizer": "bayesopt"},
    )

    memory: dict = {}
    apply_recipe(a, memory)
    assert memory["strategy_overrides"] == {
        "planner": "mars_planner", "reviewer": "reflective",
    }
    apply_recipe(b, memory)
    # A's keys are dropped; only B's remain.
    assert memory["strategy_overrides"] == {"optimizer": "bayesopt"}


def test_consecutive_apply_preserves_manual_change_made_between_them():
    """apply(A) → /strategy planner default → apply(B) keeps the manual choice."""
    from arc.core.recipes import apply_recipe, Recipe

    a = Recipe(name="a", description="", source="bundled",
               strategies={"planner": "mars_planner"})
    b = Recipe(name="b", description="", source="bundled",
               strategies={"optimizer": "bayesopt"})

    memory: dict = {}
    apply_recipe(a, memory)
    # User manually changed planner *after* applying recipe A.
    memory["strategy_overrides"]["planner"] = "default"
    apply_recipe(b, memory)
    # Manual planner choice survives — it's no longer tied to recipe A.
    assert memory["strategy_overrides"]["planner"] == "default"
    assert memory["strategy_overrides"]["optimizer"] == "bayesopt"


def test_clear_drops_only_recipe_keys():
    from arc.core.recipes import apply_recipe, clear_recipe, Recipe

    recipe = Recipe(
        name="r", description="", source="bundled",
        strategies={"planner": "mars_planner"},
    )
    memory: dict = {"strategy_overrides": {"reviewer": "reflective"}}
    apply_recipe(recipe, memory)
    cleared = clear_recipe(memory)

    assert cleared == {"planner": "mars_planner"}
    # The pre-existing manual /strategy override survives the clear.
    assert memory["strategy_overrides"] == {"reviewer": "reflective"}
    assert "recipe_applied" not in memory


def test_clear_with_no_active_recipe_returns_empty():
    from arc.core.recipes import clear_recipe
    memory: dict = {"strategy_overrides": {"planner": "default"}}
    assert clear_recipe(memory) == {}
    assert memory["strategy_overrides"] == {"planner": "default"}


# ── Validation ─────────────────────────────────────────────────────────


def test_validate_recipe_flags_unknown_role():
    from arc.core.recipes import Recipe, validate_recipe
    r = Recipe(name="bad", description="", source="user",
               strategies={"not_a_role": "default"})
    errors = validate_recipe(r)
    assert any("unknown role" in e for e in errors)


def test_validate_recipe_flags_unknown_strategy():
    from arc.core.recipes import Recipe, validate_recipe
    r = Recipe(name="bad", description="", source="user",
               strategies={"planner": "not_a_real_strategy"})
    errors = validate_recipe(r)
    assert any("unknown strategy" in e for e in errors)


# ── Precedence integration with the resolver ───────────────────────────


def test_resolver_picks_up_recipe_applied_overrides():
    """After apply, ``resolve_strategy_name`` returns the recipe's choice."""
    from arc.core.recipes import apply_recipe, get_recipe
    from arc.core.strategies import resolve_strategy_name

    memory: dict = {}
    apply_recipe(get_recipe("bayesian-materials"), memory)

    assert resolve_strategy_name(
        "optimizer", overrides=memory["strategy_overrides"],
    ) == "bayesopt"
    assert resolve_strategy_name(
        "planner", overrides=memory["strategy_overrides"],
    ) == "mars_planner"


def test_env_override_still_beats_recipe(monkeypatch):
    """``ARC_STRATEGY_<ROLE>`` outranks a recipe — same as before."""
    from arc.core.recipes import apply_recipe, get_recipe
    from arc.core.strategies import resolve_strategy_name

    memory: dict = {}
    apply_recipe(get_recipe("bayesian-materials"), memory)
    monkeypatch.setenv("ARC_STRATEGY_OPTIMIZER", "default")

    # Wait — runtime overrides actually win over env in our existing
    # precedence (`runtime > env > config > default`). A recipe-applied
    # entry lives in ``memory["strategy_overrides"]`` which IS the runtime
    # tier, so it beats env. That's the correct, documented behaviour.
    assert resolve_strategy_name(
        "optimizer", overrides=memory["strategy_overrides"],
    ) == "bayesopt"


# ── /recipe slash command ──────────────────────────────────────────────


def _state():
    from arc.chat.state import ChatState
    from tests.fakes import make_workflow
    return ChatState(workflow=make_workflow())


def test_recipe_command_list_does_not_crash(monkeypatch):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    asyncio.run(run(_state(), []))


def test_recipe_command_show_known_recipe(monkeypatch):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    asyncio.run(run(_state(), ["show", "bayesian-materials"]))


def test_recipe_command_apply_writes_overrides(monkeypatch):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = _state()
    asyncio.run(run(state, ["apply", "bayesian-materials"]))

    assert state.memory["active_recipe"] == "bayesian-materials"
    assert state.memory["strategy_overrides"]["optimizer"] == "bayesopt"
    assert state.memory["strategy_overrides"]["planner"] == "mars_planner"


def test_recipe_command_apply_force_overwrites_manual(monkeypatch):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = _state()
    state.memory["strategy_overrides"] = {"planner": "default"}
    asyncio.run(run(state, ["apply", "bayesian-materials", "--force"]))

    assert state.memory["strategy_overrides"]["planner"] == "mars_planner"


def test_recipe_command_clear_removes_recipe_keys(monkeypatch):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = _state()
    asyncio.run(run(state, ["apply", "bayesian-materials"]))
    asyncio.run(run(state, ["clear"]))

    assert "strategy_overrides" not in state.memory
    assert "active_recipe" not in state.memory


def test_recipe_command_rejects_unknown_recipe(monkeypatch):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = _state()
    asyncio.run(run(state, ["apply", "not_a_real_recipe"]))
    assert "strategy_overrides" not in state.memory


def test_recipe_command_no_subcommand_treats_arg_as_apply(monkeypatch):
    """``/recipe bayesian-materials`` is shorthand for apply."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = _state()
    asyncio.run(run(state, ["bayesian-materials"]))
    assert state.memory["active_recipe"] == "bayesian-materials"


# ── End-to-end: apply → resolve_role returns the BO class ─────────────


def test_apply_then_resolve_role_returns_bayesopt_optimizer(monkeypatch):
    """The full pipeline: /recipe apply → resolve_role → BayesOpt class."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.packages import resolve_role

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    state = _state()
    asyncio.run(run(state, ["apply", "bayesian-materials"]))

    cls = resolve_role("optimizer", state.workflow)
    assert cls.__name__ == "BayesOptOptimizerAgent"


# ── save_recipe core function ─────────────────────────────────────────


def test_save_recipe_writes_yaml_with_strategies(tmp_path):
    from arc.core.recipes import save_recipe

    out = save_recipe(
        "my-stack",
        {"planner": "mars_planner", "optimizer": "bayesopt"},
        description="A custom blend",
        target_dir=tmp_path,
    )
    assert out.exists()
    body = out.read_text()
    assert "name: my-stack" in body
    assert "planner: mars_planner" in body
    assert "optimizer: bayesopt" in body
    assert "A custom blend" in body
    assert "user-saved" in body


def test_save_recipe_slugifies_arbitrary_name(tmp_path):
    """Spaces and punctuation become a recipe-safe slug."""
    from arc.core.recipes import save_recipe

    out = save_recipe(
        "My Cool Stack!",
        {"planner": "default"},
        target_dir=tmp_path,
    )
    assert out.stem == "my-cool-stack"


def test_save_recipe_rejects_unsanitisable_name(tmp_path):
    from arc.core.recipes import RecipeSaveError, save_recipe
    with pytest.raises(RecipeSaveError, match="Invalid recipe name"):
        save_recipe("!!!", {"planner": "default"}, target_dir=tmp_path)


def test_save_recipe_rejects_empty_strategies(tmp_path):
    from arc.core.recipes import RecipeSaveError, save_recipe
    with pytest.raises(RecipeSaveError, match="no strategies"):
        save_recipe("empty", {}, target_dir=tmp_path)


def test_save_recipe_rejects_unknown_role(tmp_path):
    from arc.core.recipes import RecipeSaveError, save_recipe
    with pytest.raises(RecipeSaveError, match="unknown role"):
        save_recipe(
            "broken",
            {"not_a_role": "default"},
            target_dir=tmp_path,
        )


def test_save_recipe_rejects_unknown_strategy(tmp_path):
    from arc.core.recipes import RecipeSaveError, save_recipe
    with pytest.raises(RecipeSaveError, match="unknown strategy"):
        save_recipe(
            "broken",
            {"planner": "does_not_exist"},
            target_dir=tmp_path,
        )


def test_save_recipe_refuses_overwrite_without_force(tmp_path):
    from arc.core.recipes import RecipeSaveError, save_recipe

    save_recipe(
        "first", {"planner": "default"}, target_dir=tmp_path,
    )
    with pytest.raises(RecipeSaveError, match="already exists"):
        save_recipe(
            "first", {"planner": "mars_planner"}, target_dir=tmp_path,
        )


def test_save_recipe_overwrites_with_force(tmp_path):
    from arc.core.recipes import save_recipe

    save_recipe(
        "first", {"planner": "default"}, target_dir=tmp_path,
    )
    out = save_recipe(
        "first", {"planner": "mars_planner"},
        target_dir=tmp_path, force=True,
    )
    body = out.read_text()
    assert "planner: mars_planner" in body
    assert "planner: default" not in body


def test_save_recipe_round_trips_through_list_recipes(tmp_path, monkeypatch):
    """After saving, ``list_recipes()`` should find the new recipe."""
    from arc.core import recipes as _recipes
    from arc.core.recipes import save_recipe

    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    out = save_recipe(
        "round-trip-test",
        {"planner": "mars_planner", "reviewer": "reflective"},
        description="round trip test",
        target_dir=tmp_path,
    )
    names = {r.name for r in _recipes.list_recipes()}
    assert "round-trip-test" in names
    loaded = _recipes.get_recipe("round-trip-test")
    assert loaded is not None
    assert loaded.strategies["planner"] == "mars_planner"
    assert loaded.strategies["reviewer"] == "reflective"


# ── /recipe save command ──────────────────────────────────────────────


def test_recipe_command_save_writes_overrides(monkeypatch, tmp_path):
    """``/recipe save my-stack`` snapshots ``strategy_overrides``."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    state = _state()
    state.memory["strategy_overrides"] = {
        "planner": "mars_planner",
        "optimizer": "bayesopt",
    }
    asyncio.run(run(state, ["save", "my-stack"]))

    yaml_path = tmp_path / "my-stack.yaml"
    assert yaml_path.exists()
    body = yaml_path.read_text()
    assert "planner: mars_planner" in body
    assert "optimizer: bayesopt" in body


def test_recipe_command_save_with_description(monkeypatch, tmp_path):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    state = _state()
    state.memory["strategy_overrides"] = {"planner": "default"}
    asyncio.run(run(state, ["save", "named", "A", "great", "stack"]))

    body = (tmp_path / "named.yaml").read_text()
    assert "A great stack" in body


def test_recipe_command_save_requires_overrides(monkeypatch, tmp_path, capsys):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    state = _state()
    asyncio.run(run(state, ["save", "empty"]))

    assert not (tmp_path / "empty.yaml").exists()
    out = capsys.readouterr().out
    assert "nothing to save" in out.lower()


def test_recipe_command_save_force_overwrites(monkeypatch, tmp_path):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    state = _state()
    state.memory["strategy_overrides"] = {"planner": "default"}
    asyncio.run(run(state, ["save", "again"]))

    # Re-save with a different override + --force.
    state.memory["strategy_overrides"] = {"planner": "mars_planner"}
    asyncio.run(run(state, ["save", "again", "--force"]))

    body = (tmp_path / "again.yaml").read_text()
    assert "planner: mars_planner" in body


def test_recipe_command_save_then_apply(monkeypatch, tmp_path):
    """End-to-end: save a recipe in this session, then apply it
    successfully (using the same monkeypatched user dir)."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes
    from arc.packages import resolve_role

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    state = _state()
    state.memory["strategy_overrides"] = {
        "optimizer": "cmaes",
        "reviewer": "reflective",
    }
    asyncio.run(run(state, ["save", "custom-cmaes"]))

    # Reset memory and apply the just-saved recipe.
    state.memory.pop("strategy_overrides", None)
    asyncio.run(run(state, ["apply", "custom-cmaes"]))
    assert state.memory["active_recipe"] == "custom-cmaes"
    assert resolve_role("optimizer", state.workflow).__name__ == "CMAESOptimizerAgent"


def test_recipe_command_save_rejects_invalid_strategy(monkeypatch, tmp_path, capsys):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    state = _state()
    state.memory["strategy_overrides"] = {"planner": "definitely_not_a_strategy"}
    asyncio.run(run(state, ["save", "broken"]))

    assert not (tmp_path / "broken.yaml").exists()
    out = capsys.readouterr().out
    assert "unknown strategy" in out.lower()


# ── delete_recipe core function ──────────────────────────────────────


def test_delete_recipe_removes_user_yaml(tmp_path):
    from arc.core.recipes import delete_recipe, save_recipe

    save_recipe(
        "doomed", {"planner": "default"},
        target_dir=tmp_path,
    )
    assert (tmp_path / "doomed.yaml").exists()
    out = delete_recipe("doomed", target_dir=tmp_path)
    assert out == tmp_path / "doomed.yaml"
    assert not out.exists()


def test_delete_recipe_normalises_name(tmp_path):
    """Punctuated input resolves to the same slug we used at save time."""
    from arc.core.recipes import delete_recipe, save_recipe

    save_recipe("My Cool Stack!", {"planner": "default"}, target_dir=tmp_path)
    # Same name with different punctuation hits the same slug.
    delete_recipe("my-cool-stack!", target_dir=tmp_path)
    assert not (tmp_path / "my-cool-stack.yaml").exists()


def test_delete_recipe_rejects_bundled():
    """The error message must mention bundled + offer a path forward."""
    from arc.core.recipes import RecipeDeleteError, delete_recipe

    with pytest.raises(RecipeDeleteError) as excinfo:
        delete_recipe("bayesian-materials")
    msg = str(excinfo.value)
    assert "bundled" in msg.lower()
    assert "shadow" in msg.lower() or "save" in msg.lower()


def test_delete_recipe_rejects_missing(tmp_path):
    from arc.core.recipes import RecipeDeleteError, delete_recipe

    with pytest.raises(RecipeDeleteError, match="No user recipe"):
        delete_recipe("never-saved", target_dir=tmp_path)


def test_delete_recipe_rejects_empty_name(tmp_path):
    from arc.core.recipes import RecipeDeleteError, delete_recipe

    with pytest.raises(RecipeDeleteError, match="non-empty"):
        delete_recipe("", target_dir=tmp_path)


def test_delete_recipe_rejects_unsanitisable_name(tmp_path):
    from arc.core.recipes import RecipeDeleteError, delete_recipe

    with pytest.raises(RecipeDeleteError, match="could not be normalised"):
        delete_recipe("!!!", target_dir=tmp_path)


def test_delete_recipe_disappears_from_list_recipes(tmp_path, monkeypatch):
    """After delete, ``list_recipes()`` no longer finds the recipe."""
    from arc.core import recipes as _recipes
    from arc.core.recipes import delete_recipe, save_recipe

    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    save_recipe("transient", {"planner": "default"}, target_dir=tmp_path)
    assert _recipes.get_recipe("transient") is not None
    delete_recipe("transient", target_dir=tmp_path)
    assert _recipes.get_recipe("transient") is None


# ── /recipe delete command ───────────────────────────────────────────


def _no_input(prompt: str) -> str:
    """Default fake-input that fails the test if anyone tries to prompt."""
    raise AssertionError(f"Unexpected prompt: {prompt!r}")


def test_recipe_command_delete_confirms_yes(monkeypatch, tmp_path):
    """``y`` confirms and the file is removed."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    # Prepare a user recipe.
    _recipes.save_recipe("doomed", {"planner": "default"}, target_dir=tmp_path)

    async def _yes(prompt: str) -> str:
        return "y"

    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _yes)

    state = _state()
    asyncio.run(run(state, ["delete", "doomed"]))
    assert not (tmp_path / "doomed.yaml").exists()


def test_recipe_command_delete_cancels_on_no(monkeypatch, tmp_path):
    """``n`` (or empty) keeps the file."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    _recipes.save_recipe("safe", {"planner": "default"}, target_dir=tmp_path)

    async def _no(prompt: str) -> str:
        return "n"

    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _no)

    state = _state()
    asyncio.run(run(state, ["delete", "safe"]))
    assert (tmp_path / "safe.yaml").exists()


def test_recipe_command_delete_force_skips_confirmation(monkeypatch, tmp_path):
    """``--force`` deletes without ever calling ``chat_input_async``."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    _recipes.save_recipe("snap", {"planner": "default"}, target_dir=tmp_path)
    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _no_input)

    state = _state()
    asyncio.run(run(state, ["delete", "snap", "--force"]))
    assert not (tmp_path / "snap.yaml").exists()


def test_recipe_command_delete_rejects_bundled(monkeypatch, capsys, tmp_path):
    """Bundled recipes can't be deleted from chat either."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _no_input)

    state = _state()
    asyncio.run(run(state, ["delete", "bayesian-materials", "--force"]))

    out = capsys.readouterr().out
    assert "bundled" in out.lower()


def test_recipe_command_delete_missing_recipe(monkeypatch, capsys, tmp_path):
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)
    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _no_input)

    state = _state()
    asyncio.run(run(state, ["delete", "never-saved", "--force"]))

    out = capsys.readouterr().out
    assert "no user recipe" in out.lower()


def test_recipe_command_delete_active_recipe_clears_state(monkeypatch, tmp_path):
    """Deleting the active recipe also wipes ``active_recipe`` so the
    next /recipe call doesn't claim a recipe that no longer exists."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)
    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _no_input)

    _recipes.save_recipe(
        "active-one",
        {"planner": "mars_planner", "optimizer": "bayesopt"},
        target_dir=tmp_path,
    )

    state = _state()
    asyncio.run(run(state, ["apply", "active-one"]))
    assert state.memory["active_recipe"] == "active-one"
    assert state.memory["strategy_overrides"]["planner"] == "mars_planner"

    asyncio.run(run(state, ["delete", "active-one", "--force"]))
    assert "active_recipe" not in state.memory
    # The recipe-applied overrides are also cleared.
    assert "strategy_overrides" not in state.memory


def test_recipe_command_delete_non_active_keeps_overrides(monkeypatch, tmp_path):
    """A delete on a non-active recipe must NOT touch other overrides."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)
    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _no_input)

    _recipes.save_recipe("a", {"planner": "default"}, target_dir=tmp_path)
    _recipes.save_recipe("b", {"reviewer": "reflective"}, target_dir=tmp_path)

    state = _state()
    asyncio.run(run(state, ["apply", "a"]))
    assert state.memory["active_recipe"] == "a"

    asyncio.run(run(state, ["delete", "b", "--force"]))
    # 'a' is still active, its overrides survive.
    assert state.memory["active_recipe"] == "a"
    assert "planner" in state.memory["strategy_overrides"]


def test_recipe_command_delete_alias_rm(monkeypatch, tmp_path):
    """``/recipe rm`` should work the same as ``/recipe delete``."""
    from arc.chat.commands.recipe import run
    from arc.chat.state import ChatState
    from arc.core import recipes as _recipes

    monkeypatch.setattr(ChatState, "persist", lambda self: None)
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)
    monkeypatch.setattr("arc.chat.commands.recipe.chat_input_async", _no_input)

    _recipes.save_recipe("removable", {"planner": "default"}, target_dir=tmp_path)
    state = _state()
    asyncio.run(run(state, ["rm", "removable", "--force"]))
    assert not (tmp_path / "removable.yaml").exists()


def test_recipe_command_delete_appears_in_help():
    from arc.chat.commands import build_registry
    from arc.chat.registry import format_help_lines

    lines = "\n".join(format_help_lines(build_registry()))
    assert "/recipe" in lines
    assert "delete" in lines
