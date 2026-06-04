# Bundled package catalogue

*Every package shipped under `arc/packages/`. Each lists its type, what it
provides, required config, and how to enable it.*

| Package | Type | Provides |
|---|---|---|
| `arc-sim2l` | artifact | the artifact lifecycle + the **default** role strategies (ideator/searcher/planner/builder/validator/reviewer/reflector/optimizer/curator), the `research-loop` workflow, the local + sim2l runtime adapters, core skills. |
| `arc-mars` | strategy | cost-aware `mars_planner`, the `reflective` reviewer, an iterative-improvement workflow. |
| `arc-materials` | domain | materials evaluators (band gap, formation energy, stability, property), vocabularies (properties, simulation methods), constraints, prompts; the `materials_project` searcher (needs `MP_API_KEY`) and `materials_evaluators` validator. |
| `arc-codex` | coding | the `codex` builder — drives the Codex CLI. Config: `ARC_CODEX_*`. |
| `arc-claude-code` | coding | the `claude_code` builder — drives the Claude Code CLI. Config: `ARC_CLAUDE_CODE_*`. |
| `arc-providers` | provider | the `anthropic` and `openai` LLM providers. Config: `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`. |
| `arc-mcp` | extension | exposes MCP tools as skills (`mcp::<app>::<tool>`). Enable `[extensions.mcp]`. |
| `arc-openapi` | extension | exposes OpenAPI operations as skills. Enable `[extensions.openapi]`. |
| `arc-vector-memory` | extension | persistent semantic memory (zero-dep default, optional Chroma). Enable `[extensions.vector-memory]`. |
| `arc-knowledge-graph` | extension | persistent directed graph of experiments/artifacts/variables. Enable `[extensions.knowledge-graph]`. |
| `arc-docker` | runtime | the `docker` runtime adapter. `ARC_RUNTIME_ADAPTER=docker`; config `ARC_DOCKER_*`. |
| `arc-slurm` | runtime | the `slurm` runtime adapter. `ARC_RUNTIME_ADAPTER=slurm`; config `ARC_SLURM_*`. |
| `arc-k8s` | runtime | the `k8s` runtime adapter. `ARC_RUNTIME_ADAPTER=k8s`; config `ARC_K8S_*`. |
| `arc-coscientist` | research | a Co-Scientist-style hypothesis package (multi-candidate ideation, a tournament workflow, report sections). |

## Enabling

- **Strategies / coding backends / providers / domain assets** load when the
  package is in `arc.toml` `[packages].paths` (the bundled set already is).
  Select them via `/strategy`, `/coder`, `ARC_PROVIDER`, etc.
- **Extensions** additionally need their `[extensions.<name>]` block set to
  `enabled = true`.
- **Runtime adapters** are selected via `ARC_RUNTIME_ADAPTER`.

## Coding packages

`arc-codex` and `arc-claude-code` are coding backends. They implement builder
strategies that call external coding-agent CLIs, then return ARC artifacts and
provenance like any other builder.

Both packages follow the same shape:

- package config declares command, model/profile, working directory, extra
  arguments, timeout, and approval behavior;
- the builder runs in a controlled workspace and writes files through ARC's
  artifact flow;
- non-interactive runs require explicit approval configuration;
- package disable prevents the coding backend from being selected.

Use these packages when artifact generation should happen through a dedicated
coding agent rather than ARC's built-in stub or LLM strategy.
