# Package manifest

*The `package.yaml` reference: `name`, `config`, and every `provides.*`
group.*

```yaml
name: arc-my-package          # required; the package's identity
config:                       # optional; declares env vars the package reads
  - name: MY_API_KEY
    required: false
    secret: true
    description: API key for the foo service.

provides:
  agents: …
  strategies: …
  skills: …
  workflows: …
  scripts: …
  runtime_adapters: …
  providers: …
  loaders: …
  evaluators: …
  detectors: …
  prompts: …
  templates: …
  constraints: …
  vocabularies: …
  extensions: …
  audit_actions: …
  report_sections: …
```

## `provides.*` groups

| Group | Entry shape | Registered as |
|---|---|---|
| `agents` | `{name, entrypoint}` or `{name, path, class}` | a named agent (direct lookup) |
| `strategies` | `{role, name, entrypoint\|path+class, default?, description?, aliases?}` | a role strategy (resolver-aware) |
| `skills` | a `.md` path string | a `MarkdownSkill` (named by file stem) |
| `workflows` | `{name, path}` (YAML) | a registered workflow |
| `scripts` | `{name, path, runtime}` | a package-declared script runnable through the script runner |
| `runtime_adapters` | `{name, entrypoint\|path+class, aliases?}` | a runtime adapter |
| `providers` | `{name, entrypoint\|path+class}` | an LLM provider class |
| `loaders` | `{name, entrypoint}` or `{name, path, class}` | a file/data asset loader |
| `evaluators` / `detectors` | `{name, entrypoint\|path}` or a name string | a resource |
| `prompts` / `templates` / `constraints` / `vocabularies` | a name string (resolved from the package subdir) | a resource |
| `extensions` | `{name, entrypoint\|path+class, enabled?, …}` | an extension definition |
| `audit_actions` | `{name, phase, entrypoint, blocking?, priority?}` | an audit action |
| `report_sections` | `{name, section_name, entrypoint}` | a report contributor |

```{note}
Strategies and agents are different: `provides.agents` registers an agent for
direct lookup (`get_agent` / explicit `package:agent` workflow steps);
`provides.strategies` registers a *role implementation* the resolver can pick.
See {doc}`roles`.
```

## Skill bundles

`provides.skills` can point at either a flat Markdown file or a bundle entry:

```yaml
provides:
  skills:
    - skills/pde-problem-extractor/SKILL.md
```

When the path ends in `SKILL.md`, ARC treats the parent folder as the skill
bundle root. The registered `MarkdownSkill` can safely list/read files under
that root, such as `references/`, `scripts/`, and `agents/`; `..` traversal is
rejected and size caps apply.

## File loaders

Packages can contribute domain-specific file loaders:

```yaml
provides:
  loaders:
    - name: mesh_loader
      path: loaders/mesh.py
      class: MeshLoader
```

Loaders are package-owned components. `/package disable <name>` prevents that
package's loaders from creating new derived assets. See
{doc}`../core/file-assets`.

## Package scripts

Scripts must be explicitly declared; markdown skill instructions cannot run
arbitrary package files implicitly.

```yaml
provides:
  scripts:
    - name: export_report
      path: scripts/export_report.py
      runtime: python
```

The script runner executes declared Python scripts in a caller-provided
workspace, captures stdout/stderr, honors package disable, and can import
generated files as FileAssets.

## Runtime checks

Packages can describe local environment expectations without installing them:

```yaml
runtime:
  python: ">=3.10"
  commands:
    - name: gmsh
      required: false
  python_modules:
    - name: meshio
      required: false
  conda:
    - my-solver=1.0
```

Run `arc package doctor <dir>` to check these requirements. Missing required
items make doctor exit non-zero; `arc package validate` remains structural.

## Examples policy

Examples are package documentation, not first-class registry entries in the
current MVP. Keep examples under `examples/` and link them from `README.md`.
Do not declare `provides.examples` unless ARC grows a CLI/UI surface for
listing examples.

## Loading & validation

The loader (`arc/core/loader.py`) maps each group to a registry call and is
lenient — a single bad declaration is logged and skipped. Run
`arc package validate <dir>` to confirm every declared contribution actually
registered. See {doc}`local-packages`.
