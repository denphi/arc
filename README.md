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
