import pytest

from arc.providers.openwebui.provider import OpenWebUIProvider


class _FailingModelsClient:
    class models:
        @staticmethod
        def list():
            raise RuntimeError("boom")


def test_openwebui_list_models_returns_empty_on_error():
    provider = OpenWebUIProvider(base_url="https://example.invalid", token="x", model="m")
    provider._client = _FailingModelsClient()

    assert provider.list_models() == []


@pytest.mark.asyncio
async def test_openwebui_complete_none_content_returns_empty_string():
    class Message:
        content = None

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    class ChatCompletions:
        @staticmethod
        def create(**kwargs):
            return Response()

    class Chat:
        completions = ChatCompletions()

    class Client:
        chat = Chat()

    provider = OpenWebUIProvider(base_url="https://example.invalid", token="x", model="m")
    provider._client = Client()

    assert await provider.complete("hello") == ""


@pytest.mark.asyncio
async def test_openwebui_complete_folds_system_into_user_on_system_role_error():
    class Message:
        content = "ok"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    class ChatCompletions:
        calls = []

        @classmethod
        def create(cls, **kwargs):
            cls.calls.append(kwargs["messages"])
            if len(cls.calls) == 1:
                raise RuntimeError("unsupported role: system")
            return Response()

    class Chat:
        completions = ChatCompletions()

    class Client:
        chat = Chat()

    provider = OpenWebUIProvider(base_url="https://example.invalid", token="x", model="m")
    provider._client = Client()

    assert await provider.complete("hello", system="be precise") == "ok"
    assert ChatCompletions.calls[0][0]["role"] == "system"
    assert ChatCompletions.calls[1] == [{
        "role": "user",
        "content": "System instructions:\nbe precise\n\nUser request:\nhello",
    }]
