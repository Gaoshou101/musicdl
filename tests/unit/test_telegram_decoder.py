"""The default decoder reads the media metadata off the file a Bot sends."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from musicdl.telegram.bots import PublicTelegramBot
from musicdl.telegram.connector import TelegramConnector
from musicdl.telegram.decoder import decode_media_message


class FakeDocument:
    """The subset of Telethon's ``Document`` the decoder is allowed to read."""

    def __init__(self, *, size: int | None = None, mime_type: str | None = None,
                 file_name: str | None = None, title: str | None = None,
                 performer: str | None = None, duration: int | None = None):
        self.size, self.mime_type, self.file_name = size, mime_type, file_name
        self.title, self.performer, self.duration = title, performer, duration


class FakeMessage:
    """The subset of Telethon's ``Message`` the decoder is allowed to read."""

    def __init__(self, message_id=None, *, document=None, audio=None, file_name=None):
        self.id, self.document, self.audio, self.file_name = message_id, document, audio, file_name


def test_audio_tags_and_the_document_size_become_one_record():
    document = FakeDocument(size=4_194_304, mime_type="audio/mpeg", file_name="song.mp3")
    audio = SimpleNamespace(title="Song", performer="Artist", duration=215)

    (record,) = decode_media_message(FakeMessage(77, document=document, audio=audio))

    assert record.item_id == "telegram-77"
    assert (record.title, record.artist, record.duration) == ("Song", "Artist", 215)
    assert (record.size, record.format) == (4_194_304, "mp3")
    assert record.bitrate is None


def test_missing_tags_fall_back_to_the_file_name_and_an_unknown_placeholder():
    document = FakeDocument(size=1000, mime_type="audio/ogg; codecs=opus", file_name="track.ogg")

    (record,) = decode_media_message(FakeMessage(1, document=document))

    assert record.title == "track.ogg"
    assert record.artist == "未知"
    assert record.format == "ogg"


def test_the_mime_subtype_is_used_only_when_there_is_no_file_name():
    document = FakeDocument(size=10, mime_type="audio/flac")

    (record,) = decode_media_message(FakeMessage(2, document=document))

    assert record.format == "flac"
    assert record.title == "未知"


@pytest.mark.parametrize("message", [
    FakeMessage(3),                                        # no media at all
    FakeMessage(None, document=FakeDocument(size=1)),       # no message id to key on
    FakeMessage("not-an-id", document=FakeDocument(size=1)),
    SimpleNamespace(),                                      # not even message shaped
])
def test_anything_that_is_not_a_file_reply_is_an_invalid_bot_response(message):
    with pytest.raises(ValueError, match="invalid_bot_response"):
        decode_media_message(message)


def test_an_empty_sequence_is_an_invalid_bot_response():
    with pytest.raises(ValueError, match="invalid_bot_response"):
        decode_media_message([])


def test_a_sequence_of_replies_becomes_one_record_each():
    records = decode_media_message([
        FakeMessage(10, document=FakeDocument(size=1, file_name="a.mp3")),
        FakeMessage(11, document=FakeDocument(size=2, file_name="b.mp3")),
    ])

    assert [record.item_id for record in records] == ["telegram-10", "telegram-11"]
    assert [record.size for record in records] == [1, 2]


def test_a_negative_size_is_dropped_rather_than_reported():
    (record,) = decode_media_message(FakeMessage(12, document=FakeDocument(size=-5, mime_type="audio/mpeg")))

    assert record.size is None


class FakeConversation:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_message(self, command):
        self.command = command

    async def get_response(self):
        return self.message


class FakeClient:
    def __init__(self, message):
        self.message = message
        self.sent = []

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    def conversation(self, bot_username, timeout):
        self.sent.append((bot_username, timeout))
        return FakeConversation(self.message)


def test_the_connector_decoder_and_adapter_run_a_search_end_to_end(tmp_path):
    """No Telethon: the real connector, the real decoder, the real adapter."""
    message = FakeMessage(42, document=FakeDocument(size=9, mime_type="audio/mpeg", file_name="hit.mp3"),
                          audio=SimpleNamespace(title="Hit", performer="Band", duration=180))
    client = FakeClient(message)
    connector = TelegramConnector(tmp_path / "sessions", 7, "hash", lambda path: client)
    bot = PublicTelegramBot("MusicBot", connector.bot_requester("default", decode_media_message),
                            source_id="tg", source_version="MusicBot", timeout=12.5)

    candidates = asyncio.run(bot.search("  hit song  "))

    assert client.sent == [("MusicBot", 12.5)]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert (candidate.source_id, candidate.source_version) == ("tg", "MusicBot")
    assert (candidate.title, candidate.artist, candidate.size, candidate.format) == (
        "Hit", "Band", 9, "mp3")
