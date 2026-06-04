# Enabling & disabling packages

*Two distinct mechanisms — startup config and per-session toggle — and exactly
what each filters.*

| Mechanism | Where | Scope | Effect |
|---|---|---|---|
| `[packages].enabled` / `disabled` | `arc.toml` | process startup | packages are never *loaded* (allow-list / deny-list); shared by the kernel and the orchestrator (`filter_package_paths`). |
| `/package enable\|disable <name>` | chat / UI | the current session | the package stays loaded, but its components become *unselectable* for this session. |

## What `/package disable` filters

Disabling a package for a session makes **all** of its components
unselectable, via the registry's package-source map and the
`disabled_packages` parameter threaded through resolution:

- **strategies** — `resolve_role(..., disabled_packages=…)` refuses a disabled
  package's strategy and falls back to an enabled one; stack components from a
  disabled package are dropped.
- **agents** — direct `get_agent(name)` (non-role / explicit `package:agent`
  workflow steps) raises for a disabled owner.
- **skills / providers / adapters** — `get_skill`/`get_provider`/`get_adapter`
  raise; `build_provider` and `_build_adapter` degrade a disabled
  provider/adapter to stub/local.
- **audit actions / report sections** — excluded from dispatch / report
  assembly.
- **extension-created components** — keep their package ownership (the kernel
  hands extensions a package-scoped registry), so they're filtered too.

When a provider/adapter package is disabled mid-session,
`ResearchWorkflow.refresh_disabled_packages()` rebuilds the live
provider/adapter instances so they don't stay active.

```{seealso}
{doc}`../core/registry` (the package-source map) and {doc}`../core/strategies`
(resolution precedence).
```
