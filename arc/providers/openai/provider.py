"""OpenAI provider for ARC."""

import json
import os

from pydantic import BaseModel

from arc.contracts.provider import ProviderContract
from arc.providers.utils import strip_code_fences


class OpenAIProvider(ProviderContract):
    name = "openai"

    def __init__(self, model: str = "gpt-4.1", api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._client = None

    def _get_client(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        import asyncio

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
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        structured_prompt = (
            f"{prompt}\n\n"
            f"Respond with a valid JSON object matching this schema:\n```json\n{schema_json}\n```\n"
            "Output only the JSON object, no other text."
        )
        text = await self.complete(structured_prompt, system=system, **kwargs)
        return schema.model_validate_json(strip_code_fences(text))
