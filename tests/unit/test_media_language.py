"""Script evidence decides the media category directory."""

from __future__ import annotations

import pytest

from musicdl.media import LANGUAGES, classify_language


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
