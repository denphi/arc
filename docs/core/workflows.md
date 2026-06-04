# Workflow YAML

*The declarative format the orchestrator executes: steps, references,
conditions, and approval checkpoints.*

A workflow is a YAML document a package registers via `provides.workflows`.
The bundled `research-loop` (in `arc-sim2l`) is the default; `arc-mars` ships
an iterative-improvement variant.

The canonical workflow reference — the step format, the reference syntax
(`step.output.x`, `memory.*`, `config.*`), condition expressions, approval
checkpoints, and execution modes — follows.

Workflow-level `inputs:` can also declare `type: file` entries. ARC binds
`file_*` IDs or local paths, auto-selects a single matching session asset by
role/media type, and can run required loaders before steps execute. See
{doc}`file-assets`.

---

```{include} ../../design/workflows.md
```
