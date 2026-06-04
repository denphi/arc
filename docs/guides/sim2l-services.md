# Wire up Sim2L services

*Run against real Sim2L catalog/results/cache services instead of the local
no-op backend.*

By default ARC runs fully local: the `LocalRuntimeAdapter` executes artifacts
and the `NoopBackend` publishes nothing. To use Sim2L:

## 1. Choose the sim2l runtime adapter

```bash
export ARC_RUNTIME_ADAPTER=sim2l        # or: service (requires services)
```

This uses sim2l's isolated executor + run database. `service` sets
`ARC_STORAGE_MODE=required` so the service backend is mandatory.

## 2. Point at the services

```bash
export SIM2L_CATALOG_URL=http://localhost:8001
export SIM2L_RESULTS_URL=http://localhost:8002
export SIM2L_CACHE_URL=http://localhost:8003
```

With sim2l importable and the services reachable, `resolve_backend` selects
the `Sim2lBackend`, so register/persist/record publish to the catalog/results
services. See {doc}`../core/backends`.

## 3. Authentication

For authenticated services:

```bash
export SIM2L_USERNAME=…           # the Sim2L service account
export SIM2L_PASSWORD=…
```

The local `start_services.sh` helper runs services with `--no-auth`, so these
are not needed for local development. For the built-in local admin account,
use `SIM2L_USERNAME=admin` and the value from `~/.sim2l/admin_password` (or
`SIM2L_ADMIN_PASSWORD`). Don't use the PostgreSQL DB credentials here.

## 4. (Optional) the sim2l MCP server

```text
/services start mcp                  # from chat
```

Set `ARC_SIM2L_START_MCP=1` to auto-start it; override the transport with
`SIM2L_MCP_TRANSPORT` (default `streamable-http`).

See the consolidated {doc}`../reference/configuration` for all `SIM2L_*`
variables.
