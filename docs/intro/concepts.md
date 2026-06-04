# Concepts & vocabulary

*The core nouns. One precise sentence and a link each — read this once and the
rest of the docs make sense.*

```{glossary}
Kernel
  The top-level object that owns the registry, event bus, and session
  manager, loads packages from `arc.toml`, and initialises extensions. See
  {doc}`../core/kernel`.

Component registry
  The in-memory map of every loaded component (agents, skills, adapters,
  providers, workflows, extensions, evaluators, audit actions, …) plus a
  package-source map so `/package disable` can filter by owner. See
  {doc}`../core/registry`.

Package
  A directory with a `package.yaml` manifest that contributes components via
  `provides.*`. Packages are how ARC is extended without editing core. See
  {doc}`../packages/index`.

Role
  A named slot in the research loop — `ideator`, `searcher`, `planner`,
  `builder`, `validator`, `reviewer`, `reflector`, `optimizer`, `curator`.
  See {doc}`../packages/roles`.

Strategy
  A concrete implementation of a role (e.g. `bayesopt` for `optimizer`), or
  an ordered **stack** of them. The {term}`strategy resolver` picks one per
  call. See {doc}`../core/strategies`.

Strategy resolver
  The single lookup that maps a role to a class using a four-level
  precedence: runtime override → env var → `arc.toml` → bundled default.
  See {doc}`../core/strategies`.

Agent
  A class implementing `AgentContract` with an async `run(input_data)`. Most
  strategies are agents. See {doc}`../contracts/index`.

Skill
  A Markdown-defined unit of behaviour a package provides, executed via
  `SkillContract`. See {doc}`../contracts/index`.

Runtime adapter
  Answers *where the workflow runs* (`ARC_RUNTIME_ADAPTER`): local, sim2l, or
  a package-provided remote (docker/slurm/k8s). It validates and executes
  artifacts. See {doc}`../core/runtime-adapters`.

Backend actions
  Answers *where artifacts and results get published* (Register / Persist /
  Record). The default `NoopBackend` publishes nothing; `Sim2lBackend`
  activates when sim2l is present. **Distinct from the runtime adapter.** See
  {doc}`../architecture/execution-vs-publish`.

Extension
  An optional integration loaded at startup (MCP tools, OpenAPI, a vector
  store, a remote runtime). Implements `ExtensionContract`. See
  {doc}`../packages/extensions`.

Recipe / preset
  A named YAML bundle of strategy choices that can be applied to a session.
  The user-facing command is `/preset` (`/recipe` is a backward-compatible
  alias). See {doc}`../core/recipes-presets`.

Session
  A unit of work with its own id and directory; persists run history,
  strategy/package choices, and artifacts. See {doc}`../core/sessions`.

Stub mode
  The provider-less default: every agent uses deterministic, no-LLM logic so
  the loop runs offline and reproducibly. See {doc}`../guides/providers`.

Audit action
  A package-provided observer bound to a lifecycle phase (e.g.
  `validation.after`) that can record findings or block a run. See
  {doc}`../packages/audit-and-report`.
```

## The two axes you must not conflate

ARC keeps two questions **orthogonal**:

1. **Where does the workflow run?** → the {term}`runtime adapter`
   (`ARC_RUNTIME_ADAPTER`).
2. **Where do artifacts/results get published?** → the
   {term}`backend actions`.

You can run locally and publish to sim2l, or run on a cluster and publish
nowhere. See {doc}`../architecture/execution-vs-publish`.
