# ARC Architecture

This document captures the **current** shape of the `arc/` codebase. It
is meant as an onboarding map, not an aspiration — every module shown
exists and is wired up at the time of writing.

Last updated: 2026-06-04.

---

## Top-level layout

```
arc/
├── chat/             Interactive REPL (the focus of recent refactors)
├── cli/              Typer-based `arc <subcommand>` entry points
├── api/              FastAPI HTTP surface (server + routes + auth)
├── core/             Kernel, loader, registry, session, config
├── orchestrator/     ResearchWorkflow — runs agents end-to-end
├── runtime/          Sim2L / local execution adapters
├── memory/           Artifact registry, results store, provenance log
├── schemas/          Pydantic models (Goal, Plan, Execution, Review…)
├── packages/         Plug-in agent packages (arc-sim2l, arc-codex…)
├── providers/        LLM provider clients (anthropic, openai, openwebui)
├── agents/           Declarative YAML agent definitions (Phase 4)
├── skills/           Markdown skill files with YAML frontmatter
├── contracts/        Protocols shared by core ↔ packages
├── extensions/       Optional extras (MCP, vector memory, web-UI…)
├── services.py       Daemon manager for sim2l cache/catalog/results
├── session.py        On-disk session store under ~/.sim2l/code/
└── utils/            Cross-cutting helpers (atomic_write_text, …)
```

---

## Layered view

```
┌─────────────────────────────────────────────────────────────────┐
│                              CLI                                │
│            arc chat   arc run   arc serve   arc info            │
│                       (arc/cli/main.py)                         │
└──────────────────────────────────┬──────────────────────────────┘
                                   │
                         ┌─────────┴─────────┐
                         ▼                   ▼
                  ┌───────────────┐   ┌──────────────┐
                  │   arc.chat    │   │   arc.api    │
                  │   (REPL)      │   │ (FastAPI)    │
                  └──────┬────────┘   └──────┬───────┘
                         │                   │
                         └─────────┬─────────┘
                                   ▼
              ┌──────────────────────────────────────┐
              │   arc.orchestrator.ResearchWorkflow  │
              │  builds context, owns agent registry │
              └──────────┬────────────┬──────────────┘
                         │            │
                  ┌──────┴───┐  ┌─────┴────────┐
                  ▼          ▼  ▼              ▼
              ┌───────┐  ┌────────┐    ┌──────────────┐
              │ core  │  │  runtime│   │   memory     │
              │ kernel│  │ adapters│   │ artifact reg.│
              │ regis-│  │ sim2l / │   │ results store│
              │ try   │  │ local   │   │ provenance   │
              └───┬───┘  └────┬───┘    └──────────────┘
                  │           │
                  ▼           ▼
           ┌─────────┐  ┌──────────┐
           │packages │  │ runtime  │
           │ /agents │  │ services │
           │ /skills │  │ (daemons)│
           └────┬────┘  └──────────┘
                │
                ▼
         ┌─────────────┐
         │  providers  │
         │ anthropic / │
         │ openai /    │
         │ openwebui   │
         └─────────────┘
```

---

## arc.chat — the REPL package (most-developed subsystem)

```
┌────────────────────── arc/cli/main.py ──────────────────────┐
│  arc chat [--plan] [--check] [--events] [--session SID]     │
└────────────────────────────┬────────────────────────────────┘
                             ▼
                 ┌─────────────────────┐
                 │  arc.chat.loop      │   ← chat_loop() coroutine
                 │  REPL dispatcher    │     dispatches per user line
                 └──┬─────┬─────┬──────┘
                    │     │     │
   ┌────────────────┘     │     └────────────────┐
   ▼                      ▼                      ▼
┌──────────────┐  ┌────────────────┐  ┌────────────────────┐
│ chat.router  │  │chat.commands/  │  │ chat.research/     │
│ (heuristic + │  │ registry +     │  │ Pipeline + Phases  │
│  LLM intent) │  │ 17 slash cmds  │  │ + Hooks            │
└──────┬───────┘  └────────┬───────┘  └──────────┬─────────┘
       │                   │                     │
       │                   ▼                     ▼
       │            ┌──────────────┐    ┌────────────────┐
       │            │ChatState     │    │ Adapters call  │
       │            │(prim_goal,   │    │ workflow.adapter
       │            │ artifact,    │    │ + agents       │
       │            │ refinements, │    │ + results.save │
       │            │ target,      │    │ + provenance   │
       │            │ permission,  │    └────────────────┘
       │            │ budget,…)    │
       │            └──────────────┘
       ▼
┌──────────────┐
│ chat.router  │  ARC_CHAT_V2=1 → tool-call routing
│ _v2 + tools/ │     ──▶ ToolRegistry.dispatch
│ + agents/    │             ──▶ Pydantic schema validation
└──────────────┘
```

