"""Phase 0 baseline: ``_answer_question`` (the chit-chat path)."""

import pytest

from arc import chat
from tests.fakes import FakeProvider, make_workflow


pytestmark = pytest.mark.chat


@pytest.mark.asyncio
async def test_answer_question_uses_provider(capsys):
    provider = FakeProvider(replies=["Bandgap is the energy gap between bands."])
    wf = make_workflow(memory={}, provider=provider)
    await chat._answer_question(wf, "what is bandgap?")
    out = capsys.readouterr().out
    assert "Bandgap is the energy gap" in out
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_answer_question_stub_mode_prints_helpful_message(capsys):
    wf = make_workflow(memory={}, provider=None)
    await chat._answer_question(wf, "what is bandgap?")
    out = capsys.readouterr().out
    # Stub mode should explain what /run does, not silently no-op
    assert "/help" in out or "/run" in out or "stub" in out.lower()


@pytest.mark.asyncio
async def test_answer_question_includes_goal_context_when_active():
    provider = FakeProvider(replies=["ok"])
    wf = make_workflow(memory={"primary_goal": "optimize bandgap to 1.1 eV"},
                       provider=provider)
    await chat._answer_question(wf, "what's the current goal?")
    system = provider.calls[0]["system"]
    assert "optimize bandgap" in system


@pytest.mark.asyncio
async def test_answer_question_handles_provider_exception_gracefully(capsys):
    class BoomProvider:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("API down")
    wf = make_workflow(memory={}, provider=BoomProvider())
    # Must not raise — chat must stay alive
    await chat._answer_question(wf, "anything")
    out = capsys.readouterr().out.lower()
    assert "could not answer" in out or "error" in out
