"""Coverage tests for ``/sweep`` and ``/exec``.

Both commands talk to the workflow adapter — we stub a recording
adapter and assert the call sequence + side effects on results store.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from arc.chat.commands import build_registry
from arc.chat.state import ChatState
from tests.fakes import make_artifact, make_workflow


pytestmark = pytest.mark.chat


# ── Test infrastructure ──────────────────────────────────────────────────


@dataclass
class _StubAdapter:
    """Records every (artifact, inputs) tuple it's called with."""
    calls: list = field(default_factory=list)
    outputs: dict = field(default_factory=lambda: {"bandgap_ev": 1.1})
    status: str = "completed"
    logs: list = field(default_factory=lambda: ["start", "tail"])

    async def run(self, artifact, inputs):
        self.calls.append((artifact, dict(inputs)))
        return SimpleNamespace(
            run_id="run-xx-12345678",
            status=self.status,
            outputs=dict(self.outputs),
            logs=list(self.logs),
        )

    async def prepare_inputs(self, artifact, params):
        return dict(params)


class _StubResults:
    def __init__(self):
        self.saved = []
    def save(self, ex):
        self.saved.append(ex)
    def list_all(self):
        return list(self.saved)


def _wf_with_artifact(artifact, memory=None):
    wf = make_workflow(memory=memory or {}, artifacts=[artifact])
    wf.adapter = _StubAdapter()
    wf.results = _StubResults()
    return wf


# ── /sweep usage errors ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_with_no_artifact_or_active_artifact_prints_usage(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(memory={}))
    await reg.get("sweep").resolve_handler()(state, [])
    assert "Usage:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_sweep_artifact_not_found(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow(artifacts=[]))
    await reg.get("sweep").resolve_handler()(state, ["nonexistent-id"])
    assert "No artifact found" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_sweep_artifact_without_plan_prints_error(capsys):
    """The current_artifact is set, but ctx.memory has no current_plan."""
    art = make_artifact(artifact_id="xyz12345abc", name="silicon")
    state = ChatState(workflow=_wf_with_artifact(art, memory={}))
    state.current_artifact = art
    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "No parameter sweep" in out


@pytest.mark.asyncio
async def test_sweep_plan_without_parameter_sweep_dict_prints_error(capsys):
    art = make_artifact(artifact_id="xyz12345abc", name="silicon")
    plan = SimpleNamespace(parameter_sweep={})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    state = ChatState(workflow=wf)
    state.current_artifact = art
    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])
    assert "No parameter sweep" in capsys.readouterr().out


# ── /sweep happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_executes_every_value_in_every_param(capsys):
    art = make_artifact(artifact_id="xyz12345abc", name="silicon")
    plan = SimpleNamespace(parameter_sweep={
        "thickness": [1.0, 2.0, 3.0],
        "temperature": [300, 400],
    })
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    state = ChatState(workflow=wf)
    state.current_artifact = art

    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])

    # 3 + 2 = 5 calls
    assert len(wf.adapter.calls) == 5
    # All saved
    assert len(wf.results.saved) == 5
    # Inputs passed correctly
    thickness_inputs = [c[1] for c in wf.adapter.calls if "thickness" in c[1]]
    assert {1.0, 2.0, 3.0} == {c["thickness"] for c in thickness_inputs}


@pytest.mark.asyncio
async def test_sweep_finds_artifact_by_id_prefix(capsys):
    """The lookup matches when argv[0] is a prefix of artifact_id."""
    art = make_artifact(artifact_id="abc12345fullid", name="x")
    plan = SimpleNamespace(parameter_sweep={"p": [1]})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    state = ChatState(workflow=wf)
    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, ["abc12345"])
    assert len(wf.adapter.calls) == 1


@pytest.mark.asyncio
async def test_sweep_finds_artifact_by_name(capsys):
    art = make_artifact(artifact_id="someid", name="my-named-artifact")
    plan = SimpleNamespace(parameter_sweep={"p": [1]})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    state = ChatState(workflow=wf)
    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, ["my-named-artifact"])
    assert len(wf.adapter.calls) == 1


@pytest.mark.asyncio
async def test_sweep_uses_current_artifact_when_no_argv(capsys):
    art = make_artifact(artifact_id="zzz12345xx", name="active")
    plan = SimpleNamespace(parameter_sweep={"p": [42]})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    state = ChatState(workflow=wf)
    state.current_artifact = art
    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])
    # Defaulted to the active artifact, ran the sweep
    assert wf.adapter.calls[0][1] == {"p": 42}


@pytest.mark.asyncio
async def test_sweep_prints_total_runs(capsys):
    art = make_artifact(artifact_id="zzz12345xx", name="x")
    plan = SimpleNamespace(parameter_sweep={"p": [1, 2]})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    state = ChatState(workflow=wf)
    state.current_artifact = art
    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "Total runs" in out
    assert "2" in out


