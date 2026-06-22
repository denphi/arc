"""Backend implementations + selection.

Three backends ship today:

  * :class:`NoopBackend` — the default. Every action is a silent no-op
    and ``is_active()`` is False. ARC runs fully local: it ideates,
    plans, builds, validates, *executes locally*, reviews, and
    improves, with no shared catalog/results persistence and no
    warnings. sim2l is opt-in.

  * :class:`Sim2lBackend` — wraps a ``Sim2LRuntimeAdapter`` and routes
    the publish actions to the sim2l catalog/results services. Active
    when the sim2l package is importable.

  * :class:`GitHubBackend` — publishes artifacts (and, optionally, run
    records) to a GitHub repository via the Contents API. Gives
    versioning, PR review, and public shareability for free, with no
    sim2l dependency. Active when a ``GITHUB_TOKEN`` + repo are
    configured.

:func:`resolve_backend` picks one. Selection precedence: an explicit
``ARC_BACKEND`` env var (or ``arc.toml [backend] kind``) wins; otherwise
it infers sim2l-when-active, else the no-op.

Why a backend rather than more adapter methods?
-----------------------------------------------

Execution ("where the workflow runs") and publishing ("where its
artifacts + results go") are orthogonal. A user can run locally but
publish to sim2l, or run on sim2l but not publish, etc. Keeping the
backend separate from the runtime adapter lets those vary
independently.
"""

from __future__ import annotations

import asyncio
import logging
import inspect
from typing import Any

from arc.contracts.backend import BackendActions
from arc.schemas.artifact import ArtifactRecord
from arc.schemas.execution import ExecutionResult

logger = logging.getLogger(__name__)


_ACTION_RESULT_KEYS = {
    "register_artifact": "registered",
    "persist_result": "persisted",
    "record_execution": "recorded",
    "publish_provenance": "published",
}


async def safe_backend_action(backend: Any, action: str, *args: Any) -> dict[str, Any]:
    """Call a backend publish action without letting it abort the loop.

    BackendActions implementations are supposed to be best-effort and never
    raise. This wrapper enforces that contract at the call boundary too, so a
    misconfigured built-in backend or a third-party backend cannot turn a
    successful execution into a failed workflow.
    """
    result_key = _ACTION_RESULT_KEYS.get(action, "ok")
    backend_name = getattr(backend, "name", None) or type(backend).__name__
    if backend is None:
        return {result_key: False, "skipped": True, "backend": "none"}

    method = getattr(backend, action, None)
    if method is None:
        return {
            result_key: False,
            "backend": backend_name,
            "error": f"backend has no {action}",
        }

    try:
        result = method(*args)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            return result
        return {result_key: bool(result), "backend": backend_name}
    except Exception as exc:  # noqa: BLE001 — publishing is advisory
        logger.debug("backend %s.%s failed: %s", backend_name, action, exc)
        return {result_key: False, "backend": backend_name, "error": str(exc)}


# ── Detection (layered) ────────────────────────────────────────────────


def sim2l_importable() -> bool:
    """True when the ``sim2l`` Python package can be imported.

    Gates the executor + adapter choice and the ``Sim2lBackend``.
    """
    try:
        import sim2l  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — any import failure means "not available"
        return False


# (monotonic_timestamp, reachable) — see sim2l_services_active.
_services_probe_cache: tuple[float, bool] | None = None
_SERVICES_PROBE_TTL_SECONDS = 5.0


def sim2l_services_active(timeout: float = 1.5) -> bool:
    """Best-effort check that the sim2l catalog service responds.

    Gates the *publish* actions specifically. We probe the catalog
    health endpoint; failure (or no service) means "don't publish".
    The answer is cached for a few seconds (mirroring the adapter's
    ``_svc_probe``): the standalone backend's ``is_active()`` is called
    per registration / UI refresh, and each uncached probe against a
    down service stalls up to ``timeout`` seconds.
    """
    import os
    import time

    global _services_probe_cache
    now = time.monotonic()
    if (_services_probe_cache is not None
            and now - _services_probe_cache[0] < _SERVICES_PROBE_TTL_SECONDS):
        return _services_probe_cache[1]

    catalog_url = os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")
    try:
        import requests
        resp = requests.get(f"{catalog_url.rstrip('/')}/health", timeout=timeout)
        reachable = resp.status_code == 200
    except Exception:  # noqa: BLE001
        reachable = False
    _services_probe_cache = (now, reachable)
    return reachable


