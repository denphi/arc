"""``arc.chat.check._check_provider_auth`` — provider-branch coverage.

The base ``test_chat_check.py`` covers report aggregation and
secret-safety. This file targets the per-provider probe arms
(anthropic / openai / unknown / exception) that weren't otherwise hit.
"""

import pytest

from arc.chat.check import _check_provider_auth


pytestmark = pytest.mark.chat


# ── anthropic branch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_with_token_returns_ok(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-token-12345")
    item = await _check_provider_auth("anthropic", None, None, None)
    assert item.verdict == "ok"
    assert "token present" in item.detail
    # No model list (Anthropic SDK doesn't expose one)
    assert "no list-models probe" in item.detail


# ── openai branch ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_with_token_returns_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-token-12345")
    item = await _check_provider_auth("openai", None, None, None)
    assert item.verdict == "ok"
    assert "token present" in item.detail


# ── unknown provider branch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_provider_returns_warning(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_KEY", "fake-token")  # so token-resolution succeeds
    item = await _check_provider_auth("not-a-known-provider", None, None, None)
    assert item.verdict == "warning"
    assert "unknown provider" in item.detail


# ── exception branch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openwebui_exception_falls_back_to_warning(monkeypatch):
    """When list_models raises, the probe is caught and a warning row is emitted."""
    monkeypatch.setenv("OPENWEBUI_KEY", "fake-token")

    class _Boom:
        def __init__(self, *a, **kw): pass
        def list_models(self):
            raise ConnectionError("provider down")

    monkeypatch.setattr(
        "arc.providers.openwebui.provider.OpenWebUIProvider",
        _Boom,
    )

    item = await _check_provider_auth(
        "openwebui",
        token=None, base_url=None, model=None,
    )
    assert item.verdict == "warning"
    assert "probe failed" in item.detail
    # The exception TYPE name is shown (ConnectionError), the message is NOT
    assert "ConnectionError" in item.detail
    assert "provider down" not in item.detail


# ── explicit-token branch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_token_takes_priority_over_env(monkeypatch):
    """When --token is passed, it wins over env-var fallbacks."""
    monkeypatch.setenv("OPENWEBUI_KEY", "env-token")

    captured = {}
    class _Recorder:
        def __init__(self, base_url=None, token=None):
            captured["token"] = token
            captured["base_url"] = base_url
        def list_models(self):
            return ["model-a", "model-b"]

    monkeypatch.setattr(
        "arc.providers.openwebui.provider.OpenWebUIProvider",
        _Recorder,
    )

    item = await _check_provider_auth(
        "openwebui",
        token="cli-passed-token",
        base_url="https://example.invalid",
        model=None,
    )
    assert item.verdict == "ok"
    assert captured["token"] == "cli-passed-token"
    assert captured["base_url"] == "https://example.invalid"
    assert item.info.get("model_count") == 2


# ── provider resolution from env var ─────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_arg_none_defaults_to_arc_provider_env(monkeypatch):
    """When ``--provider`` is None, ``ARC_PROVIDER`` env var picks the default."""
    monkeypatch.setenv("ARC_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    item = await _check_provider_auth(None, None, None, None)
    assert "anthropic" in item.name


@pytest.mark.asyncio
async def test_provider_arg_none_falls_back_to_openwebui(monkeypatch):
    """No ``--provider``, no ``ARC_PROVIDER`` → defaults to openwebui."""
    monkeypatch.delenv("ARC_PROVIDER", raising=False)
    # No token configured → expect warning, not crash
    item = await _check_provider_auth(None, None, None, None)
    assert "openwebui" in item.name


# ── ok branch with model-count ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_openwebui_lists_models_returns_count(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_KEY", "x")

    class _OK:
        def __init__(self, *a, **kw): pass
        def list_models(self):
            return ["m1", "m2", "m3", "m4", "m5"]

    monkeypatch.setattr(
        "arc.providers.openwebui.provider.OpenWebUIProvider",
        _OK,
    )

    item = await _check_provider_auth("openwebui", None, None, None)
    assert item.verdict == "ok"
    assert "5 models available" in item.detail
    assert item.info["model_count"] == 5
