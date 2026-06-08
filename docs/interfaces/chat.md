# Chat REPL

*The interactive research chat (`arc chat`). Type a goal to run the loop, or a
slash command to inspect/steer it.*

```bash
arc chat --stub                          # no LLM
arc chat --provider openwebui --url https://… --token "$OPENWEBUI_KEY"
arc chat --build-context paper-context
```

Chat classifies each input as a **research goal**, a **question**, or a
**command**. A goal runs the loop (up to `--max-iterations`); a question is
answered from context; a command does the thing below.

## Slash commands

| Command | Purpose |
|---|---|
| `/help` | List commands. |
| `/strategy <role> <name…>` | Set a role's strategy (a stack is allowed). See {doc}`../core/strategies`. |
| `/preset [list\|show\|apply\|save\|delete\|clear] <name>` | Strategy presets (`/recipe` alias). See {doc}`../core/recipes-presets`. |
| `/packages` | List loaded packages + session state. |
| `/package enable\|disable <name>` | Enable/disable a package for the session (a real runtime filter). See {doc}`../packages/enable-disable`. |
| `/file add\|list\|show\|load ...` | Attach, inspect, and load session FileAssets. See {doc}`../core/file-assets`. |
| `/build-context [workflow...\|reset]` | Show or set pre-build context workflows. See {doc}`../core/build-context-workflows`. |
| `/coder [codex\|claude\|builder]` | Select the coding backend. |
| `/sweep [artifact]` | Run the planner's parameter sweep (shows planner provenance). |
| `/optimize [gens] [pop]` | Run the active optimizer (shows optimizer + planner provenance). |
| `/iterate [N]` | Rerun the full loop N times. |
| `/services [start\|stop\|status] [mcp]` | Manage sim2l services. |
| `/target <k=v…>` | Set/clear the run target. |
| `/exec <artifact>` | Execute an artifact directly. |
| `/artifacts` / `/results` / `/sessions` / `/clusters` / `/skills` | Inspect state. |
| `/continue` / `/run` | Continue / run an iteration. |
| `/clear` / `/quit` | Clear screen / exit. |

## Provenance in `/sweep` and `/optimize`

`/sweep` prints `Sweep source: planner=<key>, design=<…>, artifact=<name>`;
`/optimize` prints the active optimizer plus the planner the search space came
from. See {doc}`../core/strategies`.

## Startup files

Chat scans `./data` at session start, or `ARC_INPUTS_DIR` when that variable is
set, and prints a concise table of available files. The scan registers metadata
only; PDF text extraction, image metadata, CSV previews, and other derived
assets are created lazily with `/file load` or workflow `type: file` inputs.

## Key bindings

Up/Down for history, `Ctrl+A`/`Ctrl+E` line start/end, `Ctrl+K` delete to end,
`Ctrl+U` delete before cursor. History persists in `~/.../.arc_chat_history`.
