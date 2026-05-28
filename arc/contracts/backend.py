"""Backend actions contract — the publish-side integrations of the loop.

The research loop produces three kinds of side-effecting output that a
*backend* may persist to a shared system:

  1. **register_artifact** — publish a built artifact to a shared
     catalog so other sessions/users can find and reuse it.
  2. **persist_result** — store an execution's outputs in a results
     store keyed by the run.
  3. **record_execution** — log that an execution happened (drives
     dashboards / "total executions" counters).

These were historically hardwired to sim2l. ``BackendActions``
abstracts them so sim2l becomes *one* backend, not *the* backend. The
default :class:`NoopBackend` makes every action a silent no-op — ARC
runs fully local with no shared persistence and no warnings, and sim2l
(or a future GitHub / cloud backend) is opt-in.

Design notes
------------

- **Every method is best-effort and must not raise.** A backend that
  can't reach its service returns a result dict with ``ok=False`` and a
  reason; it never throws. The loop treats these as advisory.
- **``is_active()`` is the single gate.** The loop asks the backend
  whether it's active before announcing backend-specific UI. An
  inactive backend still answers calls (as no-ops); ``is_active`` just
  lets the loop stay quiet about a backend the user isn't using.
- **Return dicts, not exceptions or bare bools** — so a backend can
  report partial success (artifact deployed locally but catalog push
  failed) the way the sim2l path already does.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from arc.schemas.artifact import ArtifactRecord
from arc.schemas.execution import ExecutionResult


class BackendActions(ABC):
    """The publish-side actions a loop backend can provide.

    Implementations: :class:`arc.runtime.backend.NoopBackend` (default),
    :class:`arc.runtime.backend.Sim2lBackend` (wraps the sim2l adapter).
    """

    name: str = "backend"

    @abstractmethod
    def is_active(self) -> bool:
        """Return True when this backend is usable right now.

        For sim2l: the package is importable *and* (for the services
        tier) reachable. For the no-op backend: always False — there's
        nothing to be active.
        """
        raise NotImplementedError

    @abstractmethod
    async def register_artifact(self, artifact: ArtifactRecord) -> dict[str, Any]:
        """Publish ``artifact`` to the backend's shared catalog.

        Returns a dict with at least ``{"registered": bool}``. On
        success may include ``sim_name`` / ``sim_version`` /
        ``catalog_persisted``; on failure ``{"registered": False,
        "error": "..."}``. Never raises.
        """
        raise NotImplementedError

    @abstractmethod
    async def persist_result(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Store ``execution`` outputs in the backend's results store.

        Returns ``{"persisted": bool, ...}``. Never raises.
        """
        raise NotImplementedError

    @abstractmethod
    async def record_execution(
        self,
        artifact: ArtifactRecord,
        execution: ExecutionResult,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Log that an execution occurred (drives dashboards/counters).

        Returns ``{"recorded": bool, ...}``. Never raises.
        """
        raise NotImplementedError
