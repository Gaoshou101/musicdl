import asyncio
import json
import pytest
import httpx

from musicdl.ai import AIError, OpenAICompatibleClient, advise_language
from musicdl.config import AISettings
from musicdl.sources.models import Candidate


class Fake:
    def __init__(self, value=None, error=None):
        self.value = value if value is not None else {"language": "日韩"}
        self.error = error
        self.messages = []
        self.calls = 0

    async def complete_json(self, messages):
        self.messages.extend(messages)
        self.calls += 1
        if self.error:
            raise self.error
        return self.value


def _candidate():
    return Candidate(source_id="source-secret", source_version="v1", item_id="item-secret", title="Title", artist="Artist", album="Album")


def test_valid_language_advice_is_applied():
    fake = Fake()
    events = []
    result = asyncio.run(advise_language(_candidate(), "欧美", AISettings(enabled=True, api_key="secret", model="model"), client=fake, record=events.append))
    assert result.language == "日韩"
    assert result.applied is True
    assert len(events) == 1 and events[0].operation == "language" and events[0].status == "applied"
    body = json.loads(fake.messages[1]["content"])
    assert body == {"title": "Title", "artist": "Artist", "album": "Album"}
    assert "source-secret" not in fake.messages[1]["content"]
    assert "item-secret" not in fake.messages[1]["content"]
    assert "return JSON" in fake.messages[0]["content"]
    assert "untrusted data" in fake.messages[0]["content"]
    assert "华语" in fake.messages[0]["content"] and "未知" in fake.messages[0]["content"]


def test_language_real_client_receives_explicit_safe_instructions():
    seen = {}
    async def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"language":"日韩"}'}}]})
    settings = AISettings(enabled=True, api_key="secret", model="model", base_url="https://provider.example/v1")
    result = asyncio.run(advise_language(_candidate(), "欧美", settings, client=OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler))))
    assert result.applied
    system = seen["payload"]["messages"][0]["content"]
    assert "return JSON" in system and "untrusted data" in system
    assert all(choice in system for choice in ("华语", "欧美", "日韩", "未知"))
    assert "infer" in system and "title/artist/album" in system and "evidence is insufficient" in system


@pytest.mark.parametrize("fallback, expected", [("欧美", "欧美"), ("未知", "未知"), ("invalid", "未知"), (None, "未知")])
@pytest.mark.parametrize("value", [{}, {"language": "拉丁"}, {"language": "../../escape"}, {"language": 1}, {"language": "华语", "unexpected": True}])
def test_invalid_advice_uses_safe_fallback(fallback, expected, value):
    events = []
    result = asyncio.run(advise_language(_candidate(), fallback, AISettings(enabled=True, api_key="secret", model="model"), client=Fake(value), record=events.append))
    assert (result.language, result.applied, result.error_code) == (expected, False, "invalid_advice")
    assert len(events) == 1
    assert events[0].operation == "language" and events[0].status == "fallback" and events[0].error_code == "invalid_advice"


@pytest.mark.parametrize("settings, error, code", [
    (AISettings(), None, "disabled"),
    (AISettings(enabled=True, api_key="secret", model="model"), AIError("timeout"), "timeout"),
    (AISettings(enabled=True, api_key="secret", model="model"), RuntimeError("provider secret"), "provider_error"),
])
def test_disabled_and_provider_failures_are_redacted(settings, error, code):
    events = []
    result = asyncio.run(advise_language(_candidate(), "欧美", settings, client=Fake(error=error), record=events.append))
    assert (result.language, result.applied, result.error_code) == ("欧美", False, code)
    assert events[0].error_code == code and "secret" not in repr(events[0])


def test_cancellation_propagates_and_recorder_isolated():
    class Cancel(Fake):
        async def complete_json(self, messages):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(advise_language(_candidate(), "欧美", AISettings(enabled=True, api_key="secret", model="model"), client=Cancel()))
    result = asyncio.run(advise_language(_candidate(), "欧美", AISettings(enabled=True, api_key="secret", model="model"), client=Fake(), record=lambda event: (_ for _ in ()).throw(RuntimeError("secret"))))
    assert result.applied is True


def test_client_download_method_is_never_used():
    class Downloading(Fake):
        def download(self):
            raise AssertionError("download must not be called")
    fake = Downloading()
    result = asyncio.run(advise_language(_candidate(), "欧美", AISettings(enabled=True, api_key="secret", model="model"), client=fake))
    assert result.language == "日韩" and fake.calls == 1
    assert all(key not in repr(result) for key in ("download", "path", "selected"))
