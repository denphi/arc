# Python API reference

*The import-level map of ARC's core. Full member documentation (rendered from
docstrings) lives inline on the prose **Core** pages — each module below links
to where it's documented. This page is the index.*

```{note}
Optional 3rd-party dependencies (`anthropic`, `openai`, `chromadb`, `docker`,
`sim2l`, …) are mocked during the docs build, so the reference renders even
when those packages aren't installed.
```

## Core framework

| Module | Documented on |
|---|---|
| `arc.core.kernel` | {doc}`../../core/kernel` |
| `arc.core.registry` | {doc}`../../core/registry` |
| `arc.core.loader` | {doc}`../../core/loader` |
| `arc.core.config`, `arc.core.env` | {doc}`../../core/config-and-env` |
| `arc.core.strategies` | {doc}`../../core/strategies` |
| `arc.core.recipes` | {doc}`../../core/recipes-presets` |

## Contracts

| Module | Documented on |
|---|---|
| `arc.contracts.agent`, `arc.contracts.skill`, `arc.contracts.adapter`, `arc.contracts.provider`, `arc.contracts.extension` | {doc}`../../contracts/index` |
| `arc.contracts.audit` | {doc}`../../core/audit` |

## Schemas

| Module | Documented on |
|---|---|
| `arc.schemas.research`, `arc.schemas.artifact`, `arc.schemas.execution`, `arc.schemas.review` | {doc}`../../core/schemas` |

## Runtime

| Module | Documented on |
|---|---|
| `arc.runtime.local`, `arc.runtime.sim2l_adapter` | {doc}`../../core/runtime-adapters` |
| `arc.runtime.workflow_safety` | {doc}`../../core/executor-and-safety` |
| `arc.runtime.backend` | {doc}`../../core/backends` |
| `arc.runtime.audit` | {doc}`../../core/audit` |

## Memory, orchestrator, providers, session

| Module | Documented on |
|---|---|
| `arc.memory.*` | {doc}`../../core/memory` |
| `arc.orchestrator.workflow` | {doc}`../../core/orchestrator` |
| `arc.providers`, `arc.providers.utils` | {doc}`../../core/providers` |
| `arc.session`, `arc.api.session_state` | {doc}`../../core/sessions` |