### chat package modules (canonical homes)

| Module | Purpose |
|--------|---------|
| `chat/loop.py` | `chat_loop` REPL coroutine, `run_research`, `_run_with_continuation`, `_post_approval_menu`, dispatch handlers |
| `chat/classifier.py` | Heuristic + LLM intent classifier (`is_question`, `llm_classify_intent`) |
| `chat/parsers.py` | Free-text parsers (`parse_target`, `parse_refinement_target`, `parse_target_command`, `normalize_chat_command`, …) |
| `chat/router.py` | `route_input` (v1) + `Route` dataclass |
| `chat/router_v2.py` | `route_via_tools` — LLM emits a tool call (behind `ARC_CHAT_V2=1`) |
| `chat/registry.py` | `SlashCommand` + `CommandRegistry` (`/help`, `/run`, /17 commands) |
| `chat/commands/*.py` | One slash-command handler per file |
| `chat/research/pipeline.py` | `Phase` / `Pipeline` / `PipelineHook` abstractions |
| `chat/research/phases.py` | `POST_BUILD_PHASES` (validation→exec→review→reflect→provenance) |
| `chat/research/hooks.py` | Reusable hooks (`auto_save_after`, `phase_events`) |
| `chat/research/targets.py` | `pct_off`, `registry_keys_match` — distance-to-target |
| `chat/tools/registry.py` | `Tool`, `ToolRegistry.dispatch` (Pydantic-validated) |
| `chat/tools/routing.py` | The 4 router tools (`start_research_goal`, `refine_goal`, `set_target`, `answer_question`) |
| `chat/agents/definition.py` | `AgentDefinition` Pydantic model + YAML loader + `resolve_agent` |
| `chat/state.py` | `ChatState` dataclass — single source of truth for session data |
| `chat/session_io.py` | `save_session` / `restore_session` (writes `session.json`) |
| `chat/events.py` | `ChatEvent`, `AnsiSink` / `JsonlSink` / `StdoutJsonSink` / `MultiSink` |
| `chat/plan_mode.py` | Global `--plan` flag, `check_plan_gate`, `@gated` decorator |
| `chat/check.py` | `arc chat --check` dry-run report builder |
| `chat/check_render.py` | ANSI / JSON renderers for `CheckReport` |
| `chat/skill_loader.py` | Markdown skills with YAML frontmatter + path-traversal validation |
| `chat/ui.py` | ANSI helpers (`c`, `header`, `step`, `ok`, `warn`, `err`, `hr`) |
| `chat/input.py` | Prompt-toolkit wrappers (`chat_input`, `chat_input_async`) |
| `chat/io_utils.py` | `check_sim2l_services`, `print_banner`, `install_sigint_handler` |
| `chat/_env.py` | `env_flag(name)` — single truthy-env parsing helper |

---

## Flow: a single user input

```
                    ┌──────────────────┐
                    │ user types "..." │
                    └────────┬─────────┘
                             ▼
            ┌────────────────────────────────┐
            │  chat_input_async (prompt-tk)  │
            └────────────────┬───────────────┘
                             ▼
                     ┌────────────────┐
                     │ normalize \\…  │
                     └───────┬────────┘
                             ▼
              ┌─────────────────────────────┐
              │   route_input(raw, …)       │
              │   or _route_via_v2 (v2)     │
              └───────────────┬─────────────┘
                              │
   ┌──────────┬────────────┬──┴──────────┬──────────────┐
   ▼          ▼            ▼             ▼              ▼
 command   noop       question         goal         refinement
   │                       │             │              │
   ▼                       ▼             ▼              ▼
registry              _answer_      _handle_goal    _handle_
.dispatch             question      → confirm →     refinement
   │                       │       reset_for_new_  → run_with_
   ▼                       ▼         goal →           continuation
slash handler        provider.       run_with_
(commands/*.py)      complete()      continuation
   │                                       │
   ▼                                       ▼
state mutation                  ┌───────────────────────┐
+ optional                      │  run_research(…)      │
side effects                    │   ├─ Ideation         │
                                │   ├─ Catalog reuse    │
                                │   ├─ Planning         │
                                │   ├─ Build artifact   │
                                │   ├─ Curate           │
                                │   ├─ Sim2L register   │
                                │   └─ Pipeline.run(    │
                                │       POST_BUILD_PHASES)
                                └───────────────────────┘
                                          │
                                          ▼
                  ┌──────────────────────────────────────┐
                  │   Validation → Execution → Review →  │
                  │   Reflection → Provenance            │
                  │   (arc.chat.research.phases)         │
                  └──────────────────────────────────────┘
```

---

## The orchestrator (research workflow)

