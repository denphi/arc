"""OpenAI provider for ARC."""
from __future__ import annotations


import asyncio
import json
import logging
import os

from pydantic import BaseModel

from arc.contracts.provider import ProviderContract
from arc.providers.utils import (
    get_native_json_support,
    set_native_json_support,
    strip_code_fences,
)

logger = logging.getLogger(__name__)


class OpenAIProvider(ProviderContract):
    name = "openai"

    def __init__(self, model: str = "gpt-4.1", api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._client = None
        # Review item #T17: capability cache moved to a shared module-level
        # dict keyed by (provider, model). See arc.providers.utils.

    @classmethod
    def from_config(cls, *, token=None, model=None, base_url=None):
        """Factory hook for ``arc.providers.build_provider``.

        OpenAI ignores ``base_url``; ``token`` maps to the API key and
        ``model`` falls back to ``ARC_MODEL`` then the class default.
        """
        return cls(
            model=model or os.environ.get("ARC_MODEL", "gpt-4.1"),
            api_key=token,
        )

    def list_models(self) -> list[str]:
        """Curated model list."""
        return ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]

    def _get_client(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        client = self._get_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def _call():
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=kwargs.get("max_tokens", 4096),
            )

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call)
        return response.choices[0].message.content

    async def complete_structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        system: str = "",
        **kwargs,
    ) -> BaseModel:
        """Return a pydantic instance matching ``schema``.

        Prefers the native structured-output API (``response_format={"type":
        "json_schema", ...}``) — review item #A8 — which gives the model a
        compiled grammar instead of asking it to "please reply with JSON".
        Falls back to the legacy prompt-engineering path when the API
        version or model doesn't support strict JSON mode.
        """
        supported = get_native_json_support("openai", self.model)
        if supported is not False:
            try:
                return await self._complete_structured_native(prompt, schema, system, **kwargs)
            except Exception as exc:
                # First failure flips the shared toggle so every new
                # provider instance for this model skips the failing path.
                set_native_json_support("openai", self.model, False)
                logger.info(
                    "OpenAI native JSON mode unavailable for %s (%s: %s); "
                    "falling back to prompt-engineered structured output.",
                    self.model, type(exc).__name__, exc,
                )

        return await self._complete_structured_legacy(prompt, schema, system, **kwargs)

    async def _complete_structured_native(
        self,
        prompt: str,
        schema: type[BaseModel],
        system: str,
        **kwargs,
    ) -> BaseModel:
        client = self._get_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        schema_dict = schema.model_json_schema()

        def _call():
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=kwargs.get("max_tokens", 4096),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "schema": schema_dict,
                        "strict": False,
                    },
                },
            )

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, _call)
        text = response.choices[0].message.content or ""
        set_native_json_support("openai", self.model, True)
        return schema.model_validate_json(text)

    async def _complete_structured_legacy(
        self,
        prompt: str,
        schema: type[BaseModel],
        system: str,
        **kwargs,
    ) -> BaseModel:
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        structured_prompt = (
            f"{prompt}\n\n"
            f"Respond with a valid JSON object matching this schema:\n```json\n{schema_json}\n```\n"
            "Output only the JSON object, no other text."
        )
        text = await self.complete(structured_prompt, system=system, **kwargs)
        return schema.model_validate_json(strip_code_fences(text))
