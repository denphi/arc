# Browser UI

*A standalone FastAPI app (`arc ui`) over the same core primitives as the CLI
and API. It does **not** import the terminal chat loop.*

```bash
arc ui                                   # → http://127.0.0.1:8888
python -m arc.ui --host 0.0.0.0 --port 8888
```

Binds `127.0.0.1:8888` by default. Open the explicit IPv4 address
(`localhost` may resolve to IPv6). Host/port also read from `ARC_UI_HOST` /
`ARC_UI_PORT`.

## What it offers

- A chat-style thread, a sessions drawer.
- An artifact/result inspector with a file viewer and a schema-derived
  execution form.
- Live run progress over **Server-Sent Events** — including
  {doc}`audit <../core/audit>` events when a package registers audit actions.

## Security

When exposed beyond localhost, set `ARC_API_TOKEN` to require a bearer token.
The data/run endpoints are then gated; `/`, `/assets/*`, and `/api/health`
stay open so the page can load and prompt for the token. See
{doc}`../architecture/security`.

## Session history

The UI uses three distinct records (run history, the UI thread transcript, and
CLI line history) — see {doc}`../core/sessions`. For a session with no UI
thread it derives a read-only timeline from `run_history`.