```
arc.orchestrator.workflow.ResearchWorkflow
├── adapter         arc.runtime.{sim2l_adapter, local}
├── artifacts       arc.memory.artifact_registry.ArtifactRegistry
├── results         arc.memory.results_store.ResultsStore
├── provenance      arc.memory.provenance.ProvenanceLog
├── provider        arc.providers.{anthropic, openai, openwebui}
├── registry        arc.core.registry.ComponentRegistry
│                     ├── agents     (ideator, planner, builder, …)
│                     ├── skills     (markdown files)
│                     ├── workflows  (yaml)
│                     ├── evaluators
│                     ├── prompts
│                     ├── templates
│                     ├── constraints
│                     └── adapters
└── _context        AgentContext { session_id, memory, iteration, … }
```

The workflow is constructed at chat-startup time and passed by reference
into every agent that runs. Agents read state through
`workflow._context.memory` (a shared dict); the chat layer reads/writes
through `ChatState` which wraps the same dict.

### Sim2L service authentication

When catalog, results, or cache services require authentication, ARC logs in
at chat startup and passes service session ids to the runtime adapter. Configure
the service credentials in the environment or in `arc/.env`:

```bash
SIM2L_USERNAME=XXX
SIM2L_PASSWORD=XXX
```

The local `./start_services.sh` helper starts Sim2L services with `--no-auth`,
so these variables are optional for that development path. Authenticated
deployments should set them to the actual service account instead of relying on
development defaults.

The service login is separate from the PostgreSQL account. The default
PostgreSQL `sim2l` / `sim2l_password` credentials do not authenticate to the
Sim2L service API. For the built-in service admin account, use
`SIM2L_USERNAME=admin` and set `SIM2L_PASSWORD` to the persisted value in
`~/.sim2l/admin_password` or to the same value used for
`SIM2L_ADMIN_PASSWORD`.

---

## Agent packages

```
arc/packages/
├── arc-sim2l/        ← built-in agents for materials research
│   └── agents/{ideator, planner, builder, reviewer, reflector, curator, optimizer}
├── arc-codex/        ← OpenAI Codex coder backend
├── arc-claude-code/  ← Anthropic Claude Code coder backend
├── arc-materials/    ← Materials-domain skills / prompts
└── arc-mars/         ← Mars-domain (geology / planetary science)
```

Each package contributes via a `package.yaml` manifest discovered by
`arc.core.loader.load_package`. Manifests declare:

```yaml
provides:
  agents:        [{name, entrypoint}]
  skills:        [<markdown file>]
  workflows:     [{name, path}]
  evaluators:    [...]
  prompts:       [...]
  templates:     [...]
  constraints:   [...]
  vocabularies:  [...]
  runtime_adapters: [...]
```

`arc/agents/*.yaml` is the **Phase 4** declarative-agent location —
`reviewer.yaml` is the proof-of-concept.

---

## Runtime services (separate processes)

```
arc.services  (PID-file daemon manager)
├── cache    :8001   sim2l.services.cache_service     (Flask)
├── catalog  :8002   sim2l.services.catalog_service   (Flask)
├── results  :8003   sim2l.services.results_service   (Flask)
└── mcp      :8010   sim2l.mcp.server                 (optional)
```

These are sim2l microservices the chat depends on for catalog reuse,
result deduplication, and shared workflow caching. The chat layer
talks to them via HTTP and can start/stop/restart them via
`/services start|stop|restart [name]`.

The MCP process is optional and must be started explicitly with
`/services start mcp`, or automatically by setting `ARC_SIM2L_START_MCP=1`.
Omitting the service name starts only the core cache/catalog/results services.

---

## On-disk state

```
~/.sim2l/code/<session_id>/
├── session.json              # ChatState snapshot (goal, target, …)
├── events.jsonl              # --events jsonl stream (Phase 2)
├── runs/                     # ResultsStore
├── memory/provenance.jsonl   # ProvenanceLog
└── arc_sim2l.db              # sqlite for runs/artifacts

~/.sim2l/pids/                # daemon PID files
~/.sim2l/logs/                # daemon log files
~/.config/arc/agents/         # user-supplied AgentDefinitions (opt-in)
~/.config/arc/skills/         # user-supplied skills (opt-in)
./.arc/agents/                # project-local agents (opt-in via ARC_TRUST_PROJECT_AGENTS=1)
./.arc/skills/                # project-local skills (opt-in via ARC_TRUST_PROJECT_SKILLS=1)
```

---

## Feature flags

