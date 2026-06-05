# Configuration reference

*Every environment variable and `arc.toml` section ARC reads. Precedence:
process environment > `.env` files (`./.env`, `~/.arc/.env`) > `arc.toml`
defaults. See {doc}`../core/config-and-env`.*

```{tip}
`arc info` prints the loaded components and each package's declared config
(set/unset, secrets masked). `arc chat --check` reports provider/service/auth
status.
```

## Core

| Variable | Default | Meaning |
|---|---|---|
| `ARC_PROVIDER` | *(unset → stub mode)* | LLM provider name: `openwebui`, `anthropic`, `openai`. |
| `ARC_MODEL` | provider default | Model name/ID. |
| `ARC_RUNTIME_ADAPTER` | `local` | Where the workflow runs: `local`, `sim2l`, `service`, `auto`, `docker`, `slurm`, `k8s`. See {doc}`../core/runtime-adapters`. |
| `ARC_BACKEND` | *(inferred)* | Force a publish backend; otherwise `resolve_backend` infers one. |
| `ARC_STORAGE_MODE` | `local` | `required` makes the sim2l service backend mandatory. |
| `ARC_STRATEGY_<ROLE>` | *(unset)* | Override a role's strategy from the env, e.g. `ARC_STRATEGY_PLANNER=doe_lhs`. |
| `ARC_IDEATOR_CANDIDATES` | `3` | How many candidate hypotheses the default ideator generates. |
| `ARC_INPUTS_DIR` | `./data` | Session input folder scanned at startup. |
| `ARC_INPUTS_IMPORT_MODE` | `index` | Startup file handling mode: `index` or `copy`. |
| `ARC_INPUTS_RECURSIVE` | `1` | Recursively scan the input folder. |
| `ARC_INPUTS_MAX_FILE_MB` | `200` | Skip startup input files larger than this limit. |
| `ARC_INPUTS_MAX_FILES` | `1000` | Maximum number of startup input files to index per session. |
| `ARC_FILES_ALLOWED_ROOTS` | *(unset)* | Extra local directories the HTTP API may import files from, separated by `os.pathsep` (`:` on macOS/Linux, `;` on Windows). |
| `ARC_FILES_TRUSTED_LOCAL` | `0` | `1` lets the HTTP API import arbitrary local paths readable by the ARC process; use only for private local servers. |
| `SIM2L_HOME` | `~/.sim2l` | Root for per-session files. |
| `XDG_CONFIG_HOME` / `ARC_UI_ENV_PATH` | platform default | Config/`.env` discovery for the UI. |

## Execution & safety

| Variable | Default | Meaning |
|---|---|---|
| `ARC_EXECUTOR` / `ARC_LOCAL_EXECUTOR` | subprocess | `inprocess` opts into the faster, non-isolated local execution mode. |
| `ARC_LOCAL_EXEC_TIMEOUT` / `ARC_REMOTE_EXEC_TIMEOUT` | adapter default | Wall-clock timeout (seconds). |
| `ARC_JOB_DIR` | session dir | Working directory for remote adapter jobs. |

## API & UI security

| Variable | Default | Meaning |
|---|---|---|
| `ARC_API_TOKEN` | *(unset → open)* | Require `Authorization: Bearer <token>` on data/run endpoints. |
| `ARC_PROVIDER_ALLOWLIST` | upstream OpenWebUI default | Comma-separated provider base-URL allow-list. |
| `ARC_ALLOW_PRIVATE_PROVIDER_HOSTS` | `0` | `1` disables the loopback/private-IP SSRF guard. |
| `ARC_UI_HOST` / `ARC_UI_PORT` | `127.0.0.1` / `8888` | Browser-UI bind address. |

See {doc}`../architecture/security`.

## Providers

| Variable | Provider | Meaning |
|---|---|---|
| `OPENWEBUI_URL` / `OPENWEBUI_KEY` / `OPENWEBUI_MODEL` | `openwebui` | Gateway base URL, bearer token, default model. Fronts OpenWebUI / Purdue GenAI / Ollama. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | `anthropic` | (`arc-providers`) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | `openai` | (`arc-providers`) |

See {doc}`../guides/providers`.

## Coding backends (`arc-codex`, `arc-claude-code`)

