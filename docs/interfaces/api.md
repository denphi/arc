# HTTP API

*A FastAPI app (`arc serve`) exposing the research loop, artifacts, execution,
results, review, file assets, strategies/presets, and skills. Interactive docs
at `/docs`.*

Every stateful request carries a session via `?session_id=<id>` (or the
`X-Session-ID` header on some surfaces). Optional bearer-token auth is enabled
by setting `ARC_API_TOKEN` (see {doc}`../architecture/security`).

```{warning}
ARC sessions are not user-owned or access-controlled. With no
`ARC_API_TOKEN`, the API is a default-open development surface. With a shared
token, that token grants visibility across all sessions on disk; endpoints such
as `GET /artifact` and `GET /results` without `session_id` enumerate every
session under `SIM2L_HOME`.
```

## Research loop, artifacts, execution

| Method + path | Purpose |
|---|---|
| `POST /research/start` | Run one research-loop iteration. |
| `POST /artifact/create` | Register a new artifact. |
| `GET /artifact` / `GET /artifact/{id}` | List / fetch artifacts. |
| `POST /execution/run` | Execute an artifact. |
| `GET /execution/status/{run_id}` | Run status. |
| `GET /results` / `GET /results/{run_id}` | List / fetch results. |
| `POST /review/run` | Review an execution result. |
| `POST /files` | Attach a trusted local file path to a session. |
| `GET /files` / `GET /files/{id}` | List / fetch FileAsset metadata. |
| `POST /files/{id}/load` | Run an enabled loader and register derived assets. |
| `GET /files/{id}/derived` | List derived assets for a source file. |
| `GET /health` | Health check (always open). |
| `POST /provider/models` | List provider models (base-URL allow-listed). |

## Strategies, presets, clusters, skills

| Method + path | Purpose |
|---|---|
| `GET /strategies` / `GET /strategies/{role}` | List strategies / a role's options. |
| `POST /strategies/{role}` | Set a role's strategy for the session. |
| `GET /presets` / `GET /presets/{name}` | List / show presets. |
| `POST /presets/{name}/apply` / `POST /presets` / `DELETE /presets/{name}` / `POST /presets/clear` | Apply / save / delete / clear. |
| `/recipes…` | Backward-compatible aliases of `/presets…`. |
| `GET /clusters` / `GET /clusters/{signature}` | Failure clusters. |
| `GET /skills` / `GET /skills/{name}` / `POST /skills/import` / `GET /skills/export` | Learned-skill transfer. |

## File path imports

`POST /files` imports a path on the ARC server. By default those paths must be
under `ARC_INPUTS_DIR` (`./data` unless configured) or one of the directories
listed in `ARC_FILES_ALLOWED_ROOTS`. Set `ARC_FILES_TRUSTED_LOCAL=1` only when
the API is bound to a private local surface and callers are allowed to import
arbitrary paths readable by the ARC process.

## Example

```bash
curl -X POST "http://localhost:8000/research/start?session_id=demo" \
  -H "Content-Type: application/json" \
  -d '{"goal": "Verify parameter doubling", "target": {"result": 2.0}}'
```

See {doc}`../guides/use-the-api` for a worked multi-step example, and the
auto-generated {doc}`../reference/api/index` for the route handlers.
