# Local packages

*Develop a package outside the repo tree with `arc package init` / `validate`
and the `path` + `class` manifest style.*

## Workflow

Use local packages when you want to develop ARC extensions outside the core
repository.

1. Create a package directory with `arc package init`.
2. Add implementations and declare them in `package.yaml`.
3. Reference the package path from `arc.toml` or a session/package command.
4. Run `arc package validate <path>` before using it in a workflow.

Local packages can provide strategies, agents, skills, workflows, loaders,
providers, runtime adapters, extensions, audit actions, and report sections.
The loader imports local code by `path` + `class` or by `entrypoint`, then
registers components with package provenance.

## Boundaries

Local packages should not require edits to ARC core. Configuration should flow
through package-declared config keys and environment variables, and generated
agents should implement the same contracts as bundled package components.
Validation errors should point to the manifest entry or file that needs to be
fixed.
