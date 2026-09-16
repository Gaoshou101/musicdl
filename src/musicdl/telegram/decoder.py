"""Default decoding of what a Telegram music Bot replies with.

``TelegramConnector.bot_requester`` deliberately leaves decoding to its caller,
because the reply format belongs to the Bot. This module supplies the decoder
the runtime uses: it assumes the shape this project already documents for a
Telegram music Bot, namely that the Bot answers a search command by sending the
audio file, so the candidate metadata is the file's own metadata.

A Bot that answers with a *text list* of results is a different contract and is
not guessed at here; it needs its own decoder. That limit is recorded in the
README rather than smoothed over.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .bots import TelegramMediaRecord

UNKNOWN_TAG = "未知"


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _count(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _first(*values: Any) -> Any | None:
    for value in values:
        if value is not None:
            return value
    return None


def _document_of(message: Any) -> Any | None:
    """Return the file a media message carries.

    ``message.document`` is the Telegram ``Document`` and is the only place the
    size and MIME type live; ``message.audio`` is the tag attribute Telethon
    derives from it, so it is the fallback rather than the first choice.
    """
    return _first(getattr(message, "document", None), getattr(message, "audio", None),
                  getattr(message, "voice", None))


def _extension(message: Any, document: Any) -> str | None:
    """Prefer the real file name, then the MIME subtype; never invent one."""
    name = _text(getattr(document, "file_name", None)) or _text(getattr(message, "file_name", None))
    if name and "." in name:
        suffix = name.rsplit(".", 1)[1]
        if 1 <= len(suffix) <= 8 and suffix.isalnum():
            return suffix.lower()
    mime = _text(getattr(document, "mime_type", None))
    if mime and "/" in mime:
        return mime.split("/", 1)[1].split(";")[0].strip().lower() or None
    return None


def decode_media_message(response: Any) -> list[TelegramMediaRecord]:
    """Turn one Bot reply, or a sequence of replies, into media records.

    Raises ``ValueError("invalid_bot_response")`` for anything that is not a
    file reply, which is the stable code the Bot adapters already propagate.
    """
    if isinstance(response, (str, bytes, bytearray)) or not isinstance(response, Sequence):
        messages: Sequence[Any] = (response,)
    else:
        messages = response
    records: list[TelegramMediaRecord] = []
    for message in messages:
        document = _document_of(message)
        message_id = getattr(message, "id", None)
        if document is None or not isinstance(message_id, int) or isinstance(message_id, bool):
            raise ValueError("invalid_bot_response")
        # Title, performer, and duration are tags; Telethon reads them off the
        # document, and `message.audio` is the convenience accessor for them.
        tags = _first(getattr(message, "audio", None), getattr(message, "voice", None), document)
        file_name = _first(_text(getattr(document, "file_name", None)),
                           _text(getattr(message, "file_name", None)))
        records.append(TelegramMediaRecord(
            item_id=f"telegram-{message_id}",
            title=_first(_text(getattr(tags, "title", None)),
                         _text(getattr(document, "title", None)), file_name) or UNKNOWN_TAG,
            artist=_first(_text(getattr(tags, "performer", None)),
                          _text(getattr(tags, "artist", None)),
                          _text(getattr(document, "performer", None)), UNKNOWN_TAG),
            album=_text(getattr(tags, "album", None)) or _text(getattr(document, "album", None)),
            duration=_first(_count(getattr(tags, "duration", None)),
                            _count(getattr(document, "duration", None))),
            bitrate=None,
            format=_extension(message, document),
            size=_count(getattr(document, "size", None)),
        ))
    if not records:
        raise ValueError("invalid_bot_response")
    return records
