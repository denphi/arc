# Package loader

*Turns a `package.yaml` manifest into registered components.*

`load_package(package_dir, registry)` and `load_packages(paths, registry)`
(`arc/core/loader.py`) read a manifest and register each `provides.*` group.

## Manifest → registry

| `provides.<group>` | Becomes | Notes |
|---|---|---|
| `agents` | `register_agent(…, package_name)` | by `entrypoint` or `path`+`class` |
| `strategies` | `register_strategy(role, StrategySpec, …)` | spec built from the manifest; loaded lazily by file path |
| `skills` | `register_skill(stem, MarkdownSkill, …)` | each entry is a `.md` path |
| `workflows` | `register_workflow(name, yaml, …)` | YAML file under the package |
| `extensions` | `register_extension_definition(…)` | stamped with `_package_dir` + `_package_name` |
| `runtime_adapters` | `register_adapter(…, package_name)` | plus declared `aliases` |
| `providers` | `register_provider(…, package_name)` | the class, instantiated on demand |
| `evaluators` / `detectors` | `register_*` + `record_source` | by entrypoint/path or name |
| `prompts` / `templates` / `constraints` / `vocabularies` | `register_*` + `record_source` | resolved from the package subdir |
| `audit_actions` | `register_audit_action(action, package_name)` | manifest can override `phase`/`priority`/`blocking` |
| `report_sections` | `register_report_section(…, package_name)` | manifest can override `section_name` |

The loader is **lenient**: a single malformed declaration is logged and
skipped rather than aborting the whole startup. `arc package validate`
cross-checks declarations against the registry to catch those swallowed
failures (see {doc}`../packages/local-packages`).

## Hyphenated package imports

Bundled packages use distribution-style directory names with hyphens
(`arc-sim2l`), which aren't importable as dotted modules. `_import_class` /
`_import_declared` resolve a `module:Class` entrypoint via the filesystem and
register the module under an underscored alias, so the manifest format stays
stable while the modules remain loadable.

## API reference

```{eval-rst}
.. automodule:: arc.core.loader
   :members:
   :undoc-members:
   :show-inheritance:
```
