# ARC documentation

**ARC** (Autonomous Research Coder) is a multi-agent framework for autonomous
scientific research. It orchestrates an iterative research loop built around
[Sim2L](https://sim2l.readthedocs.io/) artifacts:

```text
Ideate → Plan → Build → Validate → Execute → Review → Improve → (repeat)
```

Each step is a pluggable **role**; without an LLM provider configured, every
role falls back to deterministic **stub** logic, so the whole loop runs
offline and is fully testable.

This site is **core-first**: most of it documents ARC's core framework — the
kernel, registry, loader, contracts, the strategy resolver, schemas, runtime
adapters and backends, memory, the orchestrator, providers, sessions, and the
CLI/API/UI/chat surfaces. The {doc}`packages/index` section documents how to
extend ARC with packages and catalogues the bundled ones.

```{toctree}
:maxdepth: 2
:caption: Getting started

intro/overview
intro/install
intro/quickstart
intro/concepts
```

```{toctree}
:maxdepth: 2
:caption: Architecture

architecture/overview
architecture/execution-vs-publish
architecture/security
```

```{toctree}
:maxdepth: 2
:caption: Contracts

contracts/index
```

```{toctree}
:maxdepth: 2
:caption: Core

core/kernel
core/registry
core/loader
core/config-and-env
core/schemas
core/strategies
core/recipes-presets
core/runtime-adapters
core/executor-and-safety
core/backends
core/audit
core/memory
core/orchestrator
core/workflows
core/file-assets
core/providers
core/sessions
```

```{toctree}
:maxdepth: 2
:caption: Interfaces

interfaces/cli
interfaces/api
interfaces/ui
interfaces/chat
```

```{toctree}
:maxdepth: 2
:caption: Reference

reference/configuration
reference/api/index
```

```{toctree}
:maxdepth: 2
:caption: Guides

guides/run-a-research-loop
guides/choose-a-strategy
guides/use-the-api
guides/sim2l-services
guides/providers
```

```{toctree}
:maxdepth: 2
:caption: Packages

packages/index
packages/manifest
packages/roles
packages/enable-disable
packages/authoring-strategy
packages/local-packages
packages/extensions
packages/audit-and-report
packages/catalogue/index
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
