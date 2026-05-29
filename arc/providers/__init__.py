"""ARC provider package.

Providers wrap LLM APIs and expose them through ``ProviderContract``.

Core ships exactly **one** provider — ``openwebui`` — because it speaks
the universal OpenAI-compatible API and therefore reaches Ollama, vLLM,
and institutional gateways out of the box. Every other provider
(anthropic, openai, …) is delivered by a **package** that registers it
via its ``package.yaml`` ``provides.providers`` block; the bundled
``arc-providers`` package ships anthropic + openai.

``build_provider`` is the single resolution point. The four historical
``if provider == …`` ladders (workflow / API / CLI) all delegate here, so
adding a provider is one package edit, not four core edits.

Provider classes opt into construction via an optional ``from_config``
classmethod::

    @classmethod
    def from_config(cls, *, token=None, model=None, base_url=None):
        return cls(...)

This keeps the factory provider-agnostic — it never needs to know that
anthropic takes ``api_key`` while openwebui takes ``token``/``base_url``.
A class without ``from_config`` is best-effort constructed with whichever
of those kwargs its ``__init__`` accepts.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Provider names core resolves without any package. Only openwebui.
_BUILTIN = {"openwebui"}


def _instantiate(provider_class: type, *, token, model, base_url):
    """Construct a provider class, preferring its ``from_config`` hook."""
    from_config = getattr(provider_class, "from_config", None)
    if callable(from_config):
        return from_config(token=token, model=model, base_url=base_url)
    # Best-effort: pass only the kwargs the constructor actually accepts.
    candidates = {"token": token, "model": model, "base_url": base_url, "api_key": token}
    try:
        params = inspect.signature(provider_class).parameters
        kwargs = {k: v for k, v in candidates.items() if k in params and v is not None}
    except (TypeError, ValueError):
        kwargs = {}
    return provider_class(**kwargs)


def build_provider(
    name: str | None,
    *,
    token: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    registry: Any = None,
):
    """Resolve and instantiate an LLM provider by ``name``.

    Resolution order:
      1. empty/unset name → ``None`` (the loop runs in deterministic
         stub mode — every agent has a no-LLM path);
      2. ``openwebui`` → the core built-in;
      3. otherwise → a package-registered provider class from
         ``registry`` (loaded from a ``provides.providers`` manifest).

    Returns a provider instance, or ``None`` when the name is empty or
    can't be resolved (kept tolerant so a typo degrades to stub mode
    rather than crashing the run).
    """
    if not name:
        return None
    name = name.strip().lower()

    if name == "openwebui":
        from arc.providers.openwebui.provider import OpenWebUIProvider
        return _instantiate(OpenWebUIProvider, token=token, model=model, base_url=base_url)

    if registry is not None:
        try:
            provider_class = registry.get_provider(name)
        except KeyError:
            provider_class = None
        if provider_class is not None:
            try:
                return _instantiate(provider_class, token=token, model=model, base_url=base_url)
            except Exception as exc:  # noqa: BLE001 — bad provider config → stub mode
                logger.warning("Provider %r failed to construct (%s) — no provider", name, exc)
                return None

    logger.warning(
        "Unknown provider %r (core ships only 'openwebui'; others come from "
        "packages via provides.providers). Running without a provider.", name,
    )
    return None


def list_provider_names(registry: Any = None) -> list[str]:
    """Names ``build_provider`` can resolve: the builtin + registered ones."""
    names = set(_BUILTIN)
    if registry is not None:
        try:
            names.update(registry.list_providers())
        except Exception:  # noqa: BLE001
            pass
    return sorted(names)
