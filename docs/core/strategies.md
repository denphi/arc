# Strategy resolver

*The single lookup that maps a research **role** to the class that implements
it, with a four-level precedence and composite **stacks**.*

ARC's loop calls fixed roles (`ideator`, `planner`, `reviewer`, …). Each role
can be backed by more than one **strategy** (an LLM proposer, a Bayesian
optimizer, a DoE planner, …). `arc.core.strategies` is the one place that
picks which class to instantiate.

## Precedence

`resolve_role(role, *, overrides, config, disabled_packages, loaded_packages)`
picks the strategy *name* by precedence (highest first):

1. **Runtime override** — `memory["strategy_overrides"][role]`, set by the
   chat `/strategy` command or an applied recipe.
2. **Environment** — `ARC_STRATEGY_<ROLE>` (e.g. `ARC_STRATEGY_PLANNER=doe_lhs`).
3. **`arc.toml`** — the `[strategies]` table.
4. **Bundled default** — the catalogue's default for that role.

It then loads the class from the matching `StrategySpec`. A strategy owned by
a **disabled** package is refused and the role falls back to an enabled one
(see {doc}`../packages/enable-disable`). Unknown/failed strategies log a
warning and fall back — they never crash the loop.

## Where specs come from

The catalogue is built from **two** sources:

- the bundled defaults hard-coded in `arc/core/strategies.py` (`_ROLE_CATALOGUE`);
- every package's `provides.strategies` manifest entry, registered via
  `register_strategy()` at load time.

You add a strategy by declaring it in a `package.yaml`, **not** by editing
core. See {doc}`../packages/authoring-strategy`.

## Composite stacks

A selector with more than one name (whitespace, `+`, or commas) is a
**stack** — e.g. `/strategy searcher default embeddings materials_project`.
Each role has a composite class with deterministic merge semantics (searcher
dedupes hits, planner merges sweeps/constraints, reviewer forms consensus,
optimizer shares a budget, …). See {doc}`../packages/roles` for the per-role
rules. Components from a disabled package are dropped from the stack.

## User-facing controls

Strategies can be selected at several levels:

- **One session:** use chat commands such as `/strategy planner doe_lhs` or the
  HTTP strategy endpoints.
- **One run:** pass workflow inputs or constraints that a strategy consumes.
- **One project:** set `[strategies]` in `arc.toml`.
- **One environment:** set `ARC_STRATEGY_<ROLE>` for CI or deployment defaults.

Recipes and presets bundle these choices so a session can switch between
research modes without manually setting every role.

## Planner, sweep, and optimize

ARC treats these as related but distinct concerns:

- the **planner** proposes what should be tried;
- **sweep** style strategies enumerate a structured parameter grid or design;
- the **optimizer** uses previous results to propose better candidates.

Composite stacks let a role combine several contributors while keeping a
deterministic merge rule for that role.
