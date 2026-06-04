# Packages

*How ARC is extended. A package is a directory with a `package.yaml` manifest
that contributes components via `provides.*` — without editing core.*

```{figure} ../_static/arc-packaging-extensibility.png
:alt: ARC packaging & extensibility
:width: 100%

Packages contribute strategies, agents, skills, adapters, providers,
extensions, and more — discovered and wired in by the loader and resolver.
```

## Package types

| Type | Contributes | Examples |
|---|---|---|
| **artifact** | the artifact lifecycle + default role strategies | `arc-sim2l` |
| **strategy** | alternative role strategies | `arc-mars` |
| **domain** | evaluators, vocabularies, constraints, prompts | `arc-materials` |
| **coding** | a coding backend (CLI-driven artifact generation) | `arc-codex`, `arc-claude-code` |
| **provider** | LLM providers | `arc-providers` |
| **extension** | a startup integration (`ExtensionContract`) | `arc-mcp`, `arc-openapi`, `arc-vector-memory`, `arc-knowledge-graph` |
| **runtime** | a remote runtime adapter | `arc-docker`, `arc-slurm`, `arc-k8s` |
| **research** | a richer research workflow | `arc-coscientist` |

## In this section

- {doc}`manifest` — the full `package.yaml` reference.
- {doc}`roles` — the nine loop roles + composite-stack merge rules.
- {doc}`enable-disable` — startup vs session enable/disable, and what each
  filters.
- {doc}`authoring-strategy` — write a role strategy from a manifest.
- {doc}`local-packages` — `arc package init` / `validate` for local packages.
- {doc}`extensions` — author an extension; the bundled extensions.
- {doc}`audit-and-report` — lifecycle audit actions + report sections.
- {doc}`catalogue/index` — the bundled packages.

## Package authoring summary

A package is intentionally ordinary: a directory with a `package.yaml`, Python
modules, optional Markdown skills/prompts, optional workflows, and tests. The
manifest declares what the package provides; ARC loads those entries into a
package-scoped registry so provenance and enable/disable behavior remain
consistent.

Typical package work:

- implement the relevant contract or callable class;
- declare it under `provides.*` in `package.yaml`;
- add package config keys when environment or service credentials are needed;
- validate with `arc package validate <path>`;
- add focused tests for registration, disabled-package behavior, and runtime
  execution.

Domain packages should keep domain data and examples inside the package, while
core stays generic.
