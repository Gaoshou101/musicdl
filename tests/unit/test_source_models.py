import pytest
from pydantic import ValidationError

from musicdl.sources.models import Candidate, decode_escapes, normalize_text


def test_candidate_normalizes_public_text_and_identity():
    c = Candidate(source_id="qq", source_version=" 1.0 ", item_id=" 42 ", title="  A\u00a0  song ", artist=" B ", size=2048)
    assert c.title == "A song"
    assert c.artist == "B"
    assert c.canonical_version_key[0:2] == ("a song", "b")
    assert c.public_representation["source_id"] == "qq"
    assert normalize_text("Ａ\u3000 B") == "A B"


def test_candidate_bounds_and_required_fields():
    with pytest.raises(ValidationError):
        Candidate(source_id="", source_version="1", item_id="1", title="x", artist="y")
    with pytest.raises(ValidationError):
        Candidate(source_id="a", source_version="1", item_id="1", title="x" * 501, artist="y")

def test_optional_blank_values_are_none_and_enabled_types_are_strict():
    c = Candidate(source_id="a", source_version="1", item_id="1", title="x", artist="y", album="  ", format="\t")
    assert c.album is None and c.format is None
    # The catalogue is the last part of the identity, and a channel that did
    # not state one leaves it empty rather than guessing.
    assert c.canonical_version_key == ("x", "y", "", None, "", None, "")


def test_the_catalogue_a_listing_came_from_is_part_of_its_identity():
    """Two platforms, one song: two files, so two rows instead of one."""
    base = dict(source_id="a", source_version="1", item_id="1", title="夜曲", artist="周杰伦",
                album="十一月的萧邦", duration=226)
    qq = Candidate(**base, platform=" tx ")
    netease = Candidate(**base, platform="wy")

    assert qq.platform == "tx"
    assert qq.canonical_version_key != netease.canonical_version_key
    # The same catalogue entry listed by two channels is still one recording.
    assert qq.canonical_version_key == Candidate(**base, platform="TX").canonical_version_key


def test_a_platform_name_that_is_not_one_is_refused_rather_than_repaired():
    base = dict(source_id="a", source_version="1", item_id="1", title="x", artist="y")
    with pytest.raises(ValidationError):
        Candidate(**base, platform="lx:tx:1")
    with pytest.raises(ValidationError):
        Candidate(**base, platform="a" * 17)
    assert Candidate(**base, platform="   ").platform is None


def test_the_escaping_an_upstream_left_in_its_own_text_is_read_as_the_text_it_meant():
    """Kuwo writes the ``&`` between two names four backslashes deep.

    Measured 2026-09-22: one JSON read of that literal still left two of the
    four, which is what reached the WeCom listing and the file name as text
    nobody wrote.  These are the two backslashes a reader's row carried.
    """
    c = Candidate(source_id="a", source_version="1", item_id="1",
                  title=r"中国梦\\u0026我的梦", artist=r"廖昌永\\u0026谭维维", album=r"\\u0054")
    assert c.title == "中国梦&我的梦"
    assert c.artist == "廖昌永&谭维维"
    # Resolved before the whitespace pass, so an escaped non-breaking space is
    # still collapsed like the plain one it stands for.
    assert Candidate(**{**c.model_dump(), "album": r"\\u00a0B\\u00a0"}).album == "B"


def test_one_layer_is_resolved_once_and_deeper_layers_until_they_stop():
    assert decode_escapes(r"\u0026") == "&"
    assert decode_escapes(r"\\u0026") == "&"
    # An odd backslash is an escaped one plus the escape it prefixes; only a
    # pair of them is two layers of the same escape.
    assert decode_escapes(r"\\\u0027") == "\\'"
    assert decode_escapes(r"\\\\u0027") == "'"
    assert decode_escapes(r"a\/b") == "a/b"


def test_a_value_that_only_looks_escaped_keeps_what_it_says():
    """Nothing separates a path from an escape except which escapes exist."""
    assert decode_escapes(r"C:\temp") == r"C:\temp"
    assert decode_escapes(r"AC\DC") == r"AC\DC"
    assert decode_escapes("plain 稻香") == "plain 稻香"


def test_a_surrogate_pair_is_one_character_and_half_of_one_stays_an_escape():
    assert decode_escapes(r"\ud83c\udfb5") == "\U0001F3B5"
    # Half a pair cannot be encoded as UTF-8 at all, so it is not made into text
    # nothing downstream could send.
    assert decode_escapes(r"\ud83c") == r"\ud83c"


def test_the_identity_a_source_is_asked_to_resolve_is_never_rewritten():
    c = Candidate(source_id="a", source_version="1", item_id=r"lx:kw:1\\u0026x",
                  title="x", artist="y")
    assert c.item_id == r"lx:kw:1\\u0026x"
