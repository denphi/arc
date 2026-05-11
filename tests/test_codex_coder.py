import importlib
import json

import pytest

from arc.contracts.agent import AgentContext
from arc.schemas.research import ExperimentPlan, ResearchProposal


codex_coder = importlib.import_module("arc.packages.arc-codex.agents.coder")


def _plan() -> ExperimentPlan:
    proposal = ResearchProposal(
        hypothesis="Test hypothesis",
        objective="Generate a codex artifact",
        variables=["x"],
        methodology="compute a deterministic response",
        expected_outcomes="numeric output",
        evaluation_metrics=["result"],
    )
    return ExperimentPlan(
        proposal=proposal,
        artifact_strategy="create",
        parameters={"x": 1.0},
        parameter_sweep={"x": [1.0]},
        success_criteria=["returns result"],
    )


def test_interactive_codex_args_honor_configured_approval_policy(monkeypatch):
    monkeypatch.setenv("ARC_CODEX_APPROVAL_POLICY", "untrusted")

    global_args, args = codex_coder._build_codex_args(interactive_approvals=True)

    assert global_args == ["--ask-for-approval", "untrusted"]
    assert "--json" in args


def test_interactive_codex_args_default_to_on_request(monkeypatch):
    monkeypatch.delenv("ARC_CODEX_APPROVAL_POLICY", raising=False)
    monkeypatch.delenv("ARC_CODEX_INTERACTIVE_APPROVAL_POLICY", raising=False)

    global_args, _ = codex_coder._build_codex_args(interactive_approvals=True)

    assert global_args == ["--ask-for-approval", "on-request"]


def test_interactive_codex_args_ignore_noninteractive_never_policy(monkeypatch):
    monkeypatch.setenv("ARC_CODEX_APPROVAL_POLICY", "never")
    monkeypatch.delenv("ARC_CODEX_INTERACTIVE_APPROVAL_POLICY", raising=False)

    global_args, _ = codex_coder._build_codex_args(interactive_approvals=True)

    assert global_args == ["--ask-for-approval", "on-request"]


def test_interactive_codex_args_allow_explicit_interactive_policy(monkeypatch):
    monkeypatch.setenv("ARC_CODEX_APPROVAL_POLICY", "never")
    monkeypatch.setenv("ARC_CODEX_INTERACTIVE_APPROVAL_POLICY", "untrusted")

    global_args, _ = codex_coder._build_codex_args(interactive_approvals=True)

    assert global_args == ["--ask-for-approval", "untrusted"]


def test_codex_progress_from_json_events():
    assert codex_coder._progress_from_event({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "Created files."},
    }) == "Created files."
    assert codex_coder._progress_from_event({
        "type": "item.started",
        "item": {
            "type": "file_change",
            "changes": [{"path": "/tmp/workflow.py"}, {"path": "/tmp/sim2l.yaml"}],
        },
    }) == "writing workflow.py, sim2l.yaml"
    assert codex_coder._progress_from_event({"type": "turn.completed"}) == "Codex finished"


def test_codex_preflight_rejects_dunder_attributes(tmp_path):
    workflow_path = tmp_path / "workflow.py"
    workflow_path.write_text(
        "def simulate(**inputs):\n"
        "    x = 1.0\n"
        "    return {'result': x.__tan__() if False else x}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="disallowed attribute '__tan__'"):
        codex_coder._preflight_workflow(workflow_path)


def test_codex_artifact_description_uses_generated_schema(tmp_path):
    sim2l_path = tmp_path / "sim2l.yaml"
    workflow_path = tmp_path / "workflow.py"
    workflow_path.write_text("def simulate(**inputs):\n    return {'bandgap_ev': 1.1}\n")
    sim2l_path.write_text(
        "name: bandgap\n"
        "inputs:\n"
        "  thickness_nm:\n"
        "    type: Number\n"
        "    default: 5.0\n"
        "outputs:\n"
        "  bandgap_ev:\n"
        "    type: Number\n"
    )

    description = codex_coder._artifact_description(_plan(), workflow_path, sim2l_path)

    assert "Generate a codex artifact" in description
    assert "Inputs: thickness_nm" in description
    assert "Outputs: bandgap_ev" in description


@pytest.mark.asyncio
async def test_codex_agent_requires_approval_relay_or_explicit_noninteractive(monkeypatch):
    monkeypatch.setattr(codex_coder.shutil, "which", lambda command: "/usr/bin/codex")
    context = AgentContext(session_id="test-session")
    agent = codex_coder.CodexCoderAgent(context=context)

    with pytest.raises(RuntimeError, match="approval relay is not configured"):
        await agent.run(_plan())


@pytest.mark.asyncio
async def test_codex_agent_rejects_invalid_generated_workflow(monkeypatch):
    monkeypatch.setattr(codex_coder.shutil, "which", lambda command: "/usr/bin/codex")

    async def fake_run_bypass(self, command, workspace, prompt, timeout):
        (workspace / "workflow.py").write_text(
            "def simulate(**inputs):\n"
            "    x = 1.0\n"
            "    return {'result': x.__tan__() if False else x}\n",
            encoding="utf-8",
        )
        (workspace / "sim2l.yaml").write_text(
            "name: invalid\ninputs: {}\noutputs: {}\n",
            encoding="utf-8",
        )
        return "", ""

    monkeypatch.setattr(codex_coder.CodexCoderAgent, "_run_bypass", fake_run_bypass)
    context = AgentContext(
        session_id="test-session",
        config={"allow_non_interactive": True},
    )
    agent = codex_coder.CodexCoderAgent(context=context)

    with pytest.raises(RuntimeError, match="Codex generated invalid workflow.py"):
        await agent.run(_plan())


@pytest.mark.asyncio
async def test_declined_codex_approval_raises_typed_error(monkeypatch, tmp_path):
    class FakeReader:
        def __init__(self, lines):
            self._lines = [line.encode() for line in lines]

        async def readline(self):
            if self._lines:
                return self._lines.pop(0)
            return b""

    class FakeStdin:
        def __init__(self):
            self.writes = []

        def write(self, data):
            self.writes.append(data.decode())

        async def drain(self):
            return None

    class FakeProcess:
        def __init__(self):
            event = {
                "type": "item/commandExecution/requestApproval",
                "params": {
                    "approval_id": "approval-1",
                    "command": "touch workflow.py",
                    "reason": "create artifact",
                },
            }
            self.stdout = FakeReader([json.dumps(event) + "\n"])
            self.stderr = FakeReader([])
            self.stdin = FakeStdin()
            self.returncode = 1

        async def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            return b"", b""

    fake_proc = FakeProcess()
    writes = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert kwargs["stdin"] == 102
        return fake_proc

    monkeypatch.setattr(codex_coder.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_coder.pty, "openpty", lambda: (101, 102))
    monkeypatch.setattr(codex_coder.os, "close", lambda fd: None)
    monkeypatch.setattr(codex_coder.os, "write", lambda fd, data: writes.append((fd, data.decode())))
    agent = codex_coder.CodexCoderAgent(context=AgentContext(session_id="test-session"))

    async def callback(approval_id, command, reason):
        return "decline"

    with pytest.raises(codex_coder.CodexApprovalDenied):
        await agent._run_with_approvals("codex", tmp_path, "prompt", callback, None, timeout=5)

    assert writes[0][0] == 101
    assert json.loads(writes[0][1])["params"] == {
        "approval_id": "approval-1",
        "decision": "decline",
    }
