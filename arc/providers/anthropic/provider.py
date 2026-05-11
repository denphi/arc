"""Anthropic Claude provider.

Wraps the Anthropic SDK and implements ProviderContract.
Uses prompt caching on system prompts to reduce cost on repeated calls.
"""

import json
import os

from pydantic import BaseModel

from arc.contracts.provider import ProviderContract
from arc.providers.utils import strip_code_fences


class AnthropicProvider(ProviderContract):
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-4-7", api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        import asyncio

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
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        structured_prompt = (
            f"{prompt}\n\n"
            f"Respond with a valid JSON object matching this schema:\n```json\n{schema_json}\n```\n"
            "Output only the JSON object, no other text."
        )
        text = await self.complete(structured_prompt, system=system, **kwargs)
        return schema.model_validate_json(strip_code_fences(text))
