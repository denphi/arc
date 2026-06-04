# Extensions

*Optional integrations loaded at startup — MCP tools, OpenAPI operations, a
vector store, a knowledge graph, remote runtimes.*

An **extension** implements `ExtensionContract`
(`initialize(config, registry)` / `shutdown()`), is declared via
`provides.extensions` (or an `[extensions.<name>]` block), and is loaded by
the kernel when enabled. The kernel hands it a **package-scoped registry**, so
components it registers keep the extension's package as their source (and are
filtered by `/package disable`).

## Bundled extensions

ARC ships extension packages for common integration patterns:

- `arc-mcp` exposes MCP tools as ARC skills.
- `arc-openapi` exposes OpenAPI operations as skills.
- `arc-vector-memory` provides persistent semantic memory.
- `arc-knowledge-graph` records structured experiment relationships.
- Runtime packages such as `arc-docker`, `arc-slurm`, and `arc-k8s` register
  additional runtime adapters.

## Authoring an extension

Implement `ExtensionContract`, declare it in `package.yaml`, and keep startup
behavior explicit. Extensions should register components through the
package-scoped registry they receive during initialization; this lets ARC
attribute components to the package and filter them when a package is disabled.

Use package config for credentials and service URLs. Extension startup should
fail clearly when required config is missing, and optional integrations should
degrade gracefully where possible.
