"""The listing contract: a Bot that answers with numbers, then sends the file.

The shapes here are the ones measured against ``music_v1bot`` on 2026-09-21.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from musicdl.media.models import DownloadMetadata
from musicdl.sources.models import Candidate
from musicdl.telegram.decoder import (decode_bot_reply, decode_listing_item,
                                      decode_listing_message, decode_media_message,
                                      listing_item_id)
from musicdl.telegram.source import MAX_RECORDED_QUERY, TelegramBotSource

LISTING_TEXT = (
    "搜索源 网易云音乐，当前为第1页,搜索结果如下：\n"
    "1. 晴天(深情版) Lucky小爱\n"
    "2. 晴天 (原唱 周杰伦) RyaVocal\n"
    "3. 浪漫爱 江语晨\n"
    "8. 晴天(DJ版) DJ阿罗\n"
    "16. 只有标题"
)


class FakeButton:
    def __init__(self, text):
        self.text = text


class FakeRow:
    def __init__(self, *labels):
        self.buttons = tuple(FakeButton(label) for label in labels)


class FakeMarkup:
    def __init__(self, *rows):
        self.rows = tuple(FakeRow(*row) for row in rows)


class FakeDocument:
    def __init__(self, *, size=None, mime_type=None, file_name=None):
        self.size, self.mime_type, self.file_name = size, mime_type, file_name


class FakeMessage:
    def __init__(self, message_id=None, *, text=None, markup=None, document=None, audio=None):
        self.id, self.message, self.raw_text = message_id, text, text
        self.reply_markup, self.document, self.audio = markup, document, audio


def listing_message(text=LISTING_TEXT):
    return FakeMessage(900, text=text,
                       markup=FakeMarkup([str(n) for n in range(1, 9)],
                                         ["上一页", "下一页"],
                                         ["搜索源:", "网易云", "QQ音乐", "酷我", "酷狗"]))


def file_message(size=56_212_351, mime="audio/x-flac"):
    return FakeMessage(901, document=FakeDocument(size=size, mime_type=mime))


def test_a_numbered_listing_becomes_one_record_per_button():
    records = decode_listing_message(listing_message(), query="晴天")

    assert [record.item_id for record in records] == [
        "tg-list:1:晴天", "tg-list:2:晴天", "tg-list:3:晴天", "tg-list:8:晴天"]
    assert [(record.title, record.artist) for record in records] == [
        ("晴天(深情版)", "Lucky小爱"),
        ("晴天 (原唱 周杰伦)", "RyaVocal"),
        ("浪漫爱", "江语晨"),
        ("晴天(DJ版)", "DJ阿罗"),
    ]
    # The container is only known once the button has been pressed.
    assert all(record.format is None and record.size is None for record in records)


def test_an_entry_without_a_matching_button_is_not_offered():
    """``16. 只有标题`` has no ``16`` button, so it is not selectable here."""
    records = decode_listing_message(listing_message(), query="晴天")

    assert "只有标题" not in [record.title for record in records]


@pytest.mark.parametrize("text,markup", [
    # The numbers are printed but nothing selectable carries them.
    ("搜索源 网易云音乐，当前为第1页,搜索结果如下：\n1. 晴天 Lucky小爱", FakeMarkup()),
    ("没有找到相关歌曲", FakeMarkup(["1"])),
    ("", FakeMarkup(["1"])),
])
def test_a_reply_that_is_not_a_selectable_listing_is_refused(text, markup):
    message = FakeMessage(900, text=text, markup=markup)

    with pytest.raises(ValueError, match="invalid_bot_response"):
        decode_listing_message(message, query="晴天")


@pytest.mark.parametrize("line,expected", [
    ("1. 晴天(深情版) Lucky小爱", ("晴天(深情版)", "Lucky小爱")),
    ("1. 晴天 - 周杰伦", ("晴天", "周杰伦")),
    ("1、浪漫爱 江语晨", ("浪漫爱", "江语晨")),
    ("1) 只有标题", ("只有标题", "未知")),
])
def test_the_title_and_artist_are_split_without_inventing_either(line, expected):
    message = FakeMessage(1, text=line, markup=FakeMarkup(["1"]))

    (record,) = decode_listing_message(message)

    assert (record.title, record.artist) == expected


def test_the_query_is_carried_so_the_same_listing_can_be_replayed():
    item_id = listing_item_id("3", "晴天")

    assert item_id == "tg-list:3:晴天"
    assert decode_listing_item(item_id) == ("3", "晴天")
    assert decode_listing_item("tg-list:3") == ("3", None)


@pytest.mark.parametrize("value", ["telegram-77", "tg-list:", "tg-list:abc", "tg-list:0", "", None, 42])
def test_anything_that_is_not_a_listing_item_is_refused(value):
    with pytest.raises(ValueError, match="invalid_bot_response"):
        decode_listing_item(value)


def test_a_flac_file_is_named_flac_rather_than_x_dash_flac():
    (record,) = decode_media_message(file_message())

    assert (record.format, record.size) == ("flac", 56_212_351)


def test_either_contract_is_decoded_by_the_same_call():
    listed = decode_bot_reply(listing_message(), query="晴天")

    assert [record.item_id for record in listed][0] == "tg-list:1:晴天"
    assert [record.item_id for record in decode_bot_reply(file_message())] == ["telegram-901"]


class FakeFlow:
    """The connector's primitives, without Telegram."""

    def __init__(self, reply=None, selection=None, chunks=(), ready=True):
        self.reply, self.selection, self.chunks, self.ready_value = reply, selection, tuple(chunks), ready
        self.ask_calls, self.select_calls, self.streamed = [], [], []

    async def ask(self, bot_username, command, timeout):
        self.ask_calls.append((bot_username, command, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    async def select(self, bot_username, command, timeout, label=None):
        self.select_calls.append((bot_username, command, timeout, label))
        return self.selection

    async def stream(self, message, *, request_size=None):
        self.streamed.append(message)
        for chunk in self.chunks:
            yield chunk

    async def ready(self):
        return self.ready_value


async def collect(chunks):
    return [chunk async for chunk in chunks]


def build_source(flow, **overrides):
    values = dict(source_id="music_v1bot", source_version="music_v1bot", timeout=10.0)
    values.update(overrides)
    return TelegramBotSource("music_v1bot", flow, **values)


def test_a_search_returns_the_listing_as_candidates():
    flow = FakeFlow(reply=listing_message())
    source = build_source(flow)

    candidates = asyncio.run(source.search(" 晴天 "))

    assert flow.ask_calls == [("music_v1bot", "/search 晴天", 10.0)]
    assert [candidate.item_id for candidate in candidates] == [
        "tg-list:1:晴天", "tg-list:2:晴天", "tg-list:3:晴天", "tg-list:8:晴天"]
    assert candidates[0].source_id == "music_v1bot"
    assert candidates[0].title == "晴天(深情版)" and candidates[0].artist == "Lucky小爱"


def test_the_query_a_listing_records_is_the_query_that_was_sent():
    query = "歌" * 400
    flow = FakeFlow(reply=listing_message())
    source = build_source(flow)

    candidates = asyncio.run(source.search(query))

    assert flow.ask_calls == [("music_v1bot", "/search " + query[:MAX_RECORDED_QUERY], 10.0)]
    assert candidates[0].item_id == "tg-list:1:" + query[:MAX_RECORDED_QUERY]


def test_a_download_replays_the_search_and_presses_the_entry_button():
    flow = FakeFlow(reply=listing_message(), selection=file_message(),
                    chunks=[b"fLaC", b"\x00\x01"])
    source = build_source(flow)
    candidate = asyncio.run(source.search("晴天"))[1]

    metadata = asyncio.run(source.download(candidate))

    assert flow.select_calls == [("music_v1bot", "/search 晴天", 10.0, "2")]
    assert isinstance(metadata, DownloadMetadata)
    assert (metadata.extension, metadata.declared_size) == ("flac", 56_212_351)
    assert asyncio.run(collect(metadata.chunks)) == [b"fLaC", b"\x00\x01"]


def test_a_bot_that_sends_the_file_itself_is_downloaded_without_a_button():
    flow = FakeFlow(selection=file_message(size=1000, mime="audio/mpeg"), chunks=[b"ID3"])
    source = build_source(flow)
    candidate = Candidate(source_id="music_v1bot", source_version="music_v1bot",
                          item_id="telegram-77", title="晴天", artist="周杰伦")

    metadata = asyncio.run(source.download(candidate))

    assert flow.select_calls == [("music_v1bot", "/search 晴天", 10.0, None)]
    assert (metadata.extension, metadata.declared_size) == ("mp3", 1000)


def test_a_download_for_another_channel_is_refused():
    flow = FakeFlow(selection=file_message())
    source = build_source(flow)
    candidate = Candidate(source_id="kw", source_version="1", item_id="tg-list:1:晴天",
                          title="晴天", artist="未知")

    with pytest.raises(RuntimeError, match="candidate_identity"):
        asyncio.run(source.download(candidate))
    assert flow.select_calls == []


def test_a_failing_request_is_reported_without_its_message():
    flow = FakeFlow(reply=RuntimeError("entity music_v1bot and query 晴天 leaked"))
    source = build_source(flow)

    with pytest.raises(ValueError, match="bot_request_failed") as error:
        asyncio.run(source.search("晴天"))

    assert "music_v1bot" not in str(error.value) and "晴天" not in str(error.value)


def test_a_bot_that_was_not_understood_is_a_safe_code_not_a_guess():
    flow = FakeFlow(reply=FakeMessage(1, text="没有找到相关歌曲"))
    source = build_source(flow)

    with pytest.raises(ValueError, match="invalid_bot_response"):
        asyncio.run(source.search("晴天"))


def test_health_reads_the_account_behind_the_bot():
    assert asyncio.run(build_source(FakeFlow(ready=True)).health()) is True
    assert asyncio.run(build_source(FakeFlow(ready=False)).health()) is False


def test_a_bot_channel_asks_for_the_longer_budget_its_stream_needs():
    """Telegram hands over a file at ~0.5 MiB/s; a CDN's 15s is not enough."""
    source = build_source(FakeFlow())

    assert source.stream_budget_seconds > 600


def test_the_flow_of_a_direct_bot_is_not_asked_for_a_button():
    flow = FakeFlow(reply=file_message(), chunks=[b"fLaC"])
    source = build_source(flow)

    candidates = asyncio.run(source.search("晴天"))

    assert candidates[0].item_id == "telegram-901"
    assert candidates[0].format == "flac"
