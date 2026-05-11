import importlib

import pytest

from arc.contracts.agent import AgentContext

claude_coder = importlib.import_module("arc.packages.arc-claude-code.agents.coder")


def test_claude_progress_from_system_event():
    assert claude_coder._progress_from_event({
        "type": "system",
        "subtype": "init",
        "model": "claude-test",
    }) == "started  claude-test"


def test_claude_progress_from_assistant_text():
    assert claude_coder._progress_from_event({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Creating workflow and schema."},
            ],
        },
    }) == "Creating workflow and schema."


def test_claude_progress_from_tool_use():
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
    }) == "writing workflow.py"


def test_claude_progress_from_result_event():
    assert claude_coder._progress_from_event({
        "type": "result",
        "num_turns": 3,
        "total_cost_usd": 0.0123,
    }) == "done  3 turn(s)  $0.0123"


def test_claude_mcp_config_does_not_consume_prompt_position():
    args = claude_coder._build_claude_code_args(mcp_config_path="/tmp/arc.mcp.json")

    assert "--mcp-config=/tmp/arc.mcp.json" in args
    assert "--mcp-config" not in args
    assert "--strict-mcp-config" in args


@pytest.mark.asyncio
async def test_claude_bypass_passes_prompt_as_argument(monkeypatch, tmp_path):
    class FakeReader:
        async def readline(self):
            return b""

    class FakeProcess:
        def __init__(self):
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
        return FakeProcess()

    monkeypatch.setattr(
        claude_coder.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    agent = claude_coder.ClaudeCodeCoderAgent(context=AgentContext(session_id="test-session"))

    await agent._run_bypass("claude", tmp_path, "build this artifact", None, timeout=5)

    assert captured["args"][-1] == "build this artifact"
    assert captured["kwargs"]["stdin"] == claude_coder.asyncio.subprocess.DEVNULL
