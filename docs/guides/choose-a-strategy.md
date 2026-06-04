# Choose a strategy

*Pick which implementation backs each role — per session, per project, or via
the environment — and bundle choices as presets.*

## One-off, in a chat session

```text
/strategy optimizer bayesopt
/strategy planner doe_lhs
/strategy searcher default embeddings materials_project   # a stack
```

`/strategies` lists every role's options and the active pick. The change
persists for the session.

## Per project (`arc.toml`)

```toml
[strategies]
planner = "mars_planner"
optimizer = "bayesopt"
```

## Via the environment

```bash
ARC_STRATEGY_PLANNER=doe_lhs arc run "…"
```

## Precedence

Runtime override (`/strategy`, applied preset) → `ARC_STRATEGY_<ROLE>` →
`arc.toml [strategies]` → bundled default. See {doc}`../core/strategies`.

## Bundle choices as a preset

```text
/strategy optimizer bayesopt
/strategy planner mars_planner
/preset save my-mp-tuning "Bayesian MP tuning"
…
/preset apply my-mp-tuning
```

Presets live in `~/.arc/recipes/*.yaml`; bundled ones ship in `arc/recipes/`.
See {doc}`../core/recipes-presets`.

## Disable a whole package

```text
/package disable arc-mars      # its strategies become unselectable this session
```

See {doc}`../packages/enable-disable`.
