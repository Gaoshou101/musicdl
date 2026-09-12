import asyncio
import json
from functools import wraps

import httpx
import pytest

from musicdl.ai import AIError, OpenAICompatibleClient
from musicdl.config import AISettings


def run_sync(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


def settings(**kwargs):
    values = {
        "enabled": True,
        "base_url": "https://provider.example/v1",
        "api_key": "phase5-secret",
        "model": "phase5-model",
        "timeout": 3.5,
    }
    values.update(kwargs)
    return AISettings(**values)


@run_sync
async def test_client_sends_bounded_openai_compatible_request():
    seen = {}

    async def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"language":"华语"}'}}]})

    client = OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler))
    assert await client.complete_json([{"role": "user", "content": "classify"}]) == {"language": "华语"}
    request = seen["request"]
    assert request.url == "https://provider.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer phase5-secret"
    assert json.loads(request.content) == {
        "model": "phase5-model",
        "messages": [{"role": "user", "content": "classify"}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    assert "phase5-secret" not in repr(client)
    assert "phase5-secret" not in request.content.decode()


@run_sync
async def test_standard_openai_response_with_metadata_is_accepted():
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-example",
                "object": "chat.completion",
                "created": 0,
                "model": "phase5-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"language":"华语"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler))
    assert await client.complete_json([]) == {"language": "华语"}


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("phase5-secret"), httpx.ConnectError("phase5-secret")])
@run_sync
async def test_transport_errors_are_stable_and_redacted(exc):
    async def handler(request):
        raise exc

    client = OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(AIError) as caught:
        await client.complete_json([])
    assert caught.value.code == ("timeout" if isinstance(exc, httpx.ReadTimeout) else "provider_error")
    assert "phase5-secret" not in str(caught.value)
    assert "phase5-secret" not in repr(caught.value)


@pytest.mark.parametrize("status", [401, 429, 500])
@run_sync
async def test_http_errors_are_provider_errors(status):
    async def handler(request):
        return httpx.Response(status, text="provider-secret")

    with pytest.raises(AIError) as caught:
        await OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler)).complete_json([])
    assert caught.value.code == "provider_error"
    assert "provider-secret" not in str(caught.value)
    assert "provider-secret" not in repr(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"choices": []},
        {"choices": [{"message": {"content": "{}"}}, {"message": {"content": "{}"}}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": 1}}]},
        {"choices": [{"message": {"content": "```json\\n{}\\n```"}}]},
        {"choices": [{"message": {"content": "[]"}}]},
        {"choices": [{"message": {"content": "null"}}]},
        {"choices": [{"message": {"content": "{"}}]},
    ],
)
@run_sync
async def test_invalid_provider_responses_are_stable(payload):
    async def handler(request):
        return httpx.Response(200, content=payload if isinstance(payload, str) else json.dumps(payload))

    with pytest.raises(AIError) as caught:
        await OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler)).complete_json([])
    assert caught.value.code == "invalid_response"


@run_sync
async def test_response_content_is_bounded():
    async def handler(request):
        oversized = json.dumps({"padding": "a" * 16380})
        assert len(oversized) > 16384
        return httpx.Response(200, json={"choices": [{"message": {"content": oversized}}]})

    with pytest.raises(AIError) as caught:
        await OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler)).complete_json([])
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize(("length", "expected"), [(16_384, True), (16_385, False)])
@run_sync
async def test_response_content_exact_character_limit(length, expected):
    prefix = '{"x":"'
    suffix = '"}'

    async def handler(request):
        content = prefix + ("a" * (length - len(prefix) - len(suffix))) + suffix
        assert len(content) == length
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler))
    if not expected:
        with pytest.raises(AIError) as caught:
            await client.complete_json([])
        assert caught.value.code == "invalid_response"
    else:
        result = await client.complete_json([])
        assert len(result["x"]) == length - len(prefix) - len(suffix)


@run_sync
async def test_outer_response_body_is_bounded():
    async def handler(request):
        return httpx.Response(200, content=b'{"choices":[{"message":{"content":"{}"}}]}' + b"x" * 65_000)

    with pytest.raises(AIError) as caught:
        await OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler)).complete_json([])
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize("target_size", [65_536, 65_537])
@run_sync
async def test_outer_response_exact_byte_limit_in_chunks(target_size):
    base = b'{"choices":[{"message":{"content":"{}"}}]}'
    body = base + (b" " * (target_size - len(base)))
    assert len(body) == target_size

    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(body), 7):
                yield body[offset : offset + 7]

    async def handler(request):
        return httpx.Response(200, stream=ChunkedStream())

    client = OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler))
    if target_size == 65_537:
        with pytest.raises(AIError) as caught:
            await client.complete_json([])
        assert caught.value.code == "invalid_response"
    else:
        assert await client.complete_json([]) == {}


@run_sync
async def test_slow_response_exceeds_total_deadline():
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"choices":[{"message":{"content":"{}"}}]}'
            await asyncio.sleep(0.2)
            yield b"x"

    async def handler(request):
        return httpx.Response(200, stream=SlowStream())

    with pytest.raises(AIError) as caught:
        await OpenAICompatibleClient(settings(timeout=0.05), transport=httpx.MockTransport(handler)).complete_json([])
    assert caught.value.code == "timeout"


@run_sync
async def test_cancellation_propagates_unchanged():
    async def handler(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler)).complete_json([])
