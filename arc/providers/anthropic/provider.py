"""Anthropic Claude provider.

Wraps the Anthropic SDK and implements ProviderContract.
Uses prompt caching on system prompts to reduce cost on repeated calls.
"""

import json
import os

from pydantic import BaseModel

from arc.contracts.provider import ProviderContract


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
        response = client.messages.create(**create_kwargs)
        return response.content[0].text

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
        # Strip markdown fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        return schema.model_validate_json(text)
