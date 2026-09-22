from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# A catalogue name is a short lowercase token (``kw``, ``wy``, ``tx``, ``kg``).
# It travels into the panel and into the bot's own lines, so it may only hold
# characters that read as a name wherever they land.
_PLATFORM_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


# An upstream that escapes its own text and then escapes it again on the way out
# leaves the escape itself inside the value.  Kuwo's search is the measured case
# (2026-09-22): its ``ARTIST`` field is a JavaScript object literal whose ``&``
# is written four backslashes deep, so the one JSON read that decodes the literal
# still leaves two of them in the artist.  A reader then got ``\u0026`` where the
# ``&`` belonged, in the WeCom listing and in the file name alike.  Text a person
# reads is resolved once, here, rather than by every reader of it.
_ESCAPE_SEQUENCE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|/)")
# Three layers is the deepest measured, and one pass is spent on each; a value
# that still spells one after that holds a backslash of its own.
_MAX_ESCAPE_PASSES = 4
_HEX4 = re.compile(r"^[0-9a-fA-F]{4}$")
_HEX2 = re.compile(r"^[0-9a-fA-F]{2}$")


def _unescape_once(value: str) -> str:
    """Resolve one layer of ``\\uXXXX``, ``\\xXX``, ``\\/`` and ``\\\\``.

    ``\\n`` and ``\\t`` are deliberately left alone: nothing separates a title
    holding a literal ``C:\\temp`` from one holding an escaped tab, and the
    escape that actually arrives here is the hexadecimal one.
    """
    out: list[str] = []
    index, length = 0, len(value)
    while index < length:
        char = value[index]
        if char != "\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        following = value[index + 1]
        if following in "\\/":
            out.append(following)
            index += 2
        elif following == "u" and _HEX4.match(value[index + 2:index + 6]):
            out.append(chr(int(value[index + 2:index + 6], 16)))
            index += 6
        elif following == "x" and _HEX2.match(value[index + 2:index + 4]):
            out.append(chr(int(value[index + 2:index + 4], 16)))
            index += 4
        else:
            out.append(char)
            index += 1
    try:
        # ``\ud83c\udfb5`` is one character, and half of such a pair cannot be
        # encoded at all -- so a value holding half of one keeps its escape
        # instead of becoming text nothing downstream can send.
        return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return value


def decode_escapes(value: str) -> str:
    """The text an upstream escaped, as the text it meant.

    Each pass resolves one layer and the loop stops where the value stops
    changing, so a value escaped twice over reads as its plain text and a value
    that only looks escaped -- ``AC\\DC`` -- comes back untouched.
    """
    for _ in range(_MAX_ESCAPE_PASSES):
        if _ESCAPE_SEQUENCE.search(value) is None:
            break
        decoded = _unescape_once(value)
        if decoded == value:
            break
        value = decoded
    return value


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=64)
    source_version: str = Field(min_length=1, max_length=64)
    item_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field(min_length=1, max_length=500)
    album: str | None = Field(default=None, max_length=500)
    duration: int | None = Field(default=None, ge=0, le=86400)
    bitrate: int | None = Field(default=None, ge=0, le=10000)
    format: str | None = Field(default=None, max_length=16)
    size: int | None = Field(default=None, ge=0, le=10**15)
    # Which catalogue answered for this recording, when the channel that
    # listed it did not answer out of an index of its own.  The supplied lx
    # sources all resolve against the same four catalogues, so a row that does
    # not say which one it came from is a row nothing can tell apart from the
    # same song on another one.  ``None`` when the channel produced it itself.
    platform: str | None = Field(default=None, max_length=16)

    @field_validator("source_id", "source_version", "item_id", "format", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        """The fields that are identity rather than prose, and stay as sent.

        ``item_id`` is what a source is later asked to resolve, so rewriting it
        would name something the source never listed.
        """
        if value is None:
            return None
        cleaned = normalize_text(str(value))
        return cleaned or None

    @field_validator("title", "artist", "album", mode="before")
    @classmethod
    def clean_display_text(cls, value: Any) -> Any:
        """The three fields a person reads, with an upstream's escaping resolved."""
        if value is None:
            return None
        cleaned = normalize_text(decode_escapes(str(value)))
        return cleaned or None

    @field_validator("platform", mode="before")
    @classmethod
    def clean_platform(cls, value: Any) -> Any:
        """One catalogue name, or a refusal -- never a repaired guess."""
        if value is None:
            return None
        cleaned = normalize_text(str(value)).casefold()
        if not cleaned:
            return None
        if _PLATFORM_TOKEN.fullmatch(cleaned) is None:
            raise ValueError("invalid platform")
        return cleaned

    @property
    def canonical_version_key(self) -> tuple:
        """Cross-source identity for the same normalized recording/version.

        The catalogue is part of the identity.  Two platforms listing the same
        title, artist and duration are two different files behind two different
        resolvers, and collapsing them would hide one of the two the operator
        could have downloaded.  Two channels listing the *same* catalogue entry
        still collapse, which is what keeps one song from appearing once per
        installed source.
        """
        return (self.title.casefold(), self.artist.casefold(), (self.album or "").casefold(), self.duration, (self.format or "").casefold(), self.bitrate, (self.platform or "").casefold())

    @property
    def public_representation(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
