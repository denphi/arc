"""OpenWebUI / Purdue GenAI provider.

Talks to any OpenAI-compatible endpoint (OpenWebUI, vLLM, Ollama, etc.)
using a bearer token instead of an API key. Useful for accessing open-weight
models (Llama, Mistral, Qwen, etc.) via institutional gateways.

Environment variables:
    OPENWEBUI_URL    Base URL of the OpenAI-compatible API.
                     Default: https://genai.rcac.purdue.edu/api
    OPENWEBUI_KEY    Bearer token for authentication.
    OPENWEBUI_MODEL  Model name to use (e.g. "llama3", "mistral").
                     Default: first available model from the /models endpoint.
"""

import json
import os
from typing import Any

from pydantic import BaseModel

from arc.contracts.provider import ProviderContract

DEFAULT_URL = "https://genai.rcac.purdue.edu/api"
DEFAULT_TIMEOUT = 120.0


class OpenWebUIProvider(ProviderContract):
    name = "openwebui"

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or os.environ.get("OPENWEBUI_URL", DEFAULT_URL)).rstrip("/")
        # Strip trailing /chat/completions if someone pastes the full endpoint URL.
        if self.base_url.endswith("/chat/completions"):
            self.base_url = self.base_url[: -len("/chat/completions")]

        self.token = token or os.environ.get("OPENWEBUI_KEY", "")
        self._model = model or os.environ.get("OPENWEBUI_MODEL", "")
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.token,
                timeout=self.timeout,
            )
        return self._client

    def _resolve_model(self) -> str:
        if self._model:
            return self._model
        # Auto-detect first available model.
        try:
            client = self._get_client()
            models = client.models.list()
            first = next(iter(models), None)
            if first:
                self._model = first.id
                return self._model
        except Exception:
            pass
        raise ValueError(
            "No model configured and auto-detection failed. "
            "Set OPENWEBUI_MODEL or pass model= to OpenWebUIProvider."
        )

    async def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        import asyncio
        client = self._get_client()
        model = self._resolve_model()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        max_tokens  = kwargs.get("max_tokens", 4096)
        temperature = kwargs.get("temperature", 0.2)

        def _call():
            return client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(None, _call)
        except asyncio.CancelledError:
            raise
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
            f"Respond with a valid JSON object matching this schema:\n"
            f"```json\n{schema_json}\n```\n"
            "Output only the JSON object, no markdown, no other text."
        )
        text = await self.complete(structured_prompt, system=system, **kwargs)
        text = text.strip()
        # Strip markdown fences if the model wraps its output.
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        return schema.model_validate_json(text)

    def list_models(self) -> list[str]:
        """Return available model IDs from the endpoint."""
        try:
            client = self._get_client()
            return [m.id for m in client.models.list()]
        except Exception as exc:
            return [f"error: {exc}"]
