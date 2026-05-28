"""``/recipe`` — apply named bundles of research-loop strategy choices.

Usage:
    /recipe                          list every discoverable recipe
    /recipe show <name>              show a recipe's roles + description
    /recipe apply <name>             apply a recipe to the session
    /recipe apply <name> --force     apply, overwriting manual /strategy choices
    /recipe save <name>              snapshot current /strategy overrides
    /recipe delete <name>            remove a user-saved recipe (asks to confirm)
    /recipe delete <name> --force    delete without confirmation
    /recipe clear                    drop the keys the last applied recipe set
"""

from __future__ import annotations

from arc.chat.input import chat_input_async
from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, c, err, header, ok, step, warn


def _list_recipes(state: ChatState) -> None:
    from arc.core.recipes import list_recipes

    recipes = list_recipes()
    if not recipes:
        warn("No recipes found.")
        return

    header("Available recipes")
    applied = state.memory.get("recipe_applied") or {}
    applied_recipe_name = state.memory.get("active_recipe")

    for recipe in recipes:
        marker = ""
        if recipe.name == applied_recipe_name and applied:
            marker = c("  ← active", GREEN, BOLD)
        src = c(f"[{recipe.source}]", DIM)
        print(f"  {c(recipe.name, CYAN, BOLD)} {src}{marker}")
        if recipe.description:
            from textwrap import fill
            for line in fill(recipe.description, 70).splitlines():
                print(f"    {c(line, DIM)}")
        print(f"    {c(recipe.display_strategies(), DIM)}")
    print()
    print(c("  /recipe show <name> to inspect, /recipe apply <name> to switch.", DIM))


def _show_recipe(state: ChatState, name: str) -> None:
    from arc.core.recipes import get_recipe

    recipe = get_recipe(name)
    if recipe is None:
        err(f"Unknown recipe: {name}")
        return

    header(f"Recipe: {recipe.name}")
    if recipe.description:
        from textwrap import fill
        step("Description", fill(recipe.description, 60, subsequent_indent=" " * 16))
    if recipe.tags:
        step("Tags", ", ".join(recipe.tags))
    step("Source", recipe.source)
    print(f"  {c('Strategies:', BOLD)}")
    for role, impl in recipe.strategies.items():
        print(f"    {c(role, CYAN)} = {c(impl, CYAN, BOLD)}")


def _apply_recipe(state: ChatState, name: str, *, force: bool) -> None:
    from arc.core.recipes import apply_recipe, get_recipe, validate_recipe

    recipe = get_recipe(name)
    if recipe is None:
        err(f"Unknown recipe: {name}")
        return

    errors = validate_recipe(recipe)
    if errors:
        err(f"Cannot apply {recipe.name!r} — invalid:")
        for e in errors:
            print(f"    {c('•', YELLOW)} {e}")
        return

    result = apply_recipe(recipe, state.memory, overwrite_manual=force)
    state.memory["active_recipe"] = recipe.name

    ok(f"Applied recipe {c(recipe.name, CYAN, BOLD)}")
    if result.overrides_set:
        for role, impl in result.overrides_set.items():
            print(f"    {c(role, CYAN)} → {c(impl, CYAN, BOLD)}")
    if result.overrides_skipped:
        warn("Some roles already had a manual override and were left as-is:")
        for role, impl in result.overrides_skipped.items():
            print(f"    {c(role, CYAN)} stayed at {c(impl, CYAN)}  "
                  f"{c('(use --force to overwrite)', DIM)}")
    state.persist()


def _clear_recipe(state: ChatState) -> None:
    from arc.core.recipes import clear_recipe

    cleared = clear_recipe(state.memory)
    state.memory.pop("active_recipe", None)
    if not cleared:
        warn("No recipe was active — nothing to clear.")
        return
    ok("Cleared recipe-applied overrides:")
    for role, impl in cleared.items():
        print(f"    {c(role, CYAN)} (was {c(impl, CYAN)})")
    state.persist()


