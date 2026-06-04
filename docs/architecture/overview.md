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

The canonical, detailed architecture document follows. It covers the layered
diagram, component roles, the loop-step → implementation mapping, agent
communication, memory layers, execution modes, security boundaries, and the
key design decisions.

---

```{include} ../../design/architecture.md
```
