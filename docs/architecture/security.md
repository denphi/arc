# Security & sandboxing

*The threat model and the three mechanisms that contain it: workflow-source
safety checks, subprocess execution, and the API's token + provider-URL
guard.*

ARC generates and runs Python `simulate()` code (often LLM-authored) and
exposes HTTP endpoints that can fetch from provider URLs. Three layers contain
the resulting risks.

## 1. Workflow-source safety (`arc/runtime/workflow_safety.py`)

Before any generated `simulate()` runs, its source is statically checked:

- **Size + node caps.** `MAX_SOURCE_BYTES` (64 KiB) and `MAX_AST_NODES`
  (5 000) bound the input to `ast.parse`, preventing parser-DoS.
- **Import allow-list.** Only a small allow-list is permitted. The builder
  uses `BUILDER_ALLOWED_IMPORTS = {math, cmath, itertools}`; a stricter
  `STRICT_ALLOWED_IMPORTS` exists for other call sites. Anything else is
  rejected.
- **Dunder / attribute-walk rejection.** `Subscript` access to dunder strings
  and names beginning with `__` are rejected, blocking the classic
  `().__class__.__bases__…` / `__globals__` escape walks.
- `check_workflow_source_safe(source)` returns `(ok, reason)`; the builder
  refuses to register an artifact whose code fails the check.

## 2. Subprocess execution (spawn, not fork)

Validated code runs in a **spawned** subprocess
(`multiprocessing.get_context("spawn")`), never `fork` (unsafe on macOS and
being removed; can deadlock under threaded servers). The worker:

- gets a curated `build_safe_globals(...)` (only the allow-listed modules),
- runs under a **wall-clock timeout**,
- has its descendants killed on timeout (`start_new_session=True` +
  `os.killpg`).

This is the path `LocalRuntimeAdapter` uses. The `Sim2LRuntimeAdapter` uses
sim2l's own isolated executor.

```{note}
The subprocess + AST checks are a **safety** boundary against accidental or
careless generated code, not a hardened sandbox against a determined
attacker. Treat ARC's input (goals, provider responses) accordingly, and run
untrusted workloads behind OS-level isolation (containers, seccomp) if your
threat model requires it.
```

## 3. API token + provider-URL guard (`arc/api/security.py`)

The HTTP API and browser UI add three opt-in/always-on guards
(`load_security_config()` reads these once, cached):

1. **Bearer token (optional, opt-in).** Set `ARC_API_TOKEN` to require an
   `Authorization: Bearer <token>` header on data/run endpoints. Health and
   static assets stay open so the page can load and prompt for the token.
2. **`base_url` allow-list (always on for `openwebui`).** `/provider/models`
   and `/research/start` only accept provider base URLs on the allow-list
   (`[api].provider_base_url_allowlist` / `ARC_PROVIDER_ALLOWLIST`,
   comma-separated). The default contains only the upstream OpenWebUI default.
3. **Loopback / private-IP block.** Even with no allow-list configured, base
   URLs resolving to `localhost`, `127.0.0.0/8`, or private ranges are
   rejected (SSRF guard), unless `ARC_ALLOW_PRIVATE_PROVIDER_HOSTS=1`.

## Related configuration

See the consolidated {doc}`../reference/configuration` for `ARC_API_TOKEN`,
`ARC_PROVIDER_ALLOWLIST`, `ARC_ALLOW_PRIVATE_PROVIDER_HOSTS`, and the codex /
claude-code approval knobs (`ARC_CODEX_APPROVAL_POLICY`,
`ARC_CODEX_ALLOW_NON_INTERACTIVE`, `ARC_CLAUDE_CODE_PERMISSION_MODE`).
