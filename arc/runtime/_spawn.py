"""Run a spawn-safe worker in a child process and collect its single result.

Shared by the local executor and the workflow-safety validators. The worker
must accept a ``multiprocessing`` queue as its last argument and ``put``
exactly one result on it.

Why this exists: a child that has put data on a ``multiprocessing.Queue``
cannot exit until its feeder thread has flushed that data into the
underlying pipe, and the pipe only drains when the parent reads. Calling
``proc.join(timeout)`` *before* reading therefore deadlocks for any result
larger than the OS pipe buffer (~64 KiB): the parent waits out the full
timeout, kills the child, and discards a successful result. Here the parent
reads first (with a deadline) and only then joins.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as queue_mod
import time
from dataclasses import dataclass
from typing import Any, Callable

# How often to check that the child is still alive while waiting for its
# result. ``queue.get`` returns as soon as data arrives, so this only bounds
# how long a crashed child goes unnoticed.
_LIVENESS_POLL_SECONDS = 0.1


@dataclass
class SpawnOutcome:
    result: Any = None          # whatever the worker put on the queue, or None
    timed_out: bool = False
    exitcode: int | None = None


def _stop(proc) -> None:
    """SIGTERM, then SIGKILL if the child ignores it."""
    proc.terminate()
    proc.join(5)
    if proc.is_alive():
        proc.kill()
        proc.join()


def run_spawned(
    target: Callable[..., None],
    args: tuple,
    *,
    timeout: float,
    result_grace: float = 5.0,
) -> SpawnOutcome:
    """Run ``target(*args, queue)`` in a spawned child, bounded by ``timeout``.

    Returns a :class:`SpawnOutcome`. ``result`` is ``None`` when the child
    died (or timed out) without reporting. ``result_grace`` is how long to
    wait for data still in flight after the child has already exited.
    """
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=target, args=(*args, queue))
    proc.start()
    outcome = SpawnOutcome()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome.timed_out = True
                break
            try:
                outcome.result = queue.get(timeout=min(_LIVENESS_POLL_SECONDS, remaining))
                break
            except queue_mod.Empty:
                if not proc.is_alive():
                    # Exited: anything it put is either readable now or lost.
                    try:
                        outcome.result = queue.get(timeout=result_grace)
                    except queue_mod.Empty:
                        pass
                    break
    finally:
        if outcome.timed_out:
            _stop(proc)
        else:
            proc.join(5)
            if proc.is_alive():
                # Reported but won't exit (e.g. stray non-daemon thread).
                _stop(proc)
        outcome.exitcode = proc.exitcode
        queue.close()
    return outcome
