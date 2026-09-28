import pytest

from musicdl.sources.quality import (
    LOSSLESS_CONTAINERS,
    QUALITY_RANKS,
    bitrate_kbps,
    container_of,
    format_bytes,
    is_lossless,
    parse_size,
    quality_rank,
)


def test_quality_catalogue_and_rank_cover_the_supported_names():
    assert LOSSLESS_CONTAINERS == frozenset({"flac", "alac", "ape", "wav", "aiff"})
    assert QUALITY_RANKS == {
        "master": 70, "atmos_plus": 65, "flac24bit": 60, "flac": 55,
        "alac": 54, "ape": 53, "wav": 52, "aiff": 51,
        "320k": 30, "192k": 20, "128k": 10,
    }
    assert quality_rank(" MASTER ") == 70
    assert quality_rank("flac24bit") == 60
    assert quality_rank("unlisted") == 0
    assert quality_rank(None) == 0


@pytest.mark.parametrize("value", ["flac", "alac", "ape", "wav", "aiff", "flac24bit", " FLAC "])
def test_lossless_accepts_container_and_quality_names(value):
    assert is_lossless(value)


@pytest.mark.parametrize("value", [None, "mp3", "320k", "128k", "master", "atmos_plus", "unknown"])
def test_lossless_refuses_lossy_and_unknown_names(value):
    assert not is_lossless(value)


def test_bitrate_and_container_are_only_inferred_from_named_quality_values():
    assert [bitrate_kbps(name) for name in ("128k", "192k", "320k")] == [128, 192, 320]
    assert bitrate_kbps(" 320K ") == 320
    assert bitrate_kbps("320kbps") is None
    assert bitrate_kbps("flac") is None
    assert container_of(" FLAC24BIT ") == "flac"
    assert container_of("alac") == "alac"
    assert container_of("320k") is None
    assert container_of("master") is None
    assert container_of("unknown") is None


@pytest.mark.parametrize(("raw", "expected"), [
    ("1.5 MB", 1_572_864), (" 2 mb ", 2_097_152), ("512 B", 512),
    ("1.5MiB", 1_572_864), ("1024", 1024),
])
def test_parse_size_uses_a_case_and_spacing_tolerant_1024_base(raw, expected):
    assert parse_size(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "unknown", "-2 MB", "1 XB", "NaN GB", -1, True, float("inf")])
def test_parse_size_returns_none_for_missing_or_invalid_input(raw):
    assert parse_size(raw) is None


def test_format_bytes_matches_the_panel_and_leaves_missing_size_empty():
    assert format_bytes(None) == ""
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.00 KB"
    assert format_bytes(1536) == "1.50 KB"
