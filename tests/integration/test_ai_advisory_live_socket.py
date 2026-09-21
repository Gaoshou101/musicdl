"""Advisory AI over a real socket: loopback provider, production client, emitted events.

The unit tests inject ``httpx.MockTransport``, which exercises the client's parsing and
fallback logic without ever opening a connection.  This slice keeps every layer real - a
living loopback HTTP server, the production ``OpenAICompatibleClient`` with its own
connection handling, headers and payload, the advisory service, and the events it emits -
so a change to the wire contract or the fallback boundary fails here rather than in a
deployment.  Nothing reaches the public network: the provider binds ``127.0.0.1`` on an
ephemeral port, which is also why this file carries no ``integration`` marker.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from pydantic import SecretStr

from musicdl.ai import AIEvent, OpenAICompatibleClient, advise_language, advise_ranking
from musicdl.config import AISettings
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult, SourceStatus, search_result_version

KEY = "live-socket-secret"
QUERY = "晴天 周杰伦"

# (title, artist, album, duration, bitrate, format, source_id, item_id)
ROWS = (
    ("晴天", "周杰伦", "叶惠美", 269, 1411, "flac", "tx", "item-1"),
    ("晴天", "周杰伦", None, 268, 320, "mp3", "wy", "item-2"),
    ("晴天", "沈以诚", "翻唱集", 240, 128, "mp3", "kg", "item-3"),
)
OFFERS = (("tx", "wy"), ("wy",), ("kg",))


class LiveProvider:
    """An OpenAI-compatible endpoint on loopback that records what it was asked."""

    def __init__(self) -> None:
        self.mode = "good"
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def answer(self, body: dict[str, Any]) -> tuple[int, bytes]:
        if self.mode == "error":
            return 500, json.dumps({"error": {"message": KEY}}).encode()
        prompt = json.loads(body["messages"][-1]["content"])
        if "candidates" in prompt:
            tokens = [row["token"] for row in prompt["candidates"]]
            advice: dict[str, Any] = {"ordered_tokens":
                                      ["candidate-99"] if self.mode == "garbage" else list(reversed(tokens))}
        else:
            advice = {"language": "火星语" if self.mode == "garbage" else "日韩"}
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(advice)}}]}).encode()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                """Keep the provider quiet: the test output is the assertion."""

            def do_POST(self) -> None:  # noqa: N802 - the name is the HTTP verb
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                provider.requests.append({"path": self.path,
                                          "authorization": self.headers.get("authorization"),
                                          "body": body})
                status, payload = provider.answer(body)
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler


@pytest.fixture()
def provider():
    live = LiveProvider()
    try:
        yield live
    finally:
        live.close()


@pytest.fixture()
def settings(provider: LiveProvider) -> AISettings:
    return AISettings(enabled=True, base_url=provider.base_url, api_key=SecretStr(KEY),
                      model="live-model", timeout=5.0, max_candidates=3)


def search() -> SearchResult:
    candidates = tuple(
        Candidate(source_id=row[6], source_version="1.0.0", item_id=row[7], title=row[0],
                  artist=row[1], album=row[2], duration=row[3], bitrate=row[4], format=row[5])
        for row in ROWS
    )
    statuses = (SourceStatus("tx", "1.0.0", "ok", 2), SourceStatus("wy", "1.0.0", "ok", 2))
    return SearchResult(candidates, statuses, search_result_version(candidates), OFFERS)


def items(result: SearchResult) -> list[str]:
    return [candidate.item_id for candidate in result.candidates]


def test_ranking_over_a_live_socket_reorders_the_rows_and_carries_their_offers(
        provider: LiveProvider, settings: AISettings) -> None:
    events: list[AIEvent] = []
    ranked = asyncio.run(advise_ranking(search(), QUERY, settings,
                                        client=OpenAICompatibleClient(settings), record=events.append))

    assert ranked.applied is True and ranked.error_code is None
    assert events == [AIEvent("rank", "applied")]
    assert items(ranked.search) == ["item-3", "item-2", "item-1"]
    # A reordered row keeps the channels that stand behind it, by identity.
    assert ranked.search.offers == (("kg",), ("wy",), ("tx", "wy"))
    assert ranked.search.version != search().version
    assert ranked.search.statuses == search().statuses
    assert len(provider.requests) == 1


def test_the_wire_carries_the_documented_request_and_never_the_key(
        provider: LiveProvider, settings: AISettings) -> None:
    asyncio.run(advise_ranking(search(), QUERY, settings, client=OpenAICompatibleClient(settings)))

    sent = provider.requests[-1]
    body = sent["body"]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "live-model"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False
    assert body["messages"][0]["role"] == "system"
    assert "untrusted" in body["messages"][0]["content"]
    # The query and the metadata travel as data, and the secret never travels at all.
    assert KEY not in json.dumps(body["messages"], ensure_ascii=False)
    disclosed = json.loads(body["messages"][1]["content"])
    assert disclosed["query"] == QUERY
    assert [row["token"] for row in disclosed["candidates"]] == ["candidate-1", "candidate-2", "candidate-3"]


def test_a_provider_error_falls_back_and_keeps_the_deterministic_order(
        provider: LiveProvider, settings: AISettings) -> None:
    provider.mode = "error"
    events: list[AIEvent] = []
    original = search()
    ranked = asyncio.run(advise_ranking(original, QUERY, settings,
                                        client=OpenAICompatibleClient(settings), record=events.append))

    assert ranked.applied is False and ranked.error_code == "provider_error"
    assert events == [AIEvent("rank", "fallback", "provider_error")]
    assert items(ranked.search) == ["item-1", "item-2", "item-3"]
    # A fallback hands back the deterministic result itself, not a copy of it.
    assert ranked.search is original
    # The provider's own words never reach the caller.
    assert KEY not in repr(ranked)


def test_invalid_advice_falls_back_for_both_operations(provider: LiveProvider, settings: AISettings) -> None:
    provider.mode = "garbage"
    events: list[AIEvent] = []
    ranked = asyncio.run(advise_ranking(search(), QUERY, settings,
                                        client=OpenAICompatibleClient(settings), record=events.append))
    advised = asyncio.run(advise_language(search().candidates[0], "华语", settings,
                                          client=OpenAICompatibleClient(settings), record=events.append))

    assert ranked.error_code == "invalid_advice" and ranked.applied is False
    assert advised.error_code == "invalid_advice" and advised.applied is False
    assert advised.language == "华语"
    assert events == [AIEvent("rank", "fallback", "invalid_advice"),
                      AIEvent("language", "fallback", "invalid_advice")]
    assert items(ranked.search) == ["item-1", "item-2", "item-3"]


def test_language_advice_over_a_live_socket_applies(provider: LiveProvider, settings: AISettings) -> None:
    events: list[AIEvent] = []
    advised = asyncio.run(advise_language(search().candidates[1], "华语", settings,
                                          client=OpenAICompatibleClient(settings), record=events.append))

    assert advised.applied is True and advised.language == "日韩"
    assert events == [AIEvent("language", "applied")]
    sent = json.loads(provider.requests[-1]["body"]["messages"][-1]["content"])
    assert sent["title"] == "晴天" and sent["artist"] == "周杰伦"


def test_disabled_settings_never_open_a_connection(provider: LiveProvider) -> None:
    disabled = AISettings()
    events: list[AIEvent] = []
    ranked = asyncio.run(advise_ranking(search(), QUERY, disabled,
                                        client=OpenAICompatibleClient(disabled), record=events.append))
    advised = asyncio.run(advise_language(search().candidates[0], None, disabled,
                                          client=OpenAICompatibleClient(disabled), record=events.append))

    assert ranked.error_code == "disabled" and advised.error_code == "disabled"
    assert advised.language == "未知"
    assert provider.requests == []
