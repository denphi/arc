# ARC-Sim2L

**Autonomous Research Coder for Sim2L artifacts.**

ARC is a multi-agent framework for autonomous scientific research. It orchestrates iterative research workflows built around [Sim2L](https://sim2l.readthedocs.io/) artifacts, inspired by the MARS (Multi-Agent Research System) architecture.

```
Ideate → Plan → Build → Validate → Execute → Review → Improve → (repeat)
```

---

## Architecture

```
arc-core       kernel, event bus, session manager, package loader
arc-sim2l      artifact creation, validation, registration, execution
arc-mars       MARS-inspired research strategy and iterative search
arc-materials  materials science domain knowledge and evaluators
```

See [design/architecture.md](design/architecture.md) for the full layered diagram.

---

## Quick Start

```bash
# Install dependencies
pip install fastapi uvicorn pydantic pyyaml httpx python-dotenv

# Copy and configure environment
cp .env.example .env
# Edit .env: set ARC_PROVIDER=anthropic and ANTHROPIC_API_KEY=...

# Run a single research iteration
python3 -m arc.cli.main run "Explore how temperature affects material stability"

# Start the API server
python3 -m arc.cli.main serve
# → http://localhost:8000/docs
```

---

## Chat

```bash
# Built-in lightweight artifact builder
python3 examples/chat.py --stub

# LLM-backed chat via OpenWebUI
python3 examples/chat.py \
  --token "$OPENWEBUI_KEY" \
  --model "gpt-oss:120b" \
  --url "https://genai.rcac.purdue.edu/api"
```

### Sim2L service authentication

When ARC pushes artifacts and results to authenticated Sim2L services, the
chat login uses these environment variables:

```bash
SIM2L_USERNAME=XXX
SIM2L_PASSWORD=XXX
```

Set them in `arc/.env` when your catalog, results, or cache service requires
authentication. The local `./start_services.sh` helper starts services with
`--no-auth`, so these variables are not needed for that local development mode.
For authenticated services, use the actual Sim2L service account; production
deployments should not rely on `admin/admin`.

Do not use the PostgreSQL database credentials here. `sim2l` /
`sim2l_password` is the default database account, not the Sim2L service login.
For the built-in local service admin account, use `SIM2L_USERNAME=admin` and
set `SIM2L_PASSWORD` to either the value in `~/.sim2l/admin_password` or the
same value you provide as `SIM2L_ADMIN_PASSWORD` when starting services.

### Sim2L MCP tools

ARC can start the optional sim2l MCP server from the same service manager:

```text
/services start mcp
```

Regular `/services start` starts only the core cache/catalog/results services.
Set `ARC_SIM2L_START_MCP=1` to have chat start the MCP process at startup when
it is not already running. The MCP transport defaults to `streamable-http` and
can be overridden with `SIM2L_MCP_TRANSPORT`.

Inside chat:

```text
/packages
/coder
/coder codex
/coder claude
/coder builder
/package enable arc-codex
/package enable arc-claude-code
/package disable arc-codex
```

The chat prompt supports persistent history and common Emacs-style editing keys:
Up/Down for history, `Ctrl+A` start of line, `Ctrl+E` end of line, `Ctrl+K`
delete to end of line, and `Ctrl+U` delete before cursor.

`/coder codex` selects `arc-codex:coder`. The Codex package shells out to the Codex CLI. Configure it with `ARC_CODEX_COMMAND`, `ARC_CODEX_MODEL`, `ARC_CODEX_SANDBOX`, `ARC_CODEX_APPROVAL_POLICY`, `ARC_CODEX_INTERACTIVE_APPROVAL_POLICY`, and `ARC_CODEX_EXTRA_ARGS`. Non-chat Codex runs require an approval callback by default; set `ARC_CODEX_ALLOW_NON_INTERACTIVE=true` only when the caller is intentionally running without user approval prompts.

`/coder claude` selects `arc-claude-code:coder`. The Claude Code package shells out to the Claude Code CLI. Configure it with `ARC_CLAUDE_CODE_COMMAND`, `ARC_CLAUDE_CODE_MODEL`, `ARC_CLAUDE_CODE_PERMISSION_MODE`, `ARC_CLAUDE_CODE_EFFORT`, and `ARC_CLAUDE_CODE_EXTRA_ARGS`.

---

## API

```
POST /research/start         Run a full research loop iteration
POST /research/iterate       Run multiple iterations
POST /artifact/create        Register a new artifact
GET  /artifact/{id}          Fetch an artifact record
GET  /artifact               List all artifacts
POST /execution/run          Execute an artifact
GET  /execution/status/{id}  Get run status
GET  /results/{id}           Fetch execution results
GET  /results                List all results
POST /review/run             Review an execution result
GET  /health                 Health check
```

---

## Browser UI

A standalone browser dashboard runs over the same core primitives as the
CLI and API (sessions, artifacts, results, the research workflow) — it does
**not** import the terminal chat loop.

```bash
arc ui                              # → http://127.0.0.1:8888  (default port)
# or directly, with overrides:
python -m arc.ui --host 0.0.0.0 --port 8888
```

It binds to `127.0.0.1:8888` by default (8080 is avoided — it commonly
collides with a Docker-proxied Jupyter on the same host). Open the UI at the
explicit IPv4 address `http://127.0.0.1:8888`; `localhost` may resolve to IPv6
and hit a different service. When exposed beyond localhost, set
`ARC_API_TOKEN` to require a bearer token (the data + run endpoints are then
gated; `/`, `/assets/*`, and `/api/health` stay open so the page can load and
prompt for the token). Host/port also read from `ARC_UI_HOST` / `ARC_UI_PORT`.

The UI offers a chat-style thread, a sessions drawer, an artifact/result
inspector with a file viewer and schema-derived execution form, and live
run progress over Server-Sent Events.

### Session history: three distinct records

These are easy to conflate; the UI uses all three:

| Record | What it is | Where | Persisted by |
|---|---|---|---|
| **`run_history`** | Structured per-iteration run summaries (inputs, outputs, status, review). The authoritative experiment log. | `session.json` | workflow / API / CLI |
| **Thread (transcript)** | The display timeline of typed messages and command results shown in the UI chat pane. The UI's transcript of record. | `<session>/ui_thread.json` | the browser UI |
| **CLI line history** | A single *global* line-editing history for the REPL (up-arrow recall), not per session. | `~/.../.arc_chat_history` | the CLI chat loop |

For a session with no `ui_thread.json` (e.g. one driven from the CLI before
the UI existed), the UI **derives** a read-only timeline from `run_history`
(the goal plus one message per iteration) so the pane isn't empty. That
derived view is lossy — it reconstructs from run summaries, not the actual
messages a user typed. A faithful CLI↔UI replay would require promoting a
per-session transcript into core (`arc.session`/`arc.memory`, never
`arc.chat`); deferred until CLI replay is an actual goal.

---

## Packages

| Package | Type | Description |
|---|---|---|
| [arc-sim2l](arc/packages/arc-sim2l/) | artifact | Sim2L artifact lifecycle |
| [arc-mars](arc/packages/arc-mars/) | strategy | MARS-inspired iteration and search |
| [arc-materials](arc/packages/arc-materials/) | domain | Materials science evaluators and prompts |
| [arc-codex](arc/packages/arc-codex/) | coding | Codex-backed artifact generation |
| [arc-claude-code](arc/packages/arc-claude-code/) | coding | Claude Code-backed artifact generation |

---

## LLM Providers

Without a configured provider, all agents use deterministic stub logic (good for testing).

To enable LLM-powered agents, set in `.env`:

```
ARC_PROVIDER=anthropic
ARC_MODEL=claude-opus-4-7
ANTHROPIC_API_KEY=your-key-here
```

Or for OpenAI:
```
ARC_PROVIDER=openai
ARC_MODEL=gpt-4.1
OPENAI_API_KEY=your-key-here
```

---

## Testing

```bash
python3 -m pytest tests/ -v
```

41 tests covering agents, adapter, memory, evaluators, API routes, package loading, and the full workflow.

---

## Design Documents

| Document | Description |
|---|---|
| [design/architecture.md](design/architecture.md) | System architecture and component roles |
| [design/requirements.md](design/requirements.md) | Functional and non-functional requirements |
| [design/packages.md](design/packages.md) | Package system and composition model |
| [design/coding-agents.md](design/coding-agents.md) | Codex/Claude Code package design and session selection |
| [design/contracts.md](design/contracts.md) | Agent, skill, adapter, and provider interfaces |
| [design/workflows.md](design/workflows.md) | Workflow YAML format and execution model |
| [design/extensions.md](design/extensions.md) | Available extensions and how to add new ones |

---

## Adding a Package

1. Create `arc/packages/your-package/package.yaml`
2. Implement agents in `arc/packages/your-package/agents/`
3. Define skills in `arc/packages/your-package/skills/*.md`
4. Add workflows in `arc/packages/your-package/workflows/*.yaml`
5. Register in `arc.toml` under `[packages] paths`

See [design/packages.md](design/packages.md) for the full manifest format.
