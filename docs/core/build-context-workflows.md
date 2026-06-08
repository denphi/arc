# Build-Context Workflows

Build-context workflows let packages prepare structured domain context before
ARC invokes the selected builder. They keep the normal ARC loop intact:

```text
Ideate -> Plan -> Build context -> Build -> Validate -> Execute -> Review
```

The workflow does not replace the builder. It runs before the build step and
returns a `build_context` object that ARC attaches to the active experiment
plan. Any builder strategy can then consume the same context, whether the
project uses the built-in builder, Codex, Claude Code, Gemini, or another
package-provided coder.

## Configure

Use `[strategies]` to choose the builder:

```toml
[strategies]
builder = "codex"
```

Use `[workflows.build]` to choose pre-build context workflows:

```toml
[workflows.build]
context = ["paper-context"]
```

The richer table form can pass explicit inputs:

```toml
[[workflows.build.context]]
name = "paper-context"
required = true
cache = "per_iteration"

[workflows.build.context.inputs]
paper = "paper24.pdf"
benchmark_locator = "section 5.4"
```

File inputs still use the normal workflow input binding and file-asset loader
pipeline. A context workflow can ask for a `type: file` input, and ARC can bind
an attached `file_*` asset or a matching session asset.

You can also select context workflows at runtime:

```bash
arc run "replicate the benchmark" --build-context paper-context
arc chat --build-context paper-context
```

Inside chat:

```text
/build-context
/build-context paper-context
/build-context reset
```

Runtime selections override `arc.toml` for that session only. Builder strategy
selection remains separate; continue to use `[strategies].builder`,
`/strategy builder ...`, or `/coder ...` for the coding backend.

## Contract

A build-context workflow must return a dictionary with `kind: build_context`.
Typical fields are:

```json
{
  "kind": "build_context",
  "workflow": "paper-context",
  "summary": "Short domain summary.",
  "requirements": ["Requirement for the generated artifact."],
  "inputs": {"paper": "file_..."},
  "facts": {"domain_specific_key": "value"},
  "acceptance": {
    "metrics": ["relative_error"],
    "tolerance": {"relative_error": 0.02}
  },
  "artifacts_expected": ["workflow.py", "sim2l.yaml"],
  "provenance": [
    {"source": "file_...", "locator": "page 4"}
  ]
}
```

ARC validates the returned object before invoking the builder. Invalid output
fails clearly instead of being silently folded into the prompt.

## Cache And Retry

ARC caches build-context workflow outputs by workflow name, package source,
explicit inputs, goal, and plan fingerprint. If a retry is classified as an
implementation or runtime problem, ARC can reuse the same context and ask the
selected builder to repair the artifact. If reflection records
`context_failure`, ARC invalidates the context cache and reruns the context
workflow before the next build.

Reviews can also use context-provided acceptance criteria. The default reviewer
understands generic `metrics`, `required_outputs`, `reference_values`, and
`tolerance` fields and records a `failure_classification` such as
`acceptance_failure`, `runtime_failure`, or `implementation_failure`.

## Builder Behavior

The active `ExperimentPlan` includes `build_contexts`. Builder adapters should
read that field and treat the context as authoritative package-provided
pre-build information. Prompt-only builders receive the same information in
their rendered plan JSON or prompt section.

This keeps package workflows and builder strategies independent:

- package workflows prepare domain facts and acceptance criteria,
- builders generate artifacts and code,
- reviewers judge the generated result against the plan and context.

## Package Authoring

Package workflows are registered with `provides.workflows` in `package.yaml`,
the same as any other workflow. A context workflow usually performs extraction,
formulation, or specification steps, then returns one build-context object from
its final step or from an explicit workflow `outputs` mapping.

Example workflow shape:

```yaml
name: paper-context
inputs:
  paper:
    type: file
    required: true
steps:
  - id: extract
    agent: paper_extractor
    input: inputs.paper_text
  - id: build_context
    skill: make-build-context
    input:
      extraction: extract.output
outputs: build_context.output
```
