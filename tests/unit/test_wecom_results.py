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
