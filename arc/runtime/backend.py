"""Backend implementations + selection.

Two backends ship today:

  * :class:`NoopBackend` — the default. Every action is a silent no-op
    and ``is_active()`` is False. ARC runs fully local: it ideates,
    plans, builds, validates, *executes locally*, reviews, and
    improves, with no shared catalog/results persistence and no
    warnings. sim2l is opt-in.

  * :class:`Sim2lBackend` — wraps a ``Sim2LRuntimeAdapter`` and routes
    the publish actions to the sim2l catalog/results services. Active
    when the sim2l package is importable.

:func:`resolve_backend` picks one: sim2l when active, otherwise the
no-op. A future ``GitHubBackend`` (see design/TODO.md item 15) would
slot in here as a third implementation.

Why a backend rather than more adapter methods?
-----------------------------------------------

Execution ("where the workflow runs") and publishing ("where its
artifacts + results go") are orthogonal. A user can run locally but
publish to sim2l, or run on sim2l but not publish, etc. Keeping the
backend separate from the runtime adapter lets those vary
independently.
"""

from __future__ import annotations

import logging
from typing import Any

from arc.contracts.backend import BackendActions
from arc.schemas.artifact import ArtifactRecord
from arc.schemas.execution import ExecutionResult

logger = logging.getLogger(__name__)


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


# ── Selection ───────────────────────────────────────────────────────────


def resolve_backend(adapter: Any = None) -> BackendActions:
    """Pick the active backend.

    Returns :class:`Sim2lBackend` when sim2l is importable *and* the
    given ``adapter`` exposes ``register_artifact`` (i.e. it's a
    Sim2LRuntimeAdapter). Otherwise the silent :class:`NoopBackend`.

    The ``adapter`` is passed so the sim2l backend reuses the adapter's
    already-configured catalog/results URLs + session ids rather than
    constructing a second one.
    """
    if adapter is not None and hasattr(adapter, "register_artifact") and sim2l_importable():
        return Sim2lBackend(adapter)
    return NoopBackend()
