# CLI

*The `arc` command-line interface. Installed by `pip install arc` (needs the
`typer` extra for the full CLI; `pip install 'arc[all]'`).*

```bash
arc --help
```

## Commands

| Command | What it does |
|---|---|
| `arc run "<goal>"` | Run a research loop for a goal (stub mode without a provider). |
| `arc chat` | Start the interactive research chat REPL. |
| `arc serve` | Start the HTTP API server. |
| `arc ui` | Start the standalone browser UI. |
| `arc models <provider>` | List a provider's available models. |
| `arc info` | Show registered components + each package's declared config. |
| `arc file add/list/show/load` | Attach, inspect, and load session FileAssets. |
| `arc package init <name> <dir>` | Scaffold a new local package. |
| `arc package validate <dir>` | Validate a package manifest + that its declarations register. |

## `arc run`

```bash
arc run "Maximise the output metric" \
  --domain materials \
  --input benchmark_locator="section 5.4" \
  --iterations 3 \
  --provider openwebui --model gpt-oss:120b --base-url https://… \
  --workflow research-loop \
  --output run.json
```

| Option | Default | Meaning |
|---|---|---|
| `--domain, -d` | — | Research domain. |
| `--iterations, -n` | `1` | Max iterations (stops early if a run is approved). |
| `--provider, -p` / `--token, -t` / `--model, -m` / `--base-url, -u` | env | Provider overrides. |
| `--workflow, -w` | `research-loop` | Registered workflow name. |
| `--input, -i` | — | Workflow input as `key=value`; repeat as needed. |
| `--output, -o` | stdout | Save results JSON to a file. |

## `arc chat`

Key options: `--stub` (no LLM), `--provider/--token/--model/--url`,
`--session <id>` (resume), `--list-sessions`, `--delete-session`,
`--delete-all-sessions`, `--max-iterations`, `--check` (dry-run config/auth
report), `--plan` (no files written, no sim2l pushes), `--events
ansi|jsonl|stdout-json|multi`. See {doc}`chat`.

## `arc serve` / `arc ui`

```bash
arc serve --host 0.0.0.0 --port 8000      # → /docs
arc ui    --host 127.0.0.1 --port 8888
```

See {doc}`api` and {doc}`ui`.

## `arc package`

```bash
arc package init my-lab ./arc-my-lab      # scaffold
arc package validate ./arc-my-lab         # exits non-zero if a declaration didn't register
```

See {doc}`../packages/local-packages`.

## `arc file`

```bash
arc file add paper24.pdf --role paper --session my-session
arc file list --session my-session
arc file show file_abc123 --session my-session
arc file load file_abc123 --loader pdf_loader --session my-session
```

For automatic startup discovery, put files in `./data` or set
`ARC_INPUTS_DIR` before `arc chat`. See {doc}`../core/file-assets`.
