# Component registry

*The in-memory map of every loaded component, plus the package-source map that
makes `/package disable` a real runtime filter.*

`ComponentRegistry` (`arc/core/registry.py`) holds a named slot per component
kind and a uniform **package-source map** keyed by `(kind, name)`.

## Slots

| Slot | Register | Get | List |
|---|---|---|---|
| agents | `register_agent(name, cls, package_name=…)` | `get_agent(name, package_name=None, disabled_packages=…)` | `list_agents()` |
| skills | `register_skill(name, skill, package_name=…)` | `get_skill(name, disabled_packages=…)` | `list_skills(disabled_packages=…)` |
| adapters | `register_adapter(name, cls, package_name=…)` | `get_adapter(name, disabled_packages=…)` | `list_adapters()` |
| providers | `register_provider(name, cls, package_name=…)` | `get_provider(name, disabled_packages=…)` | `list_providers()` |
| workflows | `register_workflow(name, defn, package_name=…)` | `get_workflow(name)` | `list_workflows()` |
| extensions | `register_extension(name, ext)` | `get_extension(name)` (None if missing) | — |
| extension defs | `register_extension_definition(name, defn)` | `get_extension_definition(name)` | `list_extension_definitions()` |
| evaluators / detectors | `register_*` | `get_*` | `list_*` |
| prompts / templates / constraints / vocabularies | `register_*` | `get_*` | `list_*` |
| audit actions | `register_audit_action(action, package_name=…)` | `audit_actions_for_phase(phase, disabled_packages=…)` | `list_audit_actions()` |
| report sections | `register_report_section(name, c, package_name=…)` | `report_section_contributors(disabled_packages=…)` | `list_report_sections()` |

## Package-source map

Every registration can record an owning package. The registry tracks this in a
single `(kind, name) → package_name` map:

- `record_source(kind, name, package_name)` — record ownership.
- `component_source(kind, name)` — look it up.
- `is_disabled(kind, name, disabled_packages)` — True when owned by a disabled
  package.
- `filter_disabled(kind, names, disabled_packages)` — drop disabled names (for
  list/UI surfaces).

This is what lets a session-disabled package's component become unselectable:
the `disabled_packages` parameter on `get_skill`/`get_provider`/`get_adapter`/
`get_agent` raises `KeyError` for a disabled owner. See
{doc}`../packages/enable-disable`.

### `PackageScopedRegistry`

`registry.scoped(package_name)` returns a proxy that stamps `package_name` on
every `register_*` call it forwards. The kernel hands this to an extension's
`initialize` so extension-created components keep their package ownership
without the extension code having to pass `package_name` itself.

## API reference

```{eval-rst}
.. automodule:: arc.core.registry
   :members:
   :undoc-members:
   :show-inheritance:
```
