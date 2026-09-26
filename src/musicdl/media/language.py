"""Deterministic four-category language classification for the media library."""

from __future__ import annotations

import asyncio
import inspect
from typing import Final

from .models import LANGUAGES, Language

# Each category is decided by script evidence, and the order is the precedence:
# kana and hangul are unambiguously 日韩, han characters are 华语, and Latin
# letters are 欧美. A script that is none of these leaves the category 未知.
_SCRIPTS: Final[tuple[tuple[Language, tuple[tuple[int, int], ...]], ...]] = (
    ("日韩", ((0x1100, 0x11FF), (0x3040, 0x30FF), (0x3130, 0x318F), (0x31F0, 0x31FF), (0xAC00, 0xD7AF))),
    ("华语", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))),
    ("欧美", ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F), (0x1E00, 0x1EFF))),
)


def _shows(text: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(any(low <= ord(character) <= high for low, high in ranges) for character in text)


def _category(text: str) -> Language | None:
    for language, ranges in _SCRIPTS:
        if _shows(text, ranges):
            return language
    return None


def classify_language(title: str | None, artist: str | None = None, album: str | None = None) -> Language:
    """Infer the category directory from metadata alone, deterministically.

    The title is consulted first and the remaining metadata only as a backup, so
    a Latin song title by a han-character artist stays 欧美. Only script
    evidence is used, which means a title written purely in han characters is
    classified as 华语: a kana-free Japanese title cannot be told apart from a
    Chinese one by metadata, and that ambiguity is exactly what the advisory
    classifier is for. The result is always one of the four accepted values.
    """
    for text in (str(title or ""), " ".join(str(part or "") for part in (artist, album))):
        decided = _category(text)
        if decided is not None:
            return decided
    return "未知"


async def resolve_language(candidate, advisor=None) -> Language:
    """Resolve one candidate's category with deterministic fallback semantics."""
    baseline = classify_language(candidate.title, candidate.artist, candidate.album)
    if advisor is None:
        return baseline
    try:
        advised = advisor(candidate)
        if inspect.isawaitable(advised):
            advised = await advised
    except asyncio.CancelledError:
        raise
    except Exception:
        return baseline
    return advised if isinstance(advised, str) and advised in LANGUAGES else baseline