# ── No-op backend (default) ─────────────────────────────────────────────


class NoopBackend(BackendActions):
    """Silent default. Every action is a no-op; nothing is published.

    This is what makes "ARC runs fully local, sim2l is opt-in" true:
    the loop calls ``backend.register_artifact(...)`` etc.
    unconditionally, and with this backend those calls quietly do
    nothing instead of reaching for a service that isn't there.
    """

    name = "noop"

    def is_active(self) -> bool:
        return False

    async def register_artifact(self, artifact: ArtifactRecord) -> dict[str, Any]:
        return {"registered": False, "skipped": True, "backend": "noop"}

    async def persist_result(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return {"persisted": False, "skipped": True, "backend": "noop"}

    async def record_execution(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        return {"recorded": False, "skipped": True, "backend": "noop"}


# ── sim2l backend ───────────────────────────────────────────────────────


class Sim2lBackend(BackendActions):
    """Routes publish actions to sim2l's catalog/results services.

    Two modes:

    * **Adapter-wrapped** (``adapter`` is a ``Sim2LRuntimeAdapter``):
      ``register_artifact`` delegates to the adapter. Persist/record are
      handled *inline* by the adapter's ``run()``; here we report the
      *actual* inline outcome (read from ``execution.metrics``) instead
      of claiming unconditional success.

    * **Standalone** (``adapter is None``): execution happened elsewhere
      (e.g. the local adapter) and this backend publishes directly to
      the sim2l catalog/results services over HTTP. This is what makes
      "run locally, publish to sim2l" real. No sim2l import is needed —
      only reachable services.
    """

    name = "sim2l"

    def __init__(
        self,
        adapter: Any = None,
        *,
        catalog_session_id: str | None = None,
        results_session_id: str | None = None,
    ):
        # ``adapter`` is a Sim2LRuntimeAdapter (or None for standalone).
        # Held loosely (Any) so this module doesn't hard-import the sim2l
        # adapter at module scope — keeping the import cost off the no-op
        # path. Session ids are resolved *lazily* (see the properties
        # below): the backend is constructed before the chat signs in to
        # the services, so capturing ids here would freeze them at None.
        self._adapter = adapter
        self._catalog_session_override = catalog_session_id
        self._results_session_override = results_session_id

    def set_session_ids(
        self,
        *,
        catalog_session_id: str | None = None,
        results_session_id: str | None = None,
    ) -> None:
        """Attach service session ids after a later login.

        The chat signs in to the services *after* the workflow (and its
        backend) is constructed; it calls this so standalone-mode pushes
        and ``publish_provenance`` carry authenticated headers.
        """
        if catalog_session_id is not None:
            self._catalog_session_override = catalog_session_id
        if results_session_id is not None:
            self._results_session_override = results_session_id

    @property
    def _catalog_session_id(self) -> str | None:
        # Explicit override > adapter's current id (set by chat login,
        # possibly after this backend was built) > environment.
        import os
        return (
            self._catalog_session_override
            or getattr(self._adapter, "_catalog_session_id", None)
            or os.environ.get("SIM2L_CATALOG_SESSION_ID")
        )

    @property
    def _results_session_id(self) -> str | None:
        import os
        return (
            self._results_session_override
            or getattr(self._adapter, "_results_session_id", None)
            or os.environ.get("SIM2L_RESULTS_SESSION_ID")
        )

    @property
    def _catalog_url(self) -> str:
        import os
        return os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")

    @property
    def _results_url(self) -> str:
        import os
        return os.environ.get("SIM2L_RESULTS_URL", "http://localhost:8003")

    def is_active(self) -> bool:
        if self._adapter is not None:
            # The package must be importable for the adapter to do anything.
            # Service reachability gates *persistence* but the adapter
            # already degrades gracefully when services are down.
            return sim2l_importable()
        # Standalone mode publishes over HTTP — the services are the gate.
        return sim2l_services_active()

    # --- shared helpers ---

    @staticmethod
    def _headers(session_id: str | None) -> dict[str, str]:
        return {"X-Session-ID": session_id} if session_id else {}

    def _catalog_simulation_identity(self, artifact: ArtifactRecord) -> tuple[str, str]:
        from arc.runtime.sim2l_adapter import _sim_name_for_artifact
        return _sim_name_for_artifact(artifact.name), artifact.version

    # --- actions ---

    async def register_artifact(self, artifact: ArtifactRecord) -> dict[str, Any]:
        registrar = getattr(self._adapter, "register_artifact", None)
        if registrar is not None:
            try:
                result = registrar(artifact)
                if hasattr(result, "__await__"):
                    result = await result
                return result if isinstance(result, dict) else {"registered": bool(result)}
            except Exception as exc:  # noqa: BLE001 — publishing is best-effort
                logger.debug("sim2l register_artifact failed: %s", exc)
                return {"registered": False, "error": str(exc)}
        try:
            return await asyncio.to_thread(self._register_artifact_standalone, artifact)
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("sim2l standalone register_artifact failed: %s", exc)
            return {"registered": False, "backend": "sim2l", "error": str(exc)}

    def _register_artifact_standalone(self, artifact: ArtifactRecord) -> dict[str, Any]:
        import hashlib
        from pathlib import Path

        import requests

        from arc.runtime.sim2l_adapter import _function_workflow_bundle
        from arc.sim2l_schema import load_sim2l_schema

        art_dir = Path(artifact.path)
        wf_path = art_dir / "workflow.py"
        if not wf_path.exists():
            return {"registered": False, "backend": "sim2l",
                    "error": f"workflow.py not found at {wf_path}"}
        source = wf_path.read_text()
        workflow_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        in_schema, out_schema = load_sim2l_schema(art_dir)
        sim_name, sim_version = self._catalog_simulation_identity(artifact)

        payload = {
            "name": sim_name,
            "version": sim_version,
            "description": (
                getattr(artifact, "description", "")
                or (artifact.metadata or {}).get("description")
                or artifact.name
            ),
            "workflow_type": "function",
            "workflow_hash": workflow_hash,
            "workflow_bundle": _function_workflow_bundle(
                source, workflow_hash=workflow_hash, artifact_dir=art_dir,
            ),
            "input_schema": in_schema,
            "output_schema": out_schema,
            "auto_approve": True,
            "metadata": {
                "workflow_source": source,
                "source": "arc.backend",
                **(
                    {"capability": (artifact.metadata or {}).get("capability")}
                    if isinstance((artifact.metadata or {}).get("capability"), dict)
                    else {}
                ),
            },
        }
        resp = requests.post(
            f"{self._catalog_url}/simulations",
            json=payload,
            headers=self._headers(self._catalog_session_id),
            timeout=15,
        )
        if resp.status_code in (200, 201, 202):
            return {"registered": True, "backend": "sim2l",
                    "sim_name": sim_name, "sim_version": sim_version,
                    "catalog_persisted": True}
        if resp.status_code == 409:
            return {"registered": True, "backend": "sim2l",
                    "sim_name": sim_name, "sim_version": sim_version,
                    "catalog_persisted": True, "already_registered": True}
        return {"registered": False, "backend": "sim2l",
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    def _inline_outcome(
        self, execution: ExecutionResult, metric_key: str, result_key: str,
    ) -> dict[str, Any] | None:
        """The adapter-reported outcome of an inline push, or None.

        ``Sim2LRuntimeAdapter.run()`` records what its inline pushes
        actually did in ``execution.metrics`` — report *that* rather than
        claiming success unconditionally.
        """
        metrics = getattr(execution, "metrics", None) or {}
        if metric_key not in metrics:
            return None
        ok = bool(metrics[metric_key])
        outcome: dict[str, Any] = {
            result_key: ok, "handled_inline": True, "backend": "sim2l",
        }
        if ok and metrics.get("results_persistence_assumed"):
            # Cache hit: persistence is inferred from the source execution,
            # not verified by a push in this run.
            outcome["assumed"] = True
        if not ok:
            errors = getattr(self._adapter, "last_push_errors", None) or []
            if errors:
                outcome["error"] = "; ".join(msg for _label, msg in errors[-3:])
            else:
                outcome["error"] = "inline push failed (see adapter logs)"
        return outcome

    async def persist_result(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        if self._adapter is not None:
            # Inline by Sim2LRuntimeAdapter.run() (_push_to_results); don't
            # re-push — report what actually happened.
            inline = self._inline_outcome(execution, "results_persisted", "persisted")
            if inline is not None:
                return inline
        try:
            return await asyncio.to_thread(
                self._persist_result_standalone, artifact, execution, inputs,
            )
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("sim2l standalone persist_result failed: %s", exc)
            return {"persisted": False, "backend": "sim2l", "error": str(exc)}

    def _persist_result_standalone(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        import requests

        sim_name, sim_version = self._catalog_simulation_identity(artifact)
        metrics = getattr(execution, "metrics", None) or {}
        payload = {
            "execution_id": execution.run_id,
            "simulation_name": sim_name,
            "simulation_version": sim_version,
            "squid_id": metrics.get("squid_id") or "",
            "input_params": inputs,
            "output_params": {
                k: v for k, v in (execution.outputs or {}).items() if v is not None
            },
            "status": execution.status,
            "duration_seconds": metrics.get("duration_seconds"),
        }
        resp = requests.post(
            f"{self._results_url}/register_direct",
            json=payload,
            headers=self._headers(self._results_session_id),
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return {"persisted": True, "backend": "sim2l"}
        return {"persisted": False, "backend": "sim2l",
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    async def record_execution(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        if self._adapter is not None:
            # Inline via _push_to_catalog_execution_registry.
            inline = self._inline_outcome(execution, "execution_recorded", "recorded")
            if inline is not None:
                return inline
        try:
            return await asyncio.to_thread(
                self._record_execution_standalone, artifact, execution, inputs, outputs,
            )
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("sim2l standalone record_execution failed: %s", exc)
            return {"recorded": False, "backend": "sim2l", "error": str(exc)}

    def _record_execution_standalone(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        import hashlib
        import json
        from datetime import datetime, timezone

        import requests

        sim_name, sim_version = self._catalog_simulation_identity(artifact)
        headers = self._headers(self._catalog_session_id)
        lookup = requests.get(
            f"{self._catalog_url}/simulations/{sim_name}",
            params={"version": sim_version},
            headers=headers,
            timeout=5,
        )
        if lookup.status_code == 404:
            return {"recorded": False, "backend": "sim2l",
                    "error": "simulation not in catalog (register the artifact first)"}
        lookup.raise_for_status()
        sim_id = (lookup.json() or {}).get("id")
        if not sim_id:
            return {"recorded": False, "backend": "sim2l",
                    "error": "catalog lookup returned no simulation id"}

        metrics = getattr(execution, "metrics", None) or {}
        # The backend is called right after the run; reconstruct the start
        # from the reported duration so the recorded window isn't zero-width.
        from datetime import timedelta
        completed_dt = datetime.now(timezone.utc)
        duration = metrics.get("duration_seconds")
        started_dt = (
            completed_dt - timedelta(seconds=float(duration))
            if isinstance(duration, (int, float)) else completed_dt
        )
        payload = {
            "execution_id": execution.run_id,
            "squid_id": metrics.get("squid_id") or execution.run_id,
            "simulation_id": sim_id,
            "user_id": None,
            "started_at": started_dt.replace(tzinfo=None).isoformat(),
            "completed_at": completed_dt.replace(tzinfo=None).isoformat(),
            "duration_seconds": metrics.get("duration_seconds"),
            "status": execution.status,
            "executor_type": "external",
            "cache_hit": bool(metrics.get("cache_hit", False)),
            "input_hash": hashlib.sha256(
                json.dumps(inputs, sort_keys=True, default=str).encode("utf-8"),
            ).hexdigest(),
            "output_count": len(outputs or {}),
            "error_count": 1 if execution.status not in ("completed", "success") else 0,
            "environment": {"source": "arc.backend"},
        }
        resp = requests.post(
            f"{self._catalog_url}/executions",
            json=payload, headers=headers, timeout=10,
        )
        if resp.status_code in (200, 201):
            return {"recorded": True, "backend": "sim2l"}
        return {"recorded": False, "backend": "sim2l",
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    async def publish_provenance(
        self,
        session_id: str,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Push provenance entries to the results service ``/provenance``.

        Makes the agent-action audit trail outlive the client machine —
        without this, only the *results* of agent decisions are published
        while the decisions themselves stay in a session-local JSONL.
        """
        if not entries:
            return {"published": True, "count": 0, "backend": "sim2l"}
        try:
            return await asyncio.to_thread(
                self._publish_provenance_blocking, session_id, list(entries),
            )
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("sim2l publish_provenance failed: %s", exc)
            return {"published": False, "backend": "sim2l", "error": str(exc)}

    def _publish_provenance_blocking(
        self, session_id: str, entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        import requests

        resp = requests.post(
            f"{self._results_url}/provenance",
            json={"session_id": session_id, "entries": entries},
            headers=self._headers(self._results_session_id),
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return {"published": True, "count": len(entries), "backend": "sim2l"}
        if resp.status_code == 404:
            # Older results service without the /provenance endpoint —
            # treat as "doesn't publish provenance", not a retryable error.
            return {"published": False, "skipped": True, "backend": "sim2l",
                    "error": "results service has no /provenance endpoint"}
        return {"published": False, "backend": "sim2l",
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}


# ── GitHub backend ───────────────────────────────────────────────────────


def github_config() -> dict[str, str] | None:
    """Read GitHub backend config from the environment.

    Returns ``{"token", "repo", "branch", "prefix"}`` when a token + repo
    are present, else ``None``. ``repo`` is ``owner/name``; ``branch``
    defaults to empty (the repo default branch); ``prefix`` is the path
    under which artifacts are committed (default ``artifacts``).
    """
    import os
    import re
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("ARC_GITHUB_REPO")
    # ``repo`` is interpolated into the API URL after ``/repos/``; require a
    # strict ``owner/name`` shape so a malformed value can't smuggle extra
    # path segments or query/host tricks into the request URL.
    if not token or not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return None
    return {
        "token": token,
        "repo": repo,
        "branch": os.environ.get("ARC_GITHUB_BRANCH", ""),
        "prefix": os.environ.get("ARC_GITHUB_PREFIX", "artifacts").strip("/"),
    }


class GitHubBackend(BackendActions):
    """Publishes artifacts to a GitHub repo via the Contents API.

    ``register_artifact`` commits every file in the artifact directory
    (``workflow.py`` + ``sim2l.yaml`` + ``arc_record.json`` + any tests)
    under ``<prefix>/<name>/<version>/`` on the configured repo/branch.
    ``persist_result`` / ``record_execution`` append a JSON run record
    under ``<prefix>/<name>/<version>/runs/``.

    Uses ``requests`` against the REST API — no local clone, no extra
    pip dependency, works from any environment with a token. Every
    action is best-effort and never raises (per the contract).
    """

    name = "github"
    _API = "https://api.github.com"

    def __init__(self, config: dict[str, str] | None = None):
        self._config = config or github_config()

    def is_active(self) -> bool:
        return self._config is not None

    # --- internals ---

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config['token']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _put_file(self, repo_path: str, content: str | bytes, message: str) -> dict[str, Any]:
        """Create-or-update one file via PUT /repos/{repo}/contents/{path}.

        ``content`` may be text or raw bytes (artifact files can be
        binary). Returns ``{"ok": bool, ...}``; never raises.
        """
        import base64
        import requests

        raw = content.encode() if isinstance(content, str) else content
        url = f"{self._API}/repos/{self._config['repo']}/contents/{repo_path}"
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(raw).decode(),
        }
        if self._config.get("branch"):
            payload["branch"] = self._config["branch"]
        try:
            # If the file already exists we must pass its blob sha to update it.
            get_params = {"ref": self._config["branch"]} if self._config.get("branch") else {}
            existing = requests.get(url, headers=self._headers(), params=get_params, timeout=10)
            if existing.status_code == 200:
                payload["sha"] = existing.json().get("sha")
            resp = requests.put(url, headers=self._headers(), json=payload, timeout=15)
            if resp.status_code in (200, 201):
                return {"ok": True, "path": repo_path}
            return {"ok": False, "path": repo_path,
                    "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("github PUT %s failed: %s", repo_path, exc)
            return {"ok": False, "path": repo_path, "error": str(exc)}

    def _artifact_base(self, artifact: ArtifactRecord) -> str:
        name = _safe_segment(artifact.name)
        version = _safe_segment(artifact.version)
        return f"{self._config['prefix']}/{name}/{version}"

    # --- actions ---

    async def register_artifact(self, artifact: ArtifactRecord) -> dict[str, Any]:
        if not self.is_active():
            return {
                "registered": False,
                "skipped": True,
                "backend": "github",
                "error": "GitHub backend is not configured",
            }
        # Per the BackendActions contract this must never raise — wrap the
        # whole body so a filesystem race / permission error / unreadable
        # file becomes an error result, not an exception in the loop.
        # ``_commit_artifact_dir`` does blocking file reads + HTTP, so run
        # it off the event loop.
        try:
            return await asyncio.to_thread(self._commit_artifact_dir, artifact)
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("github register_artifact failed: %s", exc)
            return {"registered": False, "backend": "github", "error": str(exc)}

    def _commit_artifact_dir(self, artifact: ArtifactRecord) -> dict[str, Any]:
        from pathlib import Path

        art_dir = Path(artifact.path)
        if not art_dir.exists():
            return {"registered": False, "error": f"artifact path missing: {art_dir}"}

        base = self._artifact_base(artifact)
        committed: list[str] = []
        errors: list[str] = []
        for f in sorted(art_dir.rglob("*")):
            if not f.is_file():
                continue
            if f.is_symlink():
                errors.append(f"{f.relative_to(art_dir).as_posix()}: symlinks are not published")
                continue
            # Read as bytes so binary artifact files survive (base64 in
            # _put_file handles both); a single unreadable file is recorded
            # as an error rather than aborting the whole publish.
            rel_path = f.relative_to(art_dir).as_posix()
            try:
                content: bytes = f.read_bytes()
            except OSError as exc:
                errors.append(f"{rel_path}: {exc}")
                continue
            result = self._put_file(
                f"{base}/{rel_path}",
                content,
                f"arc: publish {artifact.name} {artifact.version} ({rel_path})",
            )
            if result["ok"]:
                committed.append(rel_path)
            else:
                errors.append(result.get("error", "unknown"))

        return {
            "registered": bool(committed) and not errors,
            "backend": "github",
            "repo": self._config["repo"],
            "path": base,
            "files": committed,
            **({"error": "; ".join(errors)} if errors else {}),
        }

    async def persist_result(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        import json

        if not self.is_active():
            return {
                "persisted": False,
                "skipped": True,
                "backend": "github",
                "error": "GitHub backend is not configured",
            }
        try:
            base = self._artifact_base(artifact)
            record = {
                "run_id": execution.run_id,
                "status": execution.status,
                "inputs": inputs,
                "outputs": execution.outputs,
                "metrics": execution.metrics,
            }
            result = await asyncio.to_thread(
                self._put_file,
                f"{base}/runs/{_safe_segment(execution.run_id)}.json",
                json.dumps(record, indent=2, default=str),
                f"arc: result {execution.run_id} for {artifact.name}",
            )
            return {"persisted": result["ok"], "backend": "github",
                    **({"error": result["error"]} if not result["ok"] else {})}
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("github persist_result failed: %s", exc)
            return {"persisted": False, "backend": "github", "error": str(exc)}

    async def record_execution(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.is_active():
            return {
                "recorded": False,
                "skipped": True,
                "backend": "github",
                "error": "GitHub backend is not configured",
            }
        # The run record written by persist_result already captures the
        # execution; recording is a no-op here to avoid a duplicate commit.
        return {"recorded": True, "handled_inline": True, "backend": "github"}

    async def publish_provenance(
        self,
        session_id: str,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Commit a provenance batch as JSONL under ``<prefix>/provenance/``.

        Each batch gets its own timestamped file so publishing is append-only
        (no read-modify-write of a growing file via the Contents API).
        """
        import json

        if not self.is_active():
            return {"published": False, "skipped": True, "backend": "github",
                    "error": "GitHub backend is not configured"}
        if not entries:
            return {"published": True, "count": 0, "backend": "github"}
        try:
            from datetime import datetime, timezone
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = (f"{self._config['prefix']}/provenance/"
                    f"{_safe_segment(session_id)}/{stamp}.jsonl")
            content = "\n".join(json.dumps(e, default=str) for e in entries) + "\n"
            result = await asyncio.to_thread(
                self._put_file, path, content,
                f"arc: provenance batch for {session_id} ({len(entries)} entries)",
            )
            return {"published": result["ok"], "count": len(entries), "backend": "github",
                    **({"error": result["error"]} if not result["ok"] else {})}
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("github publish_provenance failed: %s", exc)
            return {"published": False, "backend": "github", "error": str(exc)}


def _safe_segment(value: str) -> str:
    """Sanitise a string for use as a single repo path segment.

    Keeps alnum, dash, underscore, dot; replaces everything else with
    ``_`` so a crafted artifact name can't escape the prefix directory.
    """
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(value)).strip("._")
    return cleaned or "_"


# ── Selection ───────────────────────────────────────────────────────────


def resolve_backend(
    adapter: Any = None,
    registry: Any = None,
    disabled_packages: Any = None,
) -> BackendActions:
    """Pick the active backend.

    Selection precedence:

    1. An explicit choice via ``ARC_BACKEND`` (or ``arc.toml [backend]
       kind``). Built-ins: ``github`` | ``sim2l`` | ``noop``. Any other
       name is resolved through the component registry, so a *package*
       can ship a backend selectable by name. An explicit choice that
       can't activate (e.g. ``github`` with no token) falls back to the
       no-op — with a warning, never silently.
    2. Otherwise infer: :class:`Sim2lBackend` when sim2l is importable
       *and* ``adapter`` exposes ``register_artifact``; else
       :class:`NoopBackend`.

    ``ARC_BACKEND=sim2l`` works with *any* runtime adapter: wrapped when
    the adapter is the sim2l adapter (reusing its session ids), else
    standalone over HTTP (run locally, publish to sim2l).
    """
    kind = _explicit_backend_kind()
    if kind == "github":
        gh = GitHubBackend()
        if gh.is_active():
            return gh
        logger.warning(
            "ARC_BACKEND=github requested but GITHUB_TOKEN/ARC_GITHUB_REPO "
            "are not configured — publishing disabled (noop backend)."
        )
        return NoopBackend()
    if kind == "sim2l":
        if adapter is not None and hasattr(adapter, "register_artifact") and sim2l_importable():
            return Sim2lBackend(adapter)
        # Standalone: execution happens elsewhere (e.g. local adapter);
        # publish to the sim2l services over HTTP.
        standalone = Sim2lBackend(None)
        if standalone.is_active():
            return standalone
        logger.warning(
            "ARC_BACKEND=sim2l requested but no sim2l runtime adapter is "
            "active and the sim2l services are unreachable — publishing "
            "disabled (noop backend). Start the services or set "
            "SIM2L_CATALOG_URL / SIM2L_RESULTS_URL."
        )
        return NoopBackend()
    if kind == "noop":
        return NoopBackend()
    if kind is not None:
        backend = _registry_backend(kind, adapter, registry, disabled_packages)
        if backend is not None:
            return backend
        logger.warning(
            "ARC_BACKEND=%s is not a built-in and no package registered a "
            "backend under that name — publishing disabled (noop backend).",
            kind,
        )
        return NoopBackend()

    # No explicit choice — infer.
    if adapter is not None and hasattr(adapter, "register_artifact") and sim2l_importable():
        return Sim2lBackend(adapter)
    return NoopBackend()


def _registry_backend(
    kind: str, adapter: Any, registry: Any, disabled_packages: Any,
) -> BackendActions | None:
    """Instantiate a package-registered backend named ``kind``, or None.

    The registry stores backend *classes* (mirroring providers/adapters).
    Instantiation tries ``cls()`` first, then ``cls(adapter)`` for backends
    that want the runtime adapter. A backend from a session-disabled
    package is not selectable.
    """
    if registry is None or not hasattr(registry, "get_backend"):
        return None
    try:
        entry = registry.get_backend(kind, disabled_packages=disabled_packages)
    except KeyError:
        return None
    except TypeError:
        try:
            entry = registry.get_backend(kind)
        except KeyError:
            return None
    try:
        if isinstance(entry, type):
            try:
                backend = entry()
            except TypeError:
                backend = entry(adapter)
        else:
            backend = entry
        if not backend.is_active():
            logger.warning(
                "Backend %r resolved but reports inactive — publishing "
                "disabled (noop backend).", kind,
            )
            return NoopBackend()
        return backend
    except Exception as exc:  # noqa: BLE001 — a broken backend must not abort the loop
        logger.warning("Backend %r could not be instantiated: %s", kind, exc)
        return None


def _explicit_backend_kind() -> str | None:
    """Return an explicitly-configured backend kind, or None.

    Checks ``ARC_BACKEND`` first, then ``arc.toml [backend] kind``.
    """
    import os
    kind = os.environ.get("ARC_BACKEND")
    if not kind:
        try:
            from arc.core.config import load_arc_toml
            _path, config = load_arc_toml()
            kind = (config.get("backend") or {}).get("kind")
        except Exception:  # noqa: BLE001 — config is optional
            kind = None
    return kind.strip().lower() if isinstance(kind, str) and kind.strip() else None
