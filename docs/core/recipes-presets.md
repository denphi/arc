# Recipes & presets

*Named bundles of strategy choices you can apply to a session. The
user-facing command is `/preset`; `/recipe` is a backward-compatible alias.*

A **recipe** (a.k.a. **preset**) is a YAML file mapping roles to strategies,
plus metadata and optional auto-suggest triggers. They live in
`arc/recipes/*.yaml` (bundled) and `~/.arc/recipes/*.yaml` (user). On-disk
they are still `recipes/*.yaml`; the docs/UI/command surface calls them
presets.

## Commands

```text
/preset list                 # discoverable presets
/preset show <name>          # roles + description
/preset apply <name>         # apply to the session (--force overrides manual picks)
/preset save <name> [desc]   # snapshot current /strategy overrides
/preset delete <name>        # remove a user preset (bundled are read-only)
/preset clear                # drop the keys the last applied preset set
```

The HTTP API mirrors these under `/presets` (with `/recipes` kept as a
compatibility alias) — see {doc}`../interfaces/api`.

## Validation

`validate_recipe(recipe)` checks each `(role, impl)` against the strategy
catalogue **before** apply — including each component of a stack — and reports
a clear error for an unknown role/strategy rather than misrouting at resolve
time.

## Triggers (auto-suggest)

A recipe can declare `triggers:` so the loop suggests it when a goal matches
(`arc/core/recipe_suggest.py`).

## API reference

```{eval-rst}
.. automodule:: arc.core.recipes
   :members:
   :undoc-members:
```
