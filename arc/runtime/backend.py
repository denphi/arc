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


def sim2l_services_active(timeout: float = 1.5) -> bool:
    """Best-effort check that the sim2l catalog service responds.

    Gates the *publish* actions specifically. We probe the catalog
    health endpoint; failure (or no service) means "don't publish".
    The probe is cheap and only runs when something asks — never on a
    hot path per-execution.
    """
    import os
    catalog_url = os.environ.get("SIM2L_CATALOG_URL", "http://localhost:8002")
    try:
        import requests
        resp = requests.get(f"{catalog_url.rstrip('/')}/health", timeout=timeout)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


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

    Wraps a ``Sim2LRuntimeAdapter`` (the object that already knows the
    catalog/results URLs + session ids). ``register_artifact``
    delegates to the adapter's public method. ``persist_result`` and
    ``record_execution`` are handled *inline* by
    ``Sim2LRuntimeAdapter.run()`` today — so here they report that and
    no-op, rather than double-pushing. If the adapter's run() is ever
    split so publishing is external, these become the seam to move it
    behind.
    """

    name = "sim2l"

    def __init__(self, adapter: Any):
        # ``adapter`` is a Sim2LRuntimeAdapter. We hold it loosely (Any)
        # so this module doesn't hard-import the sim2l adapter at module
        # scope — keeping the import cost off the no-op path.
        self._adapter = adapter

    def is_active(self) -> bool:
        # The package must be importable for the adapter to do anything.
        # Service reachability gates *persistence* but the adapter
        # already degrades gracefully when services are down, so package
        # importability is the right gate for "this backend is in play".
        return sim2l_importable() and self._adapter is not None

    async def register_artifact(self, artifact: ArtifactRecord) -> dict[str, Any]:
        registrar = getattr(self._adapter, "register_artifact", None)
        if registrar is None:
            return {"registered": False, "error": "adapter has no register_artifact"}
        try:
            result = registrar(artifact)
            if hasattr(result, "__await__"):
                result = await result
            return result if isinstance(result, dict) else {"registered": bool(result)}
        except Exception as exc:  # noqa: BLE001 — publishing is best-effort
            logger.debug("sim2l register_artifact failed: %s", exc)
            return {"registered": False, "error": str(exc)}

    async def persist_result(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        # Sim2LRuntimeAdapter.run() pushes results to the results service
        # inline (see _push_to_results). We don't re-push here to avoid
        # duplicate /register_direct calls.
        return {"persisted": True, "handled_inline": True, "backend": "sim2l"}

    async def record_execution(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        # Likewise recorded inline via _push_to_catalog_execution_registry.
        return {"recorded": True, "handled_inline": True, "backend": "sim2l"}


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


def _safe_segment(value: str) -> str:
    """Sanitise a string for use as a single repo path segment.

    Keeps alnum, dash, underscore, dot; replaces everything else with
    ``_`` so a crafted artifact name can't escape the prefix directory.
    """
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(value)).strip("._")
    return cleaned or "_"


# ── Selection ───────────────────────────────────────────────────────────


def resolve_backend(adapter: Any = None) -> BackendActions:
    """Pick the active backend.

    Selection precedence:

    1. An explicit choice via ``ARC_BACKEND`` (or ``arc.toml [backend]
       kind``): ``github`` | ``sim2l`` | ``noop``. An explicit choice
       that can't activate (e.g. ``github`` with no token) falls back to
       the no-op rather than silently inferring something else.
    2. Otherwise infer: :class:`Sim2lBackend` when sim2l is importable
       *and* ``adapter`` exposes ``register_artifact``; else
       :class:`NoopBackend`.

    The ``adapter`` is passed so the sim2l backend reuses the adapter's
    already-configured catalog/results URLs + session ids rather than
    constructing a second one.
    """
    kind = _explicit_backend_kind()
    if kind == "github":
        gh = GitHubBackend()
        return gh if gh.is_active() else NoopBackend()
    if kind == "sim2l":
        if adapter is not None and hasattr(adapter, "register_artifact") and sim2l_importable():
            return Sim2lBackend(adapter)
        return NoopBackend()
    if kind == "noop":
        return NoopBackend()

    # No explicit choice — infer.
    if adapter is not None and hasattr(adapter, "register_artifact") and sim2l_importable():
        return Sim2lBackend(adapter)
    return NoopBackend()


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
