"""In-process async job registry for the browser UI.

Long-running research/execution work must not block the browser. A job is
launched with :meth:`JobRegistry.start`, runs under ``asyncio.create_task``,
and its state is polled via ``GET /api/jobs/{id}`` or streamed via SSE
(``events.py``). Cancellation cancels the task.

Scope: an **in-process, single-worker** registry for the local developer UI.
Recent job records are kept in memory (capped); final results are also
persisted to the normal session stores by the job body itself. A durable /
multi-worker queue would slot in behind this same interface later.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from arc.ui.events import EventStream

# Job lifecycle states (mirrors ui_todo.md).
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
ERROR = "error"
CANCELLED = "cancelled"
_TERMINAL = {COMPLETED, ERROR, CANCELLED}


@dataclass
class Job:
    job_id: str
    kind: str
    session_id: str | None
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: float = 0.0
    result: Any = None
    error: str | None = None
    events: EventStream = field(default_factory=EventStream)
    _task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


# A job body receives the Job (so it can emit events / set progress) and
# returns the final result payload.
JobBody = Callable[["Job"], Awaitable[Any]]


class JobRegistry:
    def __init__(self, *, max_records: int = 100) -> None:
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._max_records = max_records

    def start(self, kind: str, session_id: str | None, body: JobBody) -> Job:
        job = Job(job_id=f"job-{uuid.uuid4().hex[:12]}", kind=kind, session_id=session_id)
        self._jobs[job.job_id] = job
        self._evict()
        job._task = asyncio.create_task(self._run(job, body))
        return job

    async def _run(self, job: Job, body: JobBody) -> None:
        job.status = RUNNING
        job.started_at = time.time()
        job.events.emit("status", "Job started", status=RUNNING)
        try:
            job.result = await body(job)
            job.status = COMPLETED
            job.progress = 1.0
            job.events.emit("status", "Job completed", status=COMPLETED)
        except asyncio.CancelledError:
            job.status = CANCELLED
            job.events.emit("status", "Job cancelled", status=CANCELLED)
            # Don't re-raise: the task is ending normally from our POV.
        except Exception as exc:  # noqa: BLE001 — surface to the client
            job.status = ERROR
            job.error = str(exc)
            job.events.emit("warning", f"Job failed: {exc}", status=ERROR)
        finally:
            job.finished_at = time.time()
            job.events.close()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        return [job.to_dict() for job in self._jobs.values()]

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status in _TERMINAL or job._task is None:
            return False
        job._task.cancel()
        try:
            await job._task
        except asyncio.CancelledError:
            pass
        return True

    def _evict(self) -> None:
        # Keep the registry bounded; only drop terminal jobs, oldest first.
        while len(self._jobs) > self._max_records:
            for jid, job in list(self._jobs.items()):
                if job.status in _TERMINAL:
                    self._jobs.pop(jid, None)
                    break
            else:
                break  # nothing terminal to evict yet


# Module-level singleton used by the server (one registry per process).
registry = JobRegistry()
