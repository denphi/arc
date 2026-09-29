"""Session ids, locked metadata updates, and one active UI job per session."""

from __future__ import annotations

import asyncio
import threading

import pytest

from arc.session import (
    load_session_meta,
    new_session_id,
    save_session_meta,
    update_session_meta,
    validate_session_id,
)
from arc.ui import jobs


def test_new_session_id_is_valid_for_any_prefix():
    for prefix in ("", "Band Gap!", "../../etc", "ünïcode", "x" * 50):
        sid = new_session_id(prefix)
        assert validate_session_id(sid) == sid


def test_new_session_id_never_reuses_an_existing_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path))
    import uuid

    taken = uuid.UUID("11111111111111111111111111111111")
    fresh = uuid.UUID("22222222222222222222222222222222")
    (tmp_path / f"demo-{taken.hex[:12]}").mkdir()
    values = iter([taken, fresh])
    monkeypatch.setattr("arc.session.uuid.uuid4", lambda: next(values))
    assert new_session_id("demo") == f"demo-{fresh.hex[:12]}"


def test_update_session_meta_merges_and_rejects_unknown_fields():
    sid = new_session_id("meta")
    save_session_meta(sid, "goal", 1, None, None, [], {"a": 1}, {})
    update_session_meta(sid, target={"b": 2})
    meta = load_session_meta(sid)
    assert meta["goal"] == "goal" and meta["iteration"] == 1 and meta["target"] == {"b": 2}
    with pytest.raises(TypeError):
        update_session_meta(sid, bogus=1)


def test_concurrent_run_history_appends_are_not_lost():
    """Read-modify-write under the lock: no appends dropped."""
    sid = new_session_id("race")
    update_session_meta(sid, run_history=[])
    from arc.session import _meta_lock

    def append(i: int) -> None:
        with _meta_lock(sid):
            history = load_session_meta(sid)["run_history"]
            history.append(i)
            # update_session_meta re-acquires the lock: flock is per open
            # file description, so write directly here instead.
            import json

            from arc.session import _meta_path
            from arc.utils.io import atomic_write_text

            meta = load_session_meta(sid)
            meta["run_history"] = history
            atomic_write_text(_meta_path(sid), json.dumps(meta))

    threads = [threading.Thread(target=append, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(load_session_meta(sid)["run_history"]) == list(range(20))


def test_registry_refuses_a_second_active_job_for_the_same_session():
    async def scenario():
        registry = jobs.JobRegistry()
        gate = asyncio.Event()

        async def body(job):
            await gate.wait()
            return {}

        first = registry.start("research", "s1", body)
        with pytest.raises(jobs.SessionBusyError):
            registry.start("research", "s1", body)
        other = registry.start("research", "s2", body)
        gate.set()
        await asyncio.sleep(0)
        await asyncio.gather(first._task, other._task)
        # Finished → the session accepts a new job again.
        await registry.start("research", "s1", body)._task

    asyncio.run(scenario())