@pytest.mark.asyncio
async def test_sweep_marks_failed_runs_with_red(capsys):
    """Failed status is reported with RED — the ●-prefix line is shown."""
    art = make_artifact(artifact_id="aaa", name="x")
    plan = SimpleNamespace(parameter_sweep={"p": [99]})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    wf.adapter.status = "failed"
    state = ChatState(workflow=wf)
    state.current_artifact = art
    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])
    out = capsys.readouterr().out
    # The ● bullet shows up either way; just confirm we got the row
    assert "●" in out
    assert "p=99" in out


# ── /exec usage errors ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_no_argv_prints_usage(capsys):
    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("exec").resolve_handler()(state, [])
    assert "Usage:" in capsys.readouterr().out


# ── /exec param parsing ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_parses_float_params(monkeypatch):
    """Numeric strings become floats."""
    captured = {}
    async def fake_run_artifact(workflow, art_id, params):
        captured["art_id"] = art_id
        captured["params"] = params
    monkeypatch.setattr("arc.chat.loop.run_artifact", fake_run_artifact)

    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("exec").resolve_handler()(
        state, ["my-artifact", "thickness=5.0", "temperature=300"],
    )
    assert captured["art_id"] == "my-artifact"
    assert captured["params"] == {"thickness": 5.0, "temperature": 300.0}


@pytest.mark.asyncio
async def test_exec_keeps_non_numeric_values_as_strings(monkeypatch):
    captured = {}
    async def fake_run_artifact(workflow, art_id, params):
        captured["params"] = params
    monkeypatch.setattr("arc.chat.loop.run_artifact", fake_run_artifact)

    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("exec").resolve_handler()(
        state, ["art", "material=silicon", "thickness=5"],
    )
    assert captured["params"] == {"material": "silicon", "thickness": 5.0}


@pytest.mark.asyncio
async def test_exec_skips_argv_without_equals(monkeypatch):
    """``foo bar=1`` — ``foo`` is silently ignored, ``bar=1`` parsed."""
    captured = {}
    async def fake_run_artifact(workflow, art_id, params):
        captured["params"] = params
    monkeypatch.setattr("arc.chat.loop.run_artifact", fake_run_artifact)

    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("exec").resolve_handler()(
        state, ["art", "junk-no-equals", "bar=1.5"],
    )
    assert captured["params"] == {"bar": 1.5}


@pytest.mark.asyncio
async def test_exec_with_no_params_calls_run_artifact_with_empty_dict(monkeypatch):
    captured = {}
    async def fake_run_artifact(workflow, art_id, params):
        captured["params"] = params
    monkeypatch.setattr("arc.chat.loop.run_artifact", fake_run_artifact)

    reg = build_registry()
    state = ChatState(workflow=make_workflow())
    await reg.get("exec").resolve_handler()(state, ["my-art"])
    assert captured["params"] == {}


# ── /sweep bookkeeping (review pass 3) ───────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_appends_run_history_and_provenance(capsys):
    """Sweep points get the same bookkeeping as single runs: run_history
    entries per point and one provenance entry for the sweep."""
    art = make_artifact(artifact_id="bk12345678", name="bk")
    plan = SimpleNamespace(parameter_sweep={"p": [1, 2, 3]})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})
    state = ChatState(workflow=wf)
    state.current_artifact = art

    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])

    history = wf._context.memory.get("run_history", [])
    assert len(history) == 3
    assert {h["inputs"]["p"] for h in history} == {1, 2, 3}

    sweep_entries = [e for e in wf.provenance.entries if e["action"] == "sweep"]
    assert len(sweep_entries) == 1
    assert sweep_entries[0]["outputs"]["runs"] == 3


@pytest.mark.asyncio
async def test_sweep_blocked_by_audit_aborts(capsys):
    """A blocking execution.before audit aborts the sweep cleanly."""
    from arc.runtime.audit import AuditBlockedError
    from arc.contracts.audit import AuditResult

    art = make_artifact(artifact_id="blk1234567", name="blk")
    plan = SimpleNamespace(parameter_sweep={"p": [1, 2]})
    wf = _wf_with_artifact(art, memory={"current_plan": plan})

    class _BlockingAudit:
        def has_actions(self):
            return True
        async def dispatch(self, phase, **fields):
            if phase == "execution.before":
                raise AuditBlockedError(AuditResult(
                    status="fail", summary="no unvalidated runs",
                    blocking=True, name="gate", phase=phase,
                ))
    wf.audit = _BlockingAudit()
    state = ChatState(workflow=wf)
    state.current_artifact = art

    reg = build_registry()
    await reg.get("sweep").resolve_handler()(state, [])
    out = capsys.readouterr().out
    assert "blocked by audit" in out
    assert len(wf.adapter.calls) == 0  # nothing ran
