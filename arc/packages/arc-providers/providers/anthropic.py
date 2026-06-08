"""Anthropic Claude provider.

Wraps the Anthropic SDK and implements ProviderContract.
Uses prompt caching on system prompts to reduce cost on repeated calls.
"""
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


class AnthropicProvider(ProviderContract):
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-4-7", api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None
        # Review item #T17: capability cache lives at module scope keyed by
        # (provider, model) so spinning up a fresh provider per workflow
        # run doesn't re-probe the API on every call.

    @classmethod
    def from_config(cls, *, token=None, model=None, base_url=None):
        """Factory hook for ``arc.providers.build_provider``.

        Anthropic ignores ``base_url``; ``token`` maps to the API key and
        ``model`` falls back to ``ARC_MODEL`` then the class default.
        """
        return cls(
            model=model or os.environ.get("ARC_MODEL", "claude-opus-4-7"),
            api_key=token,
        )

    def list_models(self) -> list[str]:
        """Curated model list (Anthropic has no public list endpoint)."""
        return ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        client = self._get_client()
        messages = [{"role": "user", "content": prompt}]
        create_kwargs: dict = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "messages": messages,
        }
        if system:
            create_kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, lambda: client.messages.create(**create_kwargs))
        # Anthropic responses can contain tool_use / thinking / empty blocks
        # in addition to text. Walk the list and return the first text block;
        # falling back to "" when no text was produced (rather than crashing
        # with IndexError on response.content[0].text).
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""

    async def complete_structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        system: str = "",
        **kwargs,
    ) -> BaseModel:
        """Return a pydantic instance matching ``schema``.

        Prefers native tool-use structured output — review item #A8 — by
        defining a single tool whose ``input_schema`` is the pydantic schema
        and forcing the model to call it. The tool's ``input`` block is
        guaranteed to validate against the schema (it's the Anthropic API's
        own grammar). Falls back to prompt-engineered JSON on any failure.
        """
        supported = get_native_json_support("anthropic", self.model)
        if supported is not False:
            try:
                return await self._complete_structured_native(prompt, schema, system, **kwargs)
            except Exception as exc:
                set_native_json_support("anthropic", self.model, False)
                logger.info(
                    "Anthropic tool-use structured output unavailable for %s "
                    "(%s: %s); falling back to prompt-engineered JSON.",
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
        tool_name = f"emit_{schema.__name__}"
        schema_dict = schema.model_json_schema()
        create_kwargs: dict = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": tool_name,
                    "description": (
                        f"Return a {schema.__name__} object matching the schema."
                    ),
                    "input_schema": schema_dict,
                }
            ],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if system:
            create_kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: client.messages.create(**create_kwargs)
        )
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
                tool_input = block.input
                if isinstance(tool_input, dict):
                    set_native_json_support("anthropic", self.model, True)
                    return schema.model_validate(tool_input)
        raise RuntimeError("Anthropic response did not include the requested tool_use block")

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
