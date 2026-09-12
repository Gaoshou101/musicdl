from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from musicdl.media import MAX_MEDIA_BYTES, DownloadEvent, DownloadMetadata, MediaError, normalize_language, sanitize_component, validate_media, validated_destination


def test_language_and_path_helpers(tmp_path):
    assert normalize_language("华语") == "华语"
    assert normalize_language("other") == "未知"
    assert sanitize_component(" ../AUX/song\x00 ") == "_AUX_song"
    path = validated_destination(tmp_path, "华语", "A/B", "../Song", ".mp3")
    assert path.relative_to(tmp_path).parts == ("华语", "A_B", "Song - A_B.mp3")


@pytest.mark.parametrize("header,fmt", [(b"ID3\x04", "mp3"), (b"\xff\xfb\x90", "mp3"), (b"fLaC", "flac"), (b"\x00\x00\x00\x18ftypM4A ", "m4a"), (b"OggS\x00", "ogg")])
def test_valid_signatures(header, fmt):
    ext, mime = validate_media(header, DownloadMetadata(chunks=(), extension=fmt), fmt)
    assert ext == "." + fmt and mime


def test_contract_constants_frozen_and_no_directory_creation(tmp_path):
    assert MAX_MEDIA_BYTES == 500 * 1024 * 1024
    event = DownloadEvent("r", "c", "s", "v", "stage", "ok")
    with pytest.raises(FrozenInstanceError):
        event.status = "bad"
    target = validated_destination(tmp_path, "华语", "A", "B", "MP3")
    assert target.suffix == ".mp3" and not target.parent.exists()
    assert sanitize_component("CON.txt") == "_CON.txt"


def test_resolved_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    language = tmp_path / "华语"
    language.mkdir()
    link = language / "artist"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(MediaError, match="path_escape"):
        validated_destination(tmp_path, "华语", "artist", "song", ".mp3")


def test_candidate_format_mismatch_alone():
    with pytest.raises(MediaError, match="extension_mismatch"):
        validate_media(b"ID3", DownloadMetadata(chunks=(), extension="mp3"), "flac")


def test_exact_allowlist():
    with pytest.raises(MediaError, match="unsupported_extension"):
        validate_media(b"ID3", DownloadMetadata(chunks=(), extension="m4b"), "m4b")
    with pytest.raises(MediaError, match="signature_mismatch"):
        validate_media(b"\x00\x00\x00\x18ftypisom", DownloadMetadata(chunks=(), extension="m4a"), "m4a")
    cases = [("wav", "unsupported_extension"), ("flac", "extension_mismatch")]
    for ext, code in cases:
        try:
            validate_media(b"ID3", DownloadMetadata(chunks=(), extension=ext), "mp3")
        except MediaError as error:
            assert error.code == code
        else:
            raise AssertionError("expected MediaError")
    for header, code in [(b"", "signature_mismatch"), (b"ID3", "mime_mismatch")]:
        try:
            validate_media(header, DownloadMetadata(chunks=(), extension="mp3", media_type="audio/flac" if code == "mime_mismatch" else None), "mp3")
        except MediaError as error:
            assert error.code == code
        else:
            raise AssertionError("expected MediaError")