def _save_recipe(
    state: ChatState,
    name: str,
    description: str,
    *,
    force: bool,
) -> None:
    """Snapshot ``memory['strategy_overrides']`` into a user-local recipe."""
    from arc.core.recipes import RecipeSaveError, save_recipe

    overrides = state.memory.get("strategy_overrides") or {}
    if not overrides:
        warn(
            "No strategy overrides set — nothing to save. Tweak strategies with "
            "/strategy first, then re-run /recipe save."
        )
        return

    try:
        path = save_recipe(
            name, overrides,
            description=description,
            force=force,
        )
    except RecipeSaveError as exc:
        err(str(exc))
        return

    ok(f"Saved recipe {c(path.stem, CYAN, BOLD)}")
    print(f"    {c(str(path), DIM)}")
    for role, impl in overrides.items():
        print(f"    {c(role, CYAN)} = {c(impl, CYAN, BOLD)}")
    print(c("  Apply later with /recipe apply " + path.stem, DIM))


async def _delete_recipe(state: ChatState, name: str, *, force: bool) -> None:
    """Remove a user-saved recipe, with y/N confirmation.

    Refuses to delete bundled recipes (they'd come back on reinstall).
    If the recipe is the currently active one, also clears the active
    state so the chat doesn't keep advertising a deleted recipe.
    """
    from arc.core.recipes import RecipeDeleteError, clear_recipe, delete_recipe

    active = state.memory.get("active_recipe")
    if not force:
        try:
            confirm = (await chat_input_async(
                c(f"  Delete recipe {name}? [y / N] > ", BOLD)
            )).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if confirm not in ("y", "yes"):
            print(c("  Cancelled.", DIM))
            return

    try:
        path = delete_recipe(name)
    except RecipeDeleteError as exc:
        err(str(exc))
        return

    ok(f"Removed recipe {c(path.stem, CYAN, BOLD)}")
    print(f"    {c(str(path), DIM)}")

    # If we just deleted the active recipe, clear its applied overrides
    # so the loop doesn't try to honour a recipe that no longer exists.
    if active == path.stem:
        cleared = clear_recipe(state.memory)
        state.memory.pop("active_recipe", None)
        if cleared:
            warn(
                f"{path.stem!r} was the active recipe — cleared its applied "
                f"overrides ({', '.join(cleared)})."
            )
    state.persist()


async def run(state: ChatState, argv: list[str]) -> None:
    if not argv:
        _list_recipes(state)
        return

    sub = argv[0].lower()
    rest = argv[1:]

    if sub == "list":
        _list_recipes(state)
        return
    if sub == "show":
        if not rest:
            err("Usage: /recipe show <name>")
            return
        _show_recipe(state, rest[0])
        return
    if sub == "apply":
        if not rest:
            err("Usage: /recipe apply <name> [--force]")
            return
        force = "--force" in rest
        name = next((r for r in rest if not r.startswith("--")), None)
        if name is None:
            err("Usage: /recipe apply <name> [--force]")
            return
        _apply_recipe(state, name, force=force)
        return
    if sub == "clear":
        _clear_recipe(state)
        return
    if sub == "save":
        if not rest:
            err("Usage: /recipe save <name> [<description>] [--force]")
            return
        force = "--force" in rest
        positional = [r for r in rest if not r.startswith("--")]
        if not positional:
            err("Usage: /recipe save <name> [<description>] [--force]")
            return
        save_name = positional[0]
        description = " ".join(positional[1:]) if len(positional) > 1 else ""
        _save_recipe(state, save_name, description, force=force)
        return
    if sub in ("delete", "remove", "rm"):
        if not rest:
            err("Usage: /recipe delete <name> [--force]")
            return
        force = "--force" in rest
        name = next((r for r in rest if not r.startswith("--")), None)
        if name is None:
            err("Usage: /recipe delete <name> [--force]")
            return
        await _delete_recipe(state, name, force=force)
        return

    # If no subcommand matched, treat the first arg as a recipe name to apply.
    _apply_recipe(state, argv[0], force=("--force" in argv))


COMMANDS = [
    SlashCommand(
        name="recipe",
        summary="List/apply/save/delete named bundles of research-loop strategy choices.",
        handler=run,
        args_help="[list|show <n>|apply <n>|save <n>|delete <n>|clear]",
    ),
]
