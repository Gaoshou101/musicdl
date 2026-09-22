import pytest

from musicdl.sources.models import Candidate
from musicdl.wecom.results import (NO_RESULTS_TEXT, SELECTION_PROMPT, format_results,
                                   platform_name, selection_message)


def row(**fields):
    """One candidate, with everything a channel may or may not have stated."""
    stated = {"source_id": "qq", "source_version": "2.1", "item_id": "x",
              "title": "Song", "artist": "Artist"}
    return Candidate(**{**stated, **fields})


def test_a_row_leads_with_what_a_person_chooses_by_and_names_the_catalogue():
    text = format_results([row(album="Album", duration=65, format="mp3", bitrate=320, platform="wy")])
    assert text == "1. Song \u2014 Artist\u300aAlbum\u300b  \u00b7  1:05  \u00b7  MP3 320kbps  \u00b7  \u7f51\u6613\u4e91"


def test_a_field_the_channel_never_stated_is_left_out_instead_of_called_unknown():
    text = format_results([row()])
    assert text == "1. Song \u2014 Artist"
    assert "\u672a\u77e5" not in text


def test_an_album_that_only_repeats_its_song_is_not_printed_twice():
    assert format_results([row(title="\u6674\u5929", artist="\u5468\u6770\u4f26", album="\u6674\u5929")]) == "1. \u6674\u5929 \u2014 \u5468\u6770\u4f26"


def test_a_reply_never_shows_the_escaping_an_upstream_left_in_a_name():
    """The row that reached a person spelled ``\\u0026`` where the ``&`` belonged."""
    text = format_results([row(title="\u4e2d\u56fd\u68a6\\\\u0026\u6211\u7684\u68a6",
                              artist="\u5ed6\u660c\u6c38\\\\u0026\u8c2d\u7ef4\u7ef4")])

    assert "\\u0026" not in text
    assert text == "1. \u4e2d\u56fd\u68a6&\u6211\u7684\u68a6 \u2014 \u5ed6\u660c\u6c38&\u8c2d\u7ef4\u7ef4"


def test_a_recording_past_the_hour_reads_as_a_clock():
    assert format_results([row(duration=3723)]) == "1. Song \u2014 Artist  \u00b7  1:02:03"


@pytest.mark.parametrize(("token", "expected"), [
    ("wy", "\u7f51\u6613\u4e91"), ("tx", "QQ\u97f3\u4e50"), ("kw", "\u9177\u6211"), ("kg", "\u9177\u72d7"),
    ("KG", "\u9177\u72d7"), ("some-channel", "some-channel"), (None, ""), ("", "")])
def test_a_catalogue_is_named_the_way_a_person_reads_it(token, expected):
    assert platform_name(token) == expected


def test_format_results_requires_positive_bounded_max_items():
    with pytest.raises(ValueError): format_results([row()], max_items=0)
    with pytest.raises(ValueError): format_results([row()], max_items=101)


def test_format_results_rejects_invalid_byte_limit_and_empty_input():
    assert format_results([], max_bytes=128) == ""
    with pytest.raises(ValueError): format_results([], max_bytes=127)
    with pytest.raises(ValueError): format_results([], max_bytes=2049)


def test_max_items_caps_the_listing_and_the_numbers_stay_continuous():
    candidates = [row(item_id=str(index)) for index in range(30)]
    text = format_results(candidates, max_items=3)
    assert [line.split(". ", 1)[0] for line in text.splitlines()] == ["1", "2", "3"]


def test_an_oversized_row_spends_the_budget_on_the_row_and_not_on_the_extras():
    wide = row(title="\u6b4c" * 40, artist="\u827a" * 40, album="\u4e13\u8f91" * 20, duration=200,
               format="flac", platform="wy")
    roomy, tight = format_results([wide]), format_results([wide], max_bytes=256)
    assert all(part in roomy for part in ("\u4e13\u8f91", "FLAC", "\u7f51\u6613\u4e91"))
    assert len(tight.encode("utf-8")) <= 256
    assert tight == "1. " + "\u6b4c" * 40 + " \u2014 " + "\u827a" * 40


