# Run a research loop

*End-to-end: a stub-mode loop first (no setup), then the same loop with an LLM.*

## Stub mode (no provider)

```bash
arc run "Verify that a parameter doubles to produce a result"
```

ARC ideates a proposal, plans, builds a Sim2L artifact, validates and executes
it, then reviews — all with deterministic stub agents. The final result JSON
prints to stdout. Add `-n 3` to iterate up to three times (it stops early if a
run is approved), and `-o run.json` to save.

What happened, step by step:

1. **Ideate** — the `ideator` produced a `ResearchProposal` (in stub mode it
   ranks a few deterministic candidates; see {doc}`../core/strategies`).
2. **Plan** — the `planner` produced an `ExperimentPlan`.
3. **Build** — the `builder` generated a `workflow.py` + `sim2l.yaml`.
4. **Validate / Execute** — the {doc}`runtime adapter <../core/runtime-adapters>`
   checked the artifact (AST safety) and ran it in a subprocess.
5. **Review / Improve** — the `reviewer` judged the run; the `reflector`
   recorded lessons.

Artifacts and logs land under `~/.sim2l/code/<session_id>/` — see
{doc}`../core/sessions`.

## With an LLM

Set a provider (see {doc}`providers`) and run the same command:

```bash
export ARC_PROVIDER=openwebui
export OPENWEBUI_URL=https://genai.rcac.purdue.edu/api
export OPENWEBUI_KEY=…
arc run "Design a structure whose band gap is close to 1.1 eV" -n 5
```

Now the agents call the model; the loop is otherwise identical.

## Interactively

```bash
arc chat
```

Type a goal and watch the phases; steer with `/strategy`, `/preset`, `/sweep`,
`/optimize`. See {doc}`../interfaces/chat`.
