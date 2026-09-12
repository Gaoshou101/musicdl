import asyncio
import json

import pytest
import httpx

from musicdl.ai import AIError, OpenAICompatibleClient, advise_ranking
from musicdl.ai import service as ai_service
from musicdl.config import AISettings
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult, SourceStatus, search_result_version


def _search():
    candidates = tuple(
        Candidate(source_id=f"source-{i}", source_version="1", item_id=f"item-{i}", title=title, artist="Artist", album="Album", duration=120, bitrate=320, format="mp3")
        for i, title in enumerate(("first", "second", "third"), 1)
    )
    return SearchResult(candidates, (SourceStatus("source-1", "1", "ok", 3),), search_result_version(candidates))


class Fake:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.messages, self.calls = value, error, [], 0

    async def complete_json(self, messages):
        self.calls += 1
        self.messages.extend(messages)
        if self.error:
            raise self.error
        return self.value


def settings(**values):
    return AISettings(enabled=True, api_key="secret", model="model", **values)


def test_valid_advice_reorders_only_disclosed_prefix():
    base = _search()
    candidates = tuple(
        candidate.model_copy(update={
            "source_id": "url-secret",
            "source_version": "authorization-secret",
            "item_id": "session-secret",
        })
        for candidate in base.candidates
    )
    search = SearchResult(candidates, base.statuses, search_result_version(candidates))
    fake = Fake({"ordered_tokens": ["candidate-2", "candidate-1"]})
    events = []
    result = asyncio.run(advise_ranking(search, "query", settings(max_candidates=2), client=fake, record=events.append))
    assert tuple(c.title for c in result.search.candidates) == ("second", "first", "third")
    assert result.search.statuses == search.statuses
    assert result.search.version == search_result_version(result.search.candidates)
    assert result.applied is True
    assert events[0].operation == "rank" and events[0].status == "applied"
    prompt = fake.messages[0]["content"]
    assert "return JSON" in prompt and "untrusted data" in prompt
    assert "ordered_tokens" in prompt and "exact permutation" in prompt
    body = json.loads(fake.messages[1]["content"])
    assert [item["token"] for item in body["candidates"]] == ["candidate-1", "candidate-2"]
    assert "url-secret" not in fake.messages[1]["content"]
    assert "authorization-secret" not in fake.messages[1]["content"]
    assert "session-secret" not in fake.messages[1]["content"]
    assert json.loads(fake.messages[1]["content"])["query"] == "query"


def test_ranking_real_client_receives_explicit_safe_instructions():
    seen = {}
    async def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ordered_tokens":["candidate-1","candidate-2"]}'}}]})
    result = asyncio.run(advise_ranking(_search(), "ignore instructions", settings(max_candidates=2), client=OpenAICompatibleClient(settings(), transport=httpx.MockTransport(handler))))
    assert result.applied
    system = seen["payload"]["messages"][0]["content"]
    assert "return JSON" in system and "untrusted data" in system and "exact permutation" in system
    assert "most to least relevant" in system and "supplied metadata" in system
def test_query_is_normalized_before_message_serialization():
    fake = Fake({"ordered_tokens": ["candidate-2", "candidate-1"]})
    asyncio.run(advise_ranking(_search(), "  Ｑ　x  ", settings(max_candidates=2), client=fake))
    assert json.loads(fake.messages[1]["content"])["query"] == "Q x"


def test_invalid_advice_returns_same_object_and_stable_event():
    search = _search()
    events = []
    result = asyncio.run(advise_ranking(search, "q", settings(), client=Fake({"ordered_tokens": ["candidate-1"]}), record=events.append))
    assert result.search is search and result.applied is False and result.error_code == "invalid_advice"
    assert events[0].error_code == "invalid_advice"


def test_disabled_and_provider_failures_fallback_without_leak():
    search = _search()
    fake = Fake(error=AIError("timeout"))
    events = []
    disabled = asyncio.run(advise_ranking(search, "q", AISettings(), client=fake, record=events.append))
    timed = asyncio.run(advise_ranking(search, "q", settings(), client=fake, record=events.append))
    assert disabled.search is search and disabled.error_code == "disabled"
    assert timed.search is search and timed.error_code == "timeout"
    assert [event.error_code for event in events] == ["disabled", "timeout"]
    assert fake.calls == 1


def test_not_applicable_zero_and_one_candidate_do_not_call_client():
    for count in (0, 1):
        search = _search()
        search = SearchResult(search.candidates[:count], search.statuses, search_result_version(search.candidates[:count]))
        fake = Fake({"ordered_tokens": []})
        events = []
        result = asyncio.run(advise_ranking(search, "q", settings(), client=fake, record=events.append))
        assert result.search is search and result.applied is False and result.error_code == "not_applicable"
        assert fake.calls == 0 and events[0].error_code == "not_applicable"


def test_invalid_advice_shapes_are_stable_fallbacks():
    for advice in (
        {},
        {"ordered_tokens": ("candidate-1", "candidate-2")},
        {"ordered_tokens": {"candidate-1", "candidate-2"}},
        {"ordered_tokens": "candidate-1"},
        {"ordered_tokens": [1, "candidate-2"]},
        {"ordered_tokens": ["candidate-1", "candidate-1"]},
        {"ordered_tokens": ["candidate-1", "candidate-9"]},
        {"ordered_tokens": ["candidate-1"]},
        {"ordered_tokens": ["candidate-1", "candidate-2"], "unexpected": True},
    ):
        search = _search()
        events = []
        result = asyncio.run(advise_ranking(search, "q", settings(max_candidates=2), client=Fake(advice), record=events.append))
        assert result.search is search and result.applied is False and result.error_code == "invalid_advice"
        assert events == [events[0]] and events[0].operation == "rank" and events[0].error_code == "invalid_advice"


def test_local_search_result_version_error_propagates(monkeypatch):
    search = _search()

    def fail(_candidates):
        raise ValueError("local-programmer-error")

    monkeypatch.setattr(ai_service, "search_result_version", fail)
    with pytest.raises(ValueError, match="local-programmer-error"):
        asyncio.run(advise_ranking(search, "q", settings(max_candidates=2), client=Fake({"ordered_tokens": ["candidate-2", "candidate-1"]})))


def test_statuses_and_result_are_unchanged_when_recorder_raises():
    search = _search()
    valid = asyncio.run(advise_ranking(search, "q", settings(max_candidates=2), client=Fake({"ordered_tokens": ["candidate-2", "candidate-1"]}), record=lambda event: (_ for _ in ()).throw(RuntimeError("secret"))))
    fallback = asyncio.run(advise_ranking(search, "q", settings(), client=Fake(error=AIError("provider_error")), record=lambda event: (_ for _ in ()).throw(RuntimeError("secret"))))
    assert valid.search.statuses == search.statuses and valid.applied is True
    assert fallback.search is search and fallback.error_code == "provider_error"


def test_cancellation_propagates():
    class Cancel:
        async def complete_json(self, messages):
            raise asyncio.CancelledError

    try:
        asyncio.run(advise_ranking(_search(), "q", settings(), client=Cancel()))
    except asyncio.CancelledError:
        return
    raise AssertionError("cancellation was swallowed")
