# Architecture overview

*The layered design, the component roles, and how each loop step maps to a
registered implementation.*

ARC separates a small **core** (the kernel, registry, loader, contracts,
resolver, schemas, orchestrator, runtime, memory, providers) from
**packages** that contribute the actual research behaviour. Core knows the
*shapes* (contracts) and the *plumbing* (how to load, resolve, and run
components); packages supply the *implementations*.

```{figure} ../_static/arc-system-design.png
:alt: ARC system design
:width: 100%

The ARC system: core plumbing and the package-provided components, wired
together by the kernel and the strategy resolver.
```

```{figure} ../_static/arc-research-lifecycle.png
:alt: ARC research lifecycle
:width: 100%

The research lifecycle — each step is a role backed by a strategy.
```

## Public summary

ARC is organized as a layered runtime:

- **Core** owns stable contracts, schemas, package loading, strategy
  resolution, workflow execution, session state, memory, providers, and
  runtime adapters.
- **Packages** contribute implementations: role strategies, agents, skills,
  loaders, providers, extensions, runtime adapters, workflows, audit actions,
  and report sections.
- **The orchestrator** runs a workflow by resolving each role from the active
  package set, executing steps, recording provenance, and persisting session
  state.
- **Runtime adapters** execute artifacts locally or remotely, while backend
  actions publish or persist results to systems such as Sim2L or GitHub.

The key boundary is intentional: core depends on contracts, not concrete
research behavior. That keeps package authors free to add domain-specific
methods without changing ARC itself.