def test_a_row_that_cannot_fit_ends_the_listing_instead_of_leaving_a_hole():
    wide = row(title="\u6b4c" * 400, artist="\u827a" * 400)
    text = format_results([wide, row(item_id="later", title="B", artist="B")], max_bytes=128)
    assert text.splitlines() and text.splitlines()[0].startswith("1. ")
    assert len(text.splitlines()) == 1 and len(text.encode("utf-8")) <= 128
    assert "\ufffd" not in text


def test_a_listing_is_byte_bounded_even_when_every_field_is_long():
    candidates = [row(item_id=str(index), title="\u6b4c" * 500, artist="\u827a" * 500, album="\u4e13\u8f91" * 250,
                      duration=86400, bitrate=10000, format="flac", platform="kg") for index in range(4)]
    text = format_results(candidates)
    assert len(text.encode("utf-8")) <= 2048 and "\ufffd" not in text
    for index, line in enumerate(text.splitlines(), 1):
        assert line.startswith(f"{index}. ")


def test_the_reply_says_what_was_searched_and_how_to_choose():
    text = selection_message("\u6674\u5929-\u5468\u6770\u4f26", [row(title="\u6674\u5929", artist="\u5468\u6770\u4f26", format="mp3")])
    assert text == "\u300c\u6674\u5929-\u5468\u6770\u4f26\u300d\u627e\u5230 1 \u4e2a\u7ed3\u679c\uff1a\n\n1. \u6674\u5929 \u2014 \u5468\u6770\u4f26  \u00b7  MP3" + SELECTION_PROMPT
    assert SELECTION_PROMPT.strip() == "\u56de\u590d\u5e8f\u53f7\u4e0b\u8f7d\u3002"


def test_a_prompt_that_followed_a_failed_download_says_why_it_repeats():
    notice = "\u4e0a\u4e00\u6b21\u7684\u7ed3\u679c\u4e0b\u8f7d\u5931\u8d25\uff0c\u8fd9\u91cc\u662f\u6700\u65b0\u7684\u7ed3\u679c\uff1a"
    text = selection_message("Song Artist", [row()], notice=notice)
    assert text.startswith(f"{notice}\n\u300cSong Artist\u300d\u627e\u5230 1 \u4e2a\u7ed3\u679c\uff1a")
    assert text.endswith(SELECTION_PROMPT)


def test_a_search_nothing_answered_does_not_ask_for_a_number():
    assert selection_message("nothing", []) == NO_RESULTS_TEXT == "\u6ca1\u6709\u627e\u5230\u5339\u914d\u7ed3\u679c\u3002"
    assert "\u56de\u590d\u5e8f\u53f7" not in selection_message("nothing", [])


def test_a_query_pasted_by_the_page_long_gives_way_to_the_row_it_would_introduce():
    text = selection_message("\u6b4c\u8bcd" * 200, [row()], max_bytes=256)
    assert text == "1. Song \u2014 Artist" + SELECTION_PROMPT
    assert len(text.encode("utf-8")) <= 256


def test_a_long_query_is_shortened_but_kept_when_the_reply_has_room():
    head = selection_message("\u6b4c\u8bcd" * 200, [row()]).splitlines()[0]
    assert head.startswith("\u300c\u6b4c\u8bcd") and head.endswith("\u300d\u627e\u5230 1 \u4e2a\u7ed3\u679c\uff1a")
    assert "\u2026" in head and len(head) <= 60


def test_the_whole_reply_stays_inside_the_byte_budget_the_channel_enforces():
    candidates = [row(item_id=str(index), title="\u6b4c" * 40, artist="\u827a" * 40, album="\u4e13\u8f91" * 20,
                      duration=200, format="flac", bitrate=1411, platform="tx") for index in range(10)]
    text = selection_message("\u6b4c \u827a", candidates, max_bytes=2048)
    assert text.startswith("\u300c\u6b4c \u827a\u300d\u627e\u5230 10 \u4e2a\u7ed3\u679c\uff1a") and text.endswith(SELECTION_PROMPT)
    assert len(text.encode("utf-8")) <= 2048 and len(text.splitlines()) > 2


def test_selection_message_refuses_a_query_that_is_not_text_and_a_budget_without_room():
    with pytest.raises(ValueError): selection_message(None, [row()])
    with pytest.raises(ValueError): selection_message("q", [row()], max_bytes=255)
    with pytest.raises(ValueError): selection_message("q", [row()], max_items=0)
