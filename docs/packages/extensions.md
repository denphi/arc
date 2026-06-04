# Extensions

*Optional integrations loaded at startup — MCP tools, OpenAPI operations, a
vector store, a knowledge graph, remote runtimes.*

An **extension** implements `ExtensionContract`
(`initialize(config, registry)` / `shutdown()`), is declared via
`provides.extensions` (or an `[extensions.<name>]` block), and is loaded by
the kernel when enabled. The kernel hands it a **package-scoped registry**, so
components it registers keep the extension's package as their source (and are
filtered by `/package disable`).

The canonical extensions reference — the implementation-status table, provider
configuration, each bundled extension, authoring a new extension, and the
audit-action manifest — follows.

```{include} ../../design/extensions.md
```
