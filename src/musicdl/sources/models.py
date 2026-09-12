from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    @field_validator("source_id", "source_version", "item_id", "title", "artist", "album", "format", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = normalize_text(str(value))
        return cleaned or None

    @property
    def canonical_version_key(self) -> tuple:
        """Cross-source identity for the same normalized recording/version."""
        return (self.title.casefold(), self.artist.casefold(), (self.album or "").casefold(), self.duration, (self.format or "").casefold(), self.bitrate)

    @property
    def public_representation(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
