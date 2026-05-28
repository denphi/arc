import importlib

import pytest

from arc.contracts.agent import AgentContext
from arc.schemas.research import ExperimentPlan, ResearchProposal

claude_coder = importlib.import_module("arc.packages.arc-claude-code.agents.coder")


def _fresh_delta_state():
    """Per-call streaming buffers expected by ``_progress_from_event``."""
    return {}, {}


def _plan() -> ExperimentPlan:
    proposal = ResearchProposal(
        hypothesis="Test hypothesis",
        objective="Generate a Claude Code artifact",
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


def test_claude_progress_from_system_event():
    buf, emitted = _fresh_delta_state()
    assert claude_coder._progress_from_event({
        "type": "system",
        "subtype": "init",
        "model": "claude-test",
    }, buf, emitted) == "started  claude-test"


def test_claude_progress_from_assistant_text():
    buf, emitted = _fresh_delta_state()
    assert claude_coder._progress_from_event({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Creating workflow and schema."},
            ],
        },
    }, buf, emitted) == "Creating workflow and schema."


def test_claude_progress_from_tool_use():
    buf, emitted = _fresh_delta_state()
    assert claude_coder._progress_from_event({
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": "/tmp/workflow.py"},
                },
            ],
        },
    }, buf, emitted) == "writing workflow.py"


def test_claude_progress_from_result_event():
    buf, emitted = _fresh_delta_state()
    assert claude_coder._progress_from_event({
        "type": "result",
        "num_turns": 3,
        "total_cost_usd": 0.0123,
    }, buf, emitted) == "done  3 turn(s)  $0.0123"


def test_claude_mcp_config_does_not_consume_prompt_position():
    args = claude_coder._build_claude_code_args(mcp_config_path="/tmp/arc.mcp.json")

    assert "--mcp-config=/tmp/arc.mcp.json" in args
    assert "--mcp-config" not in args
    assert "--strict-mcp-config" in args


@pytest.mark.asyncio
async def test_claude_bypass_writes_prompt_to_stdin(monkeypatch, tmp_path):
    """Production sends the prompt over stdin (not as an argv element).

    Verifies the subprocess is launched with ``stdin=PIPE`` and that
    the prompt bytes land in the stdin transport.
    """
    class FakeReader:
        async def readline(self):
            return b""

    class FakeStdinWriter:
        def __init__(self):
            self.written = bytearray()
            self.eof_called = False

        def write(self, data):
            self.written.extend(data)

        def write_eof(self):
            self.eof_called = True

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdinWriter()
            self.stdout = FakeReader()
            self.stderr = FakeReader()
            self.returncode = 0

        async def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            return b"", b""

    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["proc"] = FakeProcess()
        return captured["proc"]

    monkeypatch.setattr(
        claude_coder.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    agent = claude_coder.ClaudeCodeCoderAgent(context=AgentContext(session_id="test-session"))

    await agent._run_bypass("claude", tmp_path, "build this artifact", None, timeout=5)

    # The prompt is NOT in argv any more; it's piped on stdin.
    assert "build this artifact" not in captured["args"]
    # Subprocess is launched with a PIPE'd stdin so production can write to it.
    assert captured["kwargs"]["stdin"] == claude_coder.asyncio.subprocess.PIPE
    # The fake stdin received the prompt bytes.
    assert b"build this artifact" in captured["proc"].stdin.written
    assert captured["proc"].stdin.eof_called is True


@pytest.mark.asyncio
async def test_claude_requires_permission_callback_for_noninteractive_run(monkeypatch):
    monkeypatch.setattr(claude_coder.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.delenv("ARC_CLAUDE_CODE_ALLOW_NON_INTERACTIVE", raising=False)
    agent = claude_coder.ClaudeCodeCoderAgent(context=AgentContext(session_id="test-session"))

    with pytest.raises(RuntimeError) as exc:
        await agent.run(_plan())

    assert "permission relay is not configured" in str(exc.value)
