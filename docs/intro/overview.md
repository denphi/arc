# Overview

*What ARC is, the research loop it runs, and how it relates to Sim2L and MARS.*

ARC (**A**utonomous **R**esearch **C**oder) is a multi-agent framework for
autonomous scientific research. It orchestrates an iterative loop that turns a
free-text research goal into runnable [Sim2L](https://sim2l.readthedocs.io/)
artifacts, executes them, reviews the results, and iterates — inspired by the
MARS (Multi-Agent Research System) architecture.

## The research loop

```text
Ideate → Plan → Build → Validate → Execute → Review → Improve → (repeat)
```

| Step | What happens | Role |
|---|---|---|
| **Ideate** | Turn the user's goal into a structured `ResearchProposal`. | `ideator` (+ `searcher` for catalog/prior-results lookup) |
| **Plan** | Turn the proposal into an `ExperimentPlan` (parameters, sweep, success criteria). | `planner` |
| **Build** | Generate an `ArtifactDraft` (a `workflow.py` + `sim2l.yaml`). | `builder` |
| **Validate** | Check the artifact's schema/imports/AST safety before running it. | runtime adapter (`validate_artifact`) |
| **Execute** | Run the artifact, producing an `ExecutionResult`. | runtime adapter (`run`) |
| **Review** | Decide whether the run satisfies the goal. | `reviewer` (+ `validator` to grade outputs) |
| **Improve** | Extract lessons and seed the next iteration. | `reflector` (+ `optimizer`, `curator`) |

Each named step is a **role**. A role is backed by a **strategy** (a concrete
agent class) chosen by the {doc}`strategy resolver <../core/strategies>`. The
**publish** steps that record artifacts and results to external services
(Register / Persist / Record) are *not* roles — they're
{doc}`backend actions <../architecture/execution-vs-publish>`.

## Stub mode (no LLM required)

Without a configured LLM provider, every agent uses **deterministic stub
logic**. The whole loop still runs end-to-end — it just produces a
reproducible, non-LLM result. This is the default, and it's what the test
suite exercises. See {doc}`../guides/providers` to enable real LLM agents.

## How ARC relates to Sim2L

ARC builds, runs, and reviews **Sim2L artifacts**. Sim2L is the simulation
packaging/execution layer; ARC is the agentic layer that *produces and
iterates on* those artifacts. ARC can run fully local (the built-in
`LocalRuntimeAdapter` + a no-op publish backend) or wire into real Sim2L
catalog/results/cache services — see {doc}`../guides/sim2l-services`.

## What's in this documentation

- **Getting started** — install, run your first loop, learn the vocabulary.
- **Architecture** — the layered design and the two orthogonal axes
  (*where the workflow runs* vs *where results get published*).
- **Contracts** — the typed extension seams (agents, skills, adapters,
  providers, extensions, audit/report hooks).
- **Core** — every core subpackage in detail.
- **Interfaces** — the CLI, HTTP API, browser UI, and chat REPL.
- **Reference** — the consolidated configuration reference and the
  auto-generated Python API reference.
- **Packages** — how to extend ARC, and the catalogue of bundled packages.
