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

## Contract guidelines

Contracts should stay narrow and stable. Components receive an
`AgentContext`, read only the memory/config they need, and return plain
serializable data or registered artifacts. Package code may depend on ARC
contracts, but ARC core should only depend on those contracts and registries.

Use this rule of thumb when adding extension points:

- add a contract when ARC needs to call a new kind of component;
- add a registry slot when packages need to contribute that component;
- add manifest validation so package authors get early errors;
- record package provenance so enable/disable and audit behavior remain
  predictable.

The API reference renders the concrete method signatures for each contract.