| Flag | Effect |
|------|--------|
| `ARC_CHAT_V2=1` | Use the v2 tool-call router instead of the heuristic + LLM classifier |
| `ARC_TRUST_PROJECT_SKILLS=1` | Allow loading skills from `./.arc/skills/` |
| `ARC_TRUST_PROJECT_AGENTS=1` | Allow loading agent definitions from `./.arc/agents/` |
| `ARC_API_TOKEN=<value>` | Require Bearer auth on the FastAPI server |
| `ARC_PROVIDER_ALLOWLIST=<csv>` | Restrict `/provider/models` to a host allow-list |
| `ARC_ALLOW_PRIVATE_PROVIDER_HOSTS=1` | Permit loopback/private-IP provider URLs |
| `SIM2L_HOME=<path>` | Override `~/.sim2l/code/` (used heavily in tests) |
| `SIM2L_CATALOG_URL` / `SIM2L_RESULTS_URL` / `SIM2L_CACHE_URL` | Per-service URL override |
| `ARC_SIM2L_START_MCP=1` | Start the optional sim2l MCP process at chat startup if missing |
| `SIM2L_MCP_TRANSPORT=<name>` | MCP transport for `/services start mcp` (default: `streamable-http`) |
| `XDG_CONFIG_HOME=<path>` | Override the user-config base dir |

---

## CLI flags (`arc chat`)

```
arc chat [options]

Connectivity & auth:
  --provider, -p   anthropic | openai | openwebui
  --token, -t      bearer/API key
  --model, -m      model id
  --url, -u        provider base URL
  --stub           run without an LLM

Session management:
  --session, -s SID         resume a session
  --list-sessions           print sessions and exit
  --delete-session SID      delete a session and exit
  --delete-all-sessions     wipe sessions and exit

Behaviour & observability:
  --max-iterations N        autonomous iteration budget per goal
  --plan                    no-side-effects "what would happen" mode
  --events <kind>           ansi | jsonl | stdout-json | multi
  --events-path PATH        explicit events.jsonl location
  --check                   dry-run config / health probe and exit
  --check-format <fmt>      ansi | json output for --check
```

---

## Test layout

The test suite is organized by subsystem:

```text
tests/
├── chat / command / router / pipeline tests
├── API, UI, provider, security, and session tests
├── package loader / manifest / local-package tests
├── runtime adapter and executor tests
├── workflow / strategy / recipe / audit tests
├── asset loader and FileAsset tests
└── coding backend tests
```

Use `python -m pytest -q` for the current count and timing. Avoid recording
hard-coded totals here; they change with every review pass.

---

## Key contracts and invariants

* `arc.chat.__init__` documents the **frozen-dataclass mutation convention**:
  `Route`, `CheckItem`, `SlashCommand`, `SkillRecord`, `Tool`,
  `ToolDecision` all use `frozen=True`. Their inner collections are
  conventionally read-only.

* **`ChatState`** is the single source of truth for session data within
  the chat layer. It wraps `workflow._context.memory` by reference so
  writes through `state.primary_goal = ...` are visible to agents that
  still read `ctx.memory` directly.

* **Plan mode** is a global flag (`arc.chat.plan_mode.is_plan_mode()`).
  Side-effect call sites self-gate via `check_plan_gate(label)`.
  Currently enforced in `ArtifactRegistry.register`,
  `_register_artifact_with_sim2l`, and `ToolRegistry.dispatch`
  for tools declared with `side_effects=True`.

* **Cost budget**: `ChatState.router_calls` is bumped on every v1
  uncertain-input LLM classification call AND every v2 routing call;
  the budget is `state.router_call_budget` (default 200). Exhaustion
  raises `RouterBudgetExceeded` BEFORE the provider request.
  Slash commands and stub mode (no provider) don't tick the counter.

* **Path traversal**: `arc.session.validate_session_id` is consulted
  whenever a session id flows into a filesystem path
  (skill loader, agent loader, event sink materialisation).

* **YAML safety**: every YAML load in `arc.chat.*` uses `yaml.safe_load`.
  A static-check test enforces this.

---

## Phases of the recent refactor (history, in case it's useful context)

| Phase | What it added |
|------:|--------------|
| 1 | Split `chat.py` into the `arc/chat/` package; CommandRegistry; Route; ChatState |
| 2 | `arc chat --check`, structured events with sinks, `--plan` mode |
| 3 | `Phase` / `Pipeline` / hooks abstraction; YAML-frontmatter skill loader |
| 4 | `AgentDefinition` Pydantic model + YAML loader; `arc.chat.tools.*` registry; v2 router (behind `ARC_CHAT_V2=1`); reviewer migrated to YAML |
| R3 / Q / etc. | Security tightening (path-traversal, schema validation, plan-mode gates) + code-quality cleanup (extracted classifier, parsers, targets, session_io modules) |

This document supersedes the per-phase / per-cycle notes scattered
through the chat package's source files — those were progressively
stripped in the Q-cycle so the source only describes *what is*, not
*how it got there*.
