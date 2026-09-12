import re

import pytest

from musicdl.sources.models import Candidate
from musicdl.wecom.results import format_results


def test_format_results_contains_distinguishing_fields_and_iec_size():
    text = format_results([Candidate(source_id="qq", source_version="2.1", item_id="x", title="Song", artist="Artist", album="Album", duration=65, format="mp3", bitrate=320, size=1536)])
    assert "1. Song - Artist" in text
    assert "qq@2.1" in text and "Album" in text and "1m 5s" in text
    assert "MP3" in text.upper() and "320 kbps" in text and "1.5 KiB" in text


def test_format_results_requires_positive_bounded_max_items():
    c = Candidate(source_id="q", source_version="1", item_id="x", title="T", artist="A")
    with pytest.raises(ValueError): format_results([c], max_items=0)
    with pytest.raises(ValueError): format_results([c], max_items=101)

def test_format_results_is_utf8_bounded_and_keeps_required_labels():
    c = Candidate(source_id="qq", source_version="2.1", item_id="x", title="歌" * 500, artist="艺" * 500, format="flac", size=1536)
    text = format_results([c], max_bytes=2048)
    assert len(text.encode("utf-8")) <= 2048
    assert "1. " in text and "qq@2.1" in text and "格式" in text and "大小" in text
    assert "\ufffd" not in text

def test_format_results_rejects_invalid_byte_limit_and_empty_input():
    assert format_results([], max_bytes=128) == ""
    with pytest.raises(ValueError): format_results([], max_bytes=127)
    with pytest.raises(ValueError): format_results([], max_bytes=2049)

def test_format_results_long_required_and_optional_fields_keeps_first_line():
    candidates = [Candidate(source_id="源" * 64, source_version="版" * 64, item_id=str(i), title="歌" * 500, artist="艺" * 500, album="专辑" * 250, duration=86400, bitrate=10000, format="格式" * 8, size=1536) for i in range(4)]
    text = format_results(candidates)
    assert len(text.encode("utf-8")) <= 2048
    assert text.splitlines()
    for index, line in enumerate(text.splitlines(), 1):
        assert line.startswith(f"{index}. ")
        assert " - " in line and "@" in line and "格式" in line and "大小" in line
        assert "\ufffd" not in line

def test_format_results_small_budget_still_emits_complete_normal_line():
    c = Candidate(source_id="q", source_version="1", item_id="x", title="歌", artist="艺", size=1)
    text = format_results([c], max_bytes=128)
    assert len(text.encode("utf-8")) <= 128
    assert "1. 歌 - 艺" in text and "q@1" in text and "格式 未知" in text and "大小 1 B" in text

def test_format_results_small_budget_truncates_long_source_and_version():
    c = Candidate(source_id="源" * 64, source_version="版" * 64, item_id="x", title="歌" * 20, artist="艺" * 20, format="flac", size=1)
    text = format_results([c], max_bytes=128)
    assert text and len(text.encode("utf-8")) <= 128
    match = re.fullmatch(r"1\. (?P<title>.+) - (?P<artist>.+)（(?P<source>[^@]+)@(?P<version>[^；]+)；格式 (?P<format>[^；]+)；大小 .+）", text)
    assert match and all(match.group(name).strip() for name in ("title", "artist", "source", "version", "format"))


def test_format_results_keeps_multiple_medium_candidates_with_continuous_numbers():
    candidates = [Candidate(source_id="source", source_version="1", item_id=str(i), title="歌名" * 12, artist="艺术家" * 8, album="专辑" * 8, format="mp3", bitrate=320, size=1536) for i in range(3)]
    text = format_results(candidates)
    assert len(text.encode("utf-8")) <= 2048
    assert len(text.splitlines()) >= 2
    assert [line.split(". ", 1)[0] for line in text.splitlines()] == [str(i) for i in range(1, len(text.splitlines()) + 1)]

def test_format_results_avoids_single_character_fragment_for_subsequent_candidates():
    c1 = Candidate(source_id="netease", source_version="1.0", item_id="1", title="First Song Long Title " * 2, artist="Artist Name " * 2, size=1024)
    c2 = Candidate(source_id="netease", source_version="1.0", item_id="2", title="Second Song Long Title " * 2, artist="Artist Name " * 2, size=1024)
    c1_bytes = len(format_results([c1], max_bytes=2048).encode("utf-8"))
    text = format_results([c1, c2], max_bytes=c1_bytes + 20)
    assert len(text.splitlines()) == 1
    assert "First Song" in text
