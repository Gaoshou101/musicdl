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
from musicdl.sources.search import SearchResult


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


def test_contract_annotations_are_importable():
    assert Language is not None
    assert SearchResult is not None
    assert AIRankResult is not None
