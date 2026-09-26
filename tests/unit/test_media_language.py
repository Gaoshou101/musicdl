"""Script evidence decides the media category directory."""

from __future__ import annotations

import asyncio

import pytest

from musicdl.media import LANGUAGES, classify_language
from musicdl.sources.models import Candidate


@pytest.mark.parametrize("title,artist,album,expected", [
    ("七里香", "周杰伦", None, "华语"),
    ("月亮代表我的心", "邓丽君", "岛国之情歌", "华语"),
    ("残酷な天使のテーゼ", "高橋洋子", None, "日韩"),
    ("봄날", "방탄소년단", None, "日韩"),
    ("Song", "Artist", None, "欧美"),
    ("Café", "Édith Piaf", None, "欧美"),
    ("Ярослав", "Мельница", None, "未知"),
    ("", None, None, "未知"),
    ("🎵", "🎤", None, "未知"),
])
def test_classification_uses_script_evidence(title, artist, album, expected):
    assert classify_language(title, artist, album) == expected


def test_the_title_outranks_the_rest_of_the_metadata():
    assert classify_language("Dynamite", "防弹少年团") == "欧美"
    assert classify_language("七里香", "Jay Chou") == "华语"


def test_classification_is_stable_and_always_inside_the_accepted_set():
    parts = ("残酷な天使のテーゼ", "高橋洋子", "Neon Genesis")
    verdicts = {classify_language(*parts) for _ in range(5)}
    assert verdicts == {"日韩"} and verdicts <= LANGUAGES


def _candidate() -> Candidate:
    return Candidate(source_id="primary", source_version="1.0.0", item_id="1",
                     title="稻香", artist="周杰伦", album="魔杰座", format="mp3")


def test_resolve_language_without_advisor_uses_the_deterministic_baseline():
    # Keep this import local so missing shared behavior does not hide the
    # panel-route regression during collection.
    from musicdl.media.language import resolve_language

    assert asyncio.run(resolve_language(_candidate())) == "华语"


def test_resolve_language_accepts_a_sync_advisor():
    from musicdl.media.language import resolve_language

    assert asyncio.run(resolve_language(_candidate(), lambda candidate: "日韩")) == "日韩"


def test_resolve_language_accepts_an_async_advisor():
    from musicdl.media.language import resolve_language

    async def advisor(candidate):
        return "欧美"

    assert asyncio.run(resolve_language(_candidate(), advisor)) == "欧美"


@pytest.mark.parametrize("answer", ["Unknown", "zh", [], {}], ids=["unknown", "zh", "list", "dict"])
def test_resolve_language_falls_back_for_invalid_or_unhashable_advice(answer):
    from musicdl.media.language import resolve_language

    async def advisor(candidate):
        return answer

    assert asyncio.run(resolve_language(_candidate(), advisor)) == "华语"


@pytest.mark.parametrize("failure", [RuntimeError("provider"), TimeoutError("timeout")],
                         ids=["error", "timeout"])
def test_resolve_language_falls_back_when_advisor_fails(failure):
    from musicdl.media.language import resolve_language

    async def advisor(candidate):
        raise failure

    assert asyncio.run(resolve_language(_candidate(), advisor)) == "华语"


def test_resolve_language_propagates_cancellation_from_advisor():
    from musicdl.media.language import resolve_language

    async def advisor(candidate):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(resolve_language(_candidate(), advisor))
