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

The coding packages (`arc-codex`, `arc-claude-code`) are documented in depth in
the design note included below.

---

```{include} ../../../design/coding-agents.md
```
