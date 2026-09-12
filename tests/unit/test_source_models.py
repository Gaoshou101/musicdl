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
