# Authoring a strategy

*Add a role implementation from a `package.yaml` manifest — never by editing
`arc/core/strategies.py`.*

A strategy is an `AgentContract` (or a role-specific subclass) declared under
`provides.strategies`.

## Steps

1. Implement the class in your package, e.g.
   `arc/packages/arc-my-lab/agents/planner.py`.
2. Declare it in `package.yaml`:

   ```yaml
   provides:
     strategies:
       - role: planner
         name: my_planner
         # dotted entrypoint…
         entrypoint: arc.packages.arc-my-lab.agents.planner:MyPlannerAgent
         # …or, for a local package, package-relative path + class:
         # path: agents/planner.py
         # class: MyPlannerAgent
         description: A cost-aware planner tuned for nanophotonics.
         default: false        # true → become the role's default
   ```

3. Add the package to `arc.toml` `[packages].paths` (or use a local package —
   see {doc}`local-packages`).
4. Validate: `arc package validate <dir>` (exits non-zero if it didn't
   register).
5. Select it: `/strategy planner my_planner`, `ARC_STRATEGY_PLANNER=my_planner`,
   or `[strategies] planner = "my_planner"`.

## The per-role contract

Each role's `run`/`search`/`validate` signature is published in
{doc}`../packages/index` (the per-role examples) and the
{doc}`../contracts/index` reference. Open a bundled strategy of the same role
as a template.

```{note}
`arc.core.strategies.register_strategy(role, StrategySpec(...))` is the
low-level API the loader calls; reach for it directly only when you have no
manifest (a test or a notebook).
```
