# Providers

*LLM backends. Core ships `openwebui`; others come from packages. Without one,
ARC runs in stub mode.*

`build_provider(name, *, token, model, base_url, registry, disabled_packages)`
(`arc/providers/__init__.py`) resolves a provider:

1. empty/unset name → `None` (**stub mode** — every agent has a no-LLM path);
2. `openwebui` → the core built-in (an OpenAI-compatible client with a custom
   `base_url` + bearer token — also fronts Ollama and Purdue GenAI);
3. otherwise → a package-registered provider class (e.g. `anthropic` /
   `openai` from `arc-providers`).

A provider whose package is **disabled** for the session is not built
(degrades to stub mode).

## The contract

`ProviderContract` (`arc/contracts/provider.py`):

- `complete(prompt, **kwargs) → str`
- `complete_structured(prompt, schema, **kwargs) → BaseModel`
- `embed(text) → list[float]` (optional)

Providers may try a **native JSON / tool-use** path for structured output and
fall back to prompt-engineered JSON; a shared capability cache
(`arc/providers/utils.py`) avoids retry storms. `strip_code_fences` cleans
fenced responses.

## Configuring a provider

See {doc}`../guides/providers` and {doc}`../reference/configuration`
(`ARC_PROVIDER`, `ARC_MODEL`, `OPENWEBUI_*`, `ANTHROPIC_*`, `OPENAI_*`).

## API reference

```{eval-rst}
.. automodule:: arc.providers
   :members:
   :undoc-members:

.. automodule:: arc.providers.utils
   :members:
   :undoc-members:
```
