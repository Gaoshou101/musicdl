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

    @field_validator("source_id", "source_version", "item_id", "title", "artist", "album", "format", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = normalize_text(str(value))
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
