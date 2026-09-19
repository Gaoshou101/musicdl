import pytest
from pydantic import ValidationError

from musicdl.sources.models import Candidate, normalize_text


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