| Prefix | Variables |
|---|---|
| `ARC_CODEX_*` | `COMMAND`, `MODEL`, `PROFILE`, `SANDBOX`, `APPROVAL_POLICY`, `INTERACTIVE_APPROVAL_POLICY`, `ALLOW_NON_INTERACTIVE`, `SKIP_GIT_REPO_CHECK`, `ALLOWED_IMPORTS`, `CONFIG`, `WORKDIR`, `ARGS`/`GLOBAL_ARGS`/`EXTRA_ARGS`, `TIMEOUT_SECONDS` |
| `ARC_CLAUDE_CODE_*` | `COMMAND`, `MODEL`, `FALLBACK_MODEL`, `PERMISSION_MODE`, `EFFORT`, `OUTPUT_FORMAT`, `ALLOWED_TOOLS`/`DISALLOWED_TOOLS`, `SYSTEM_PROMPT`/`APPEND_SYSTEM_PROMPT`, `MAX_BUDGET_USD`, `ARGS`/`EXTRA_ARGS`, `TIMEOUT_SECONDS` |

Non-chat Codex/Claude Code runs require an approval callback unless
`ARC_CODEX_ALLOW_NON_INTERACTIVE=true` or
`ARC_CLAUDE_CODE_ALLOW_NON_INTERACTIVE=true`. Those non-interactive modes are
full-trust automation: do not enable them when environment variables or package
configuration can be influenced by an untrusted caller.

## Runtime-adapter packages

| Prefix | Variables |
|---|---|
| `ARC_DOCKER_*` | `IMAGE`, `COMMAND`, `NETWORK`, `CPUS`, `MEMORY` |
| `ARC_SLURM_*` | `PARTITION`, `ACCOUNT`, `TIME`, `PYTHON`, `SCRATCH` |
| `ARC_K8S_*` / `ARC_KUBECTL_COMMAND` | `IMAGE`, `NAMESPACE`, kubectl path |

## Sim2L services & MCP

| Variable | Meaning |
|---|---|
| `SIM2L_CATALOG_URL` / `SIM2L_RESULTS_URL` / `SIM2L_CACHE_URL` | Service endpoints. |
| `SIM2L_USERNAME` / `SIM2L_PASSWORD` | Service login (authenticated services). |
| `SIM2L_ADMIN_PASSWORD` | Local-service admin password. |
| `ARC_SIM2L_START_MCP` | `1` to auto-start the sim2l MCP process from chat. |
| `SIM2L_MCP_TRANSPORT` | MCP transport (`streamable-http` default). |

See {doc}`../guides/sim2l-services`.

## Other integrations

| Variable | Used by | Meaning |
|---|---|---|
| `MP_API_KEY` | `arc-materials` searcher | Materials Project API key. |
| `GITHUB_TOKEN` / `ARC_GITHUB_REPO` / `ARC_GITHUB_BRANCH` / `ARC_GITHUB_PREFIX` | `GitHubBackend` | Publish artifacts to a GitHub repo. |
| `CO_SCIENTIST_REPO` / `CO_SCIENTIST_DATA_DIR` | `arc-coscientist` | Reference implementation paths. |

## `arc.toml` sections

| Section | Keys |
|---|---|
| `[arc]` | `name`, `version` |
| `[packages]` | `paths`, `enabled`, `disabled` |
| `[runtime]` | `adapter`, `workspace` |
| `[provider]` | `name`, `base_url`, `model` (token via env) |
| `[memory]` | `artifact_registry`, `results_store`, `provenance_log` |
| `[api]` | `host`, `port`, `reload`, `provider_base_url_allowlist` |
| `[strategies]` | `<role> = <strategy>` overrides |
| `[extensions.<name>]` | `enabled`, `entrypoint`/`path`, plus per-extension keys (mcp, openapi, vector-memory, docker/slurm/k8s-runtime, knowledge-graph) |

```{note}
There is no `[extensions.web-ui]` block — the browser UI is a standalone app
(`arc ui`), not an extension.
```
```{note}
This table is maintained by hand from the codebase (`grep os.environ`, the
`.env.example`, and `arc.toml`). When you add a new knob, add it here too
(`doc_todo.md` D3.3/D3.7).
```
