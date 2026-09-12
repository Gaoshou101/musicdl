import asyncio

import pytest

from musicdl.sources.models import Candidate
from musicdl.telegram import (
    CustomTelegramBot,
    PublicTelegramBot,
    TelegramMediaRecord,
)


def run(coro):
    return asyncio.run(coro)


def complete_record(item_id="42"):
    return {
        "item_id": item_id,
        "title": "Hello World",
        "artist": "The Artist",
        "album": "The Album",
        "duration": 201,
        "bitrate": 320,
        "format": "mp3",
        "size": 123456,
    }


def test_public_bot_renders_command_and_maps_media_to_candidate():
    calls = []

    async def request(username, command, timeout):
        calls.append((username, command, timeout))
        return [complete_record()]

    bot = PublicTelegramBot("music_bot", request, source_id="tg-public", source_version="3")
    result = run(bot.search(" hello   world "))

    assert calls == [("music_bot", "/search hello world", 10.0)]
    assert result == [Candidate(
        source_id="tg-public", source_version="3", item_id="42",
        title="Hello World", artist="The Artist", album="The Album",
        duration=201, bitrate=320, format="mp3", size=123456,
    )]


def test_custom_bot_renders_template_and_maps_response():
    calls = []

    async def request(username, command, timeout):
        calls.append((username, command, timeout))
        return [TelegramMediaRecord(**complete_record())]

    bot = CustomTelegramBot(
        "custom_bot", request, command_template="/find {query}",
        source_id="tg-custom", source_version="1",
    )
    result = run(bot.search("jazz"))

    assert calls == [("custom_bot", "/find jazz", 10.0)]
    assert result[0].item_id == "42"
    assert result[0].source_id == "tg-custom"


@pytest.mark.parametrize("template", ["/find", "/find {query} {query}", "/find {other}", "/find {query!r}", "/find {query} extra {x}"])
def test_custom_bot_rejects_unsafe_templates(template):
    with pytest.raises(ValueError, match="invalid_command_template"):
        CustomTelegramBot("custom_bot", lambda *_: None, command_template=template, source_id="x", source_version="1")


@pytest.mark.parametrize("username", ["", "bot", "bad-name", "@music_bot", "music bot", "x" * 33, 42])
def test_bot_rejects_invalid_usernames(username):
    with pytest.raises(ValueError, match="invalid_bot_username"):
        PublicTelegramBot(username, lambda *_: None, source_id="x", source_version="1")


@pytest.mark.parametrize("query", ["", "   ", "x" * 257])
def test_bot_rejects_blank_or_oversized_queries(query):
    bot = PublicTelegramBot("music_bot", lambda *_: None, source_id="x", source_version="1")
    with pytest.raises(ValueError, match="invalid_query"):
        run(bot.search(query))


@pytest.mark.parametrize("response", ["bad", b"bad", 42, None, ["bad"], [{}], [{"item_id": "1", "title": "x", "artist": ""}]])
def test_bot_rejects_malformed_or_empty_media_response(response):
    async def request(*_):
        return response

    bot = PublicTelegramBot("music_bot", request, source_id="x", source_version="1")
    with pytest.raises(ValueError, match="invalid_bot_response"):
        run(bot.search("song"))


def test_bot_accepts_empty_result_as_no_candidates():
    async def request(*_):
        return []

    bot = PublicTelegramBot("music_bot", request, source_id="x", source_version="1")
    assert run(bot.search("song")) == []


def test_bot_enforces_result_bound():
    async def request(*_):
        return [complete_record(str(i)) for i in range(3)]

    bot = PublicTelegramBot("music_bot", request, source_id="x", source_version="1", max_results=2)
    with pytest.raises(ValueError, match="too_many_bot_results"):
        run(bot.search("song"))


def test_requester_exception_is_replaced_with_safe_error():
    async def request(*_):
        raise RuntimeError("secret query and response")

    bot = PublicTelegramBot("music_bot", request, source_id="x", source_version="1")
    with pytest.raises(ValueError, match="bot_request_failed") as error:
        run(bot.search("song"))
    assert "secret" not in str(error.value)
    assert "song" not in str(error.value)
