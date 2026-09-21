"""Default decoding of what a Telegram music Bot replies with.

``TelegramConnector.bot_requester`` deliberately leaves decoding to its caller,
because the reply format belongs to the Bot. This module supplies the decoders
the runtime uses, one per contract a Bot speaks:

* ``decode_media_message`` -- the Bot answers a search command by sending the
  audio file, so the candidate metadata is the file's own metadata.
* ``decode_listing_message`` -- the Bot answers with a numbered text list whose
  inline buttons fetch one entry.  The listing names the title and the artist;
  which container an entry is in is only known once the button has been
  pressed, so its ``format`` stays ``None`` and the download reads the
  container off the bytes it receives.
* ``decode_bot_reply`` -- whichever of the two arrived, for a caller that does
  not already know which Bot it is talking to.

The listing contract was measured on 2026-09-21 against ``music_v1bot``: the
reply is ``搜索源 网易云音乐，当前为第1页,搜索结果如下：`` followed by
``1. 晴天(深情版) Lucky小爱`` and an inline keyboard of ``1``..``8``, and
pressing a number answers with the audio document.  A number sent back as text
is treated by that Bot as a new search, which is why the button -- and not a
reply -- is what this decoder records.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from .bots import TelegramMediaRecord

UNKNOWN_TAG = "未知"
#: Prefix of the ``item_id`` a listing entry carries: the label of the inline
#: button that fetches it, then the query behind the listing when it is known.
LISTING_ITEM_PREFIX = "tg-list:"

#: ``1. 歌名 歌手``, ``2、歌名 歌手`` and ``3) 歌名 歌手`` are all in the wild.
_LISTING_LINE = re.compile(r"^\s*(\d{1,3})\s*[.、)：:)]\s*(\S.*?)\s*$")
_DASHES = (" - ", " – ", " — ", "－")
_MAX_LINE = 300
_MAX_ARTIST = 60
#: Telegram labels a FLAC file ``audio/x-flac``; the project's own extension
#: vocabulary is the plain container name.  A subtype in neither table is
#: reported as unknown rather than guessed at.
_MIME_SUBTYPES = {"mpeg": "mp3", "mp4": "m4a", "x-m4a": "m4a", "x-flac": "flac",
                  "x-aac": "aac", "x-ogg": "ogg", "vorbis": "ogg"}
KNOWN_EXTENSIONS = frozenset({"mp3", "m4a", "ogg", "aac", "flac"})


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
            return _known(suffix.lower())
    mime = _text(getattr(document, "mime_type", None))
    if mime and "/" in mime:
        return _known(mime.split("/", 1)[1].split(";")[0].strip().lower())
    return None


def _known(subtype: str | None) -> str | None:
    """One container name the media pipeline can publish, or nothing."""
    if not subtype:
        return None
    mapped = _MIME_SUBTYPES.get(subtype, subtype)
    return mapped if mapped in KNOWN_EXTENSIONS else None


def extension_of(message: Any) -> str | None:
    """The container one file reply announces, as a bare extension."""
    document = _document_of(message)
    return None if document is None else _extension(message, document)


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


def _labels(message: Any) -> list[str]:
    """Every inline button label on one reply, in the order they are shown."""
    rows = getattr(getattr(message, "reply_markup", None), "rows", None) or ()
    labels: list[str] = []
    for row in rows:
        for button in getattr(row, "buttons", None) or ():
            text = _text(getattr(button, "text", None))
            if text is not None:
                labels.append(text)
    return labels


def _split_listing_line(line: str) -> tuple[str, str]:
    """One listing line as ``(title, artist)``.

    These Bots print ``歌名 歌手``, and some print ``歌名 - 歌手``.  The last
    whitespace-separated token is read as the artist when there is more than
    one token and it still looks like a name; a line that is only a title is
    kept whole rather than split in half to invent an artist.
    """
    for dash in _DASHES:
        if dash in line:
            head, _, tail = line.partition(dash)
            if head.strip() and tail.strip():
                return head.strip(), tail.strip()
    head, _, tail = line.rpartition(" ")
    if head.strip() and tail.strip() and len(tail) <= _MAX_ARTIST:
        return head.strip(), tail.strip()
    return line, UNKNOWN_TAG


def listing_item_id(label: Any, query: str | None = None) -> str:
    """The ``item_id`` of one listing entry.

    It carries the button that fetches the entry and, when the caller knows it,
    the query the listing answered: the buttons are numbered per listing, so a
    different query can order the same catalogue differently and pressing
    ``1`` again would fetch a different song.
    """
    cleaned = _text(str(label))
    if cleaned is None or not cleaned.isdigit() or not 1 <= int(cleaned) <= 999:
        raise ValueError("invalid_bot_response")
    if query is None:
        return f"{LISTING_ITEM_PREFIX}{cleaned}"
    return f"{LISTING_ITEM_PREFIX}{cleaned}:{_text(query) or ''}"


def decode_listing_item(item_id: Any) -> tuple[str, str | None]:
    """``(button label, query)`` behind an item one listing produced."""
    if not isinstance(item_id, str) or not item_id.startswith(LISTING_ITEM_PREFIX):
        raise ValueError("invalid_bot_response")
    label, separator, query = item_id[len(LISTING_ITEM_PREFIX):].partition(":")
    cleaned = _text(label)
    if cleaned is None or not cleaned.isdigit() or not 1 <= int(cleaned) <= 999:
        raise ValueError("invalid_bot_response")
    return cleaned, (_text(query) if separator else None)


def decode_listing_message(response: Any, *, query: str | None = None,
                           max_records: int = 100) -> list[TelegramMediaRecord]:
    """Turn a numbered Bot listing into one record per selectable entry.

    Only entries that carry a matching inline button are recorded: a Bot that
    merely prints numbers and expects the reader to type one back is a
    different contract, and ``music_v1bot`` treats such a reply as a fresh
    search rather than as a choice.  Raises ``invalid_bot_response`` when the
    reply is not a listing at all.
    """
    if isinstance(response, (str, bytes, bytearray)) or not isinstance(response, Sequence):
        messages: Sequence[Any] = (response,)
    else:
        messages = response
    records: list[TelegramMediaRecord] = []
    for message in messages:
        text = _text(getattr(message, "message", None)) or _text(getattr(message, "raw_text", None))
        if text is None:
            continue
        available = _labels(message)
        for line in text.splitlines():
            match = _LISTING_LINE.match(line)
            if match is None:
                continue
            number, body = match.group(1), match.group(2)
            if not body or len(body) > _MAX_LINE or number not in available:
                continue
            title, artist = _split_listing_line(body)
            records.append(TelegramMediaRecord(item_id=listing_item_id(number, query),
                                               title=title, artist=artist))
    if not records:
        raise ValueError("invalid_bot_response")
    if len(records) > max_records:
        raise ValueError("too_many_bot_results")
    return records


def decode_bot_reply(response: Any, *, query: str | None = None,
                     max_records: int = 100) -> list[TelegramMediaRecord]:
    """Whatever this Bot answers with, decoded as one list of records."""
    try:
        return decode_listing_message(response, query=query, max_records=max_records)
    except ValueError as listing_error:
        if str(listing_error) == "too_many_bot_results":
            raise
        try:
            return decode_media_message(response)
        except ValueError:
            raise listing_error from None
