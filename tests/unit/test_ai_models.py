import asyncio
from dataclasses import FrozenInstanceError

import pytest

from musicdl.ai import (
    AICompletionClient,
    AIError,
    AIEvent,
    AILanguageResult,
    AIRankResult,
    emit_event,
)
from musicdl.media.models import Language
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult, SourceStatus


def test_public_contracts_are_frozen():
    event = AIEvent("rank", "applied")
    with pytest.raises(FrozenInstanceError):
        event.status = "fallback"
    result = AILanguageResult("未知", False, "disabled")
    with pytest.raises(FrozenInstanceError):
        result.applied = True


def test_ai_error_exposes_stable_code():
    assert AIError("timeout").code == "timeout"


def test_completion_client_protocol_accepts_small_async_fake():
    class Fake:
        async def complete_json(self, messages):
            return {"messages": messages}

    client: AICompletionClient = Fake()
    assert asyncio.run(client.complete_json([{"role": "user", "content": "hi"}]))


def test_emit_event_suppresses_recorder_errors():
    emit_event(lambda event: (_ for _ in ()).throw(RuntimeError("ignored")), AIEvent("language", "fallback"))


def test_emit_event_propagates_cancelled_error():
    def recorder(event):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        emit_event(recorder, AIEvent("rank", "fallback"))


def test_ai_event_contains_no_provider_sensitive_fields():
    event = AIEvent("rank", "fallback", "timeout")
    assert set(event.__dict__) == {"operation", "status", "error_code"}
    assert not hasattr(event, "api_key")
    assert not hasattr(event, "base_url")
    assert "provider.example" not in repr(event)


def test_rank_result_wraps_concrete_search_result():
    candidate = Candidate(source_id="source", source_version="1", item_id="item", title="Song", artist="Artist")
    search = SearchResult((candidate,), (SourceStatus("source", "1", "ok", 1),), "digest")
    result = AIRankResult(search, True)
    assert result.search is search
    assert result.applied is True
    assert result.error_code is None
    assert Language is not None
