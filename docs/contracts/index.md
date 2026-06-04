# Contracts

*The typed seams that make ARC pluggable. Every package-provided component
implements one of these abstract base classes; core only ever talks to the
contract, never a concrete class.*

A **contract** is an ABC in `arc/contracts/` (plus `BackendActions` in
`arc/runtime/backend.py`). Implement one, register it (usually via a
`package.yaml` `provides.*` entry), and ARC will discover and drive it.

## The universal context: `AgentContext`

Every agent, skill, audit action, and report section is handed an
`AgentContext`:

```python
@dataclass
class AgentContext:
    session_id: str
    iteration: int = 0
    config: dict[str, Any]    # package-declared config resolved from env
    memory: dict[str, Any]    # the shared session blackboard
```

`memory` is the **blackboard** the loop coordinates through. Well-known keys
include `provider`, `adapter`, `registry`/`component_registry`, `results`,
`provenance`, `target`, `run_history`, `current_artifact`, `current_plan`,
`strategy_overrides`, `packages`, `memory_search`/`memory_hooks`. A component
reads what earlier ones wrote and writes what later ones need. See
{doc}`../core/sessions` for what is persisted.

## The contract map

| Contract | File | Implemented by | Drives |
|---|---|---|---|
| `AgentContract` | `contracts/agent.py` | role strategies (ideator, planner, …) | the research loop steps |
| `SkillContract` | `contracts/skill.py` | Markdown skills | `provides.skills` |
| `RuntimeAdapterContract` | `contracts/adapter.py` | local/sim2l/docker/slurm/k8s | the **Execute** step |
| `BackendActions` | `runtime/backend.py` | Noop/Sim2l/GitHub | the **publish** steps (Register/Persist/Record) |
| `ProviderContract` | `contracts/provider.py` | openwebui/anthropic/openai | LLM calls (`complete`, `complete_structured`, `embed`) |
| `ExtensionContract` | `contracts/extension.py` | mcp/openapi/vector-memory/… | optional integrations loaded at startup |
| `AuditActionContract` | `contracts/audit.py` | package audit actions | lifecycle observation / blocking |
| `ReportSectionContract` | `contracts/audit.py` | package report contributors | the assembled research report |

```{tip}
The {doc}`../packages/roles` page lists which contract each loop **role**
uses, and the bundled default + alternative strategies for each. The
{doc}`../reference/api/index` renders the full ABC signatures from the
docstrings.
```

The canonical contract reference (with per-contract method signatures,
artifact states, and examples) follows.

---

```{include} ../../design/contracts.md
```
