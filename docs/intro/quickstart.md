# Quickstart

*Run a loop from the CLI, the API, the browser UI, and chat — stub mode first.*

Everything below works **without an LLM** (stub mode). To switch on real LLM
agents, set a provider (see {doc}`../guides/providers`); the commands are
otherwise identical.

## 1. Run one iteration (CLI)

```bash
arc run "Verify that a parameter doubles to produce a result"
```

ARC ideates a proposal, plans, builds a Sim2L artifact, validates it, executes
it, and reviews the result — printing the final result JSON. Add `-n 3` to run
up to three iterations (it stops early if a run is approved):

```bash
arc run "Maximise the output metric" -n 3 -o run.json
```

See {doc}`../interfaces/cli` for every flag.

## 2. Start the HTTP API

```bash
arc serve                      # → http://localhost:8000/docs
```

Then drive a loop over HTTP (every request carries a session id):

```bash
curl -X POST "http://localhost:8000/research/start?session_id=demo" \
  -H "Content-Type: application/json" \
  -d '{"goal": "Verify parameter doubling"}'
```

See {doc}`../interfaces/api` for every route, and {doc}`../guides/use-the-api`
for a worked example.

## 3. Start the browser UI

```bash
arc ui                         # → http://127.0.0.1:8888
```

A chat-style thread, a sessions drawer, an artifact/result inspector, and live
run progress over Server-Sent Events. See {doc}`../interfaces/ui`.

## 4. Interactive chat

```bash
arc chat --stub                # no LLM
# or with a provider:
arc chat --provider openwebui --url https://… --token "$OPENWEBUI_KEY"
```

Inside chat you can type a research goal, or use slash commands:

```text
/strategy planner doe_lhs        # pick a strategy
/preset list                     # list strategy presets
/sweep                           # run the planner's parameter sweep
/optimize                        # run the active optimizer
/package disable arc-mars        # disable a package for this session
```

See {doc}`../interfaces/chat` for the full command set.

## Where things are stored

ARC writes per-session files under `~/.sim2l/code/<session_id>/` (artifacts,
runs, a `provenance.jsonl`, and `session_state.json`). See
{doc}`../core/sessions`.

## Next steps

- Learn the vocabulary: {doc}`concepts`.
- Understand the design: {doc}`../architecture/overview`.
- Pick strategies and recipes: {doc}`../guides/choose-a-strategy`.
