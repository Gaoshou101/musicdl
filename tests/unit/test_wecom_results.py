from musicdl.sources.models import Candidate
from musicdl.wecom.results import format_results
import pytest


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
