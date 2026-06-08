# Workflow YAML

*The declarative format the orchestrator executes: steps, references,
conditions, and approval checkpoints.*

A workflow is a YAML document a package registers via `provides.workflows`.
The bundled `research-loop` (in `arc-sim2l`) is the default; `arc-mars` ships
an iterative-improvement variant.

## Step format

A workflow contains named steps. Each step selects a skill/agent/runtime action
and maps inputs from workflow inputs, previous step outputs, memory, or config.
References use dotted paths such as `inputs.paper`, `plan.parameters`,
`memory.current_artifact`, and `config.threshold`.

Steps may declare conditions so optional branches can be skipped, and approval
checkpoints so interactive runs can pause before expensive or sensitive work.
The orchestrator records every step result in the session context.

Workflow-level `inputs:` can also declare `type: file` entries. ARC binds
`file_*` IDs or local paths, auto-selects a single matching session asset by
role/media type, and can run required loaders before steps execute. See
{doc}`file-assets`.

Workflows can also act as pre-build context providers. Configure them under
`[workflows.build]` and ARC will run them before the selected builder, attach
their `build_context` output to the experiment plan, and keep the builder
strategy independent. See {doc}`build-context-workflows`.

---

## Execution model

The default research loop is still just a workflow: ideate, search, plan,
build, validate, execute, review, reflect, optimize, and curate. Packages can
register alternative workflows for richer loops or domain-specific pipelines.
Workflow definitions should keep values serializable and should pass file
inputs by `FileAsset` ID rather than raw paths.
