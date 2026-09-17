from dataclasses import FrozenInstanceError
import os
from pathlib import Path

import pytest

from musicdl.media import MAX_MEDIA_BYTES, DownloadEvent, DownloadMetadata, MediaError, normalize_language, sanitize_component, validate_media, validated_destination
from musicdl.media.validation import detect_container

ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
MPEG = b"\xff\xfb\x90\x64"
M4A = b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A "
# What kuwo's car CDN actually served on 2026-09-17 for
# `car-bj.kuwo.cn/.../1904613985.aac`: a 32-byte box whose major brand is the
# generic `mp42`, with `M4A `, `mp42`, and `isom` as its compatible brands.
KUWO_AAC = bytes.fromhex("00000020667479706d703432000000004d3441206d70343269736f6d00000000")


def test_language_and_path_helpers(tmp_path):
    assert normalize_language("华语") == "华语"
    assert normalize_language("other") == "未知"
    assert sanitize_component(" ../AUX/song\x00 ") == "_AUX_song"
    path = validated_destination(tmp_path, "华语", "A/B", "../Song", ".mp3")
    assert path.relative_to(tmp_path).parts == ("华语", "A_B", "Song - A_B.mp3")


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path behavior")
def test_extended_length_target_prefix_does_not_trigger_path_escape(tmp_path, monkeypatch):
    original_resolve = Path.resolve
    calls = 0

    def resolve_with_extended_target(self, strict=False):
        nonlocal calls
        resolved = original_resolve(self, strict=strict)
        calls += 1
        if calls == 2:
            return Path("\\\\?\\" + str(resolved))
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_with_extended_target)
    expected = tmp_path / "华语" / "artist" / "song - artist.mp3"
    assert validated_destination(tmp_path, "华语", "artist", "song", ".mp3") == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path behavior")
def test_extended_length_unc_target_prefix_does_not_trigger_path_escape(tmp_path, monkeypatch):
    original_resolve = Path.resolve
    calls = 0

    def resolve_with_extended_unc_target(self, strict=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Path(r"\\server\share\root")
        return Path(r"\\?\UNC\server\share\root\华语\artist\song - artist.mp3")

    monkeypatch.setattr(Path, "resolve", resolve_with_extended_unc_target)
    expected = Path(r"\\server\share\root") / "华语" / "artist" / "song - artist.mp3"
    assert validated_destination(tmp_path, "华语", "artist", "song", ".mp3") == expected


@pytest.mark.parametrize("header,fmt", [(ID3, "mp3"), (MPEG, "mp3"), (b"fLaC", "flac"), (M4A, "m4a"), (b"OggS\x00", "ogg")])
def test_valid_signatures(header, fmt):
    ext, mime = validate_media(header, DownloadMetadata(chunks=(), extension=fmt), fmt)
    assert ext == "." + fmt and mime


def test_a_generic_major_brand_still_names_the_container_its_brand_list_names():
    # The brand list identifies the container, and the major brand is only its
    # first entry: kuwo's `.aac` links are mp42 files that list `M4A ` among
    # their brands, and reading that list is what admits them.
    assert detect_container(KUWO_AAC) == ".m4a"
    assert validate_media(KUWO_AAC, DownloadMetadata(chunks=(), extension="m4a"), None) == (".m4a", "audio/mp4")
    # Cut off before the brands, the same bytes still decide nothing: this is
    # the defect that was fixed, not a widened signature table.
    with pytest.raises(MediaError, match="signature_mismatch"):
        validate_media(KUWO_AAC[:16], DownloadMetadata(chunks=(), extension="m4a"), None)


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
        validate_media(ID3, DownloadMetadata(chunks=(), extension="mp3"), "flac")


def test_exact_allowlist():
    with pytest.raises(MediaError, match="unsupported_extension"):
        validate_media(ID3, DownloadMetadata(chunks=(), extension="m4b"), "m4b")
    with pytest.raises(MediaError, match="signature_mismatch"):
        validate_media(b"\x00\x00\x00\x18ftypisom", DownloadMetadata(chunks=(), extension="m4a"), "m4a")
    cases = [("wav", "unsupported_extension"), ("flac", "extension_mismatch")]
    for ext, code in cases:
        try:
            validate_media(ID3, DownloadMetadata(chunks=(), extension=ext), "mp3")
        except MediaError as error:
            assert error.code == code
        else:
            raise AssertionError("expected MediaError")


@pytest.mark.parametrize("header", [b"ID3", b"ID3\x04\x00\x00\x80\x00\x00\x00", b"ID3\x04\x00\x01\x00\x00\x00\x00"])
def test_malformed_id3_rejected(header):
    with pytest.raises(MediaError, match="signature_mismatch"):
        validate_media(header, DownloadMetadata(chunks=(), extension="mp3"), "mp3")


@pytest.mark.parametrize("version,reserved", [(2, 0x20), (3, 0x10)])
def test_version_specific_id3_reserved_flags_rejected(version, reserved):
    header = b"ID3" + bytes((version, 0, reserved)) + b"\x00\x00\x00\x00"
    with pytest.raises(MediaError, match="signature_mismatch"):
        validate_media(header, DownloadMetadata(chunks=(), extension="mp3"), "mp3")


@pytest.mark.parametrize("version,allowed", [(2, 0xC0), (3, 0xE0), (4, 0xF0)])
def test_version_specific_id3_allowed_flags_accepted(version, allowed):
    header = b"ID3" + bytes((version, 0, allowed)) + b"\x00\x00\x00\x00"
    assert validate_media(header, DownloadMetadata(chunks=(), extension="mp3"), "mp3") == (".mp3", "audio/mpeg")


@pytest.mark.parametrize("header", [b"\xff\xeb\x90\x64", b"\xff\xfb\x00\x64", b"\xff\xfb\xf0\x64", b"\xff\xf1\x50\x80", b"\xff\xfb\x90\x66"])
def test_reserved_mpeg_and_aac_rejected(header):
    with pytest.raises(MediaError, match="signature_mismatch"):
        validate_media(header, DownloadMetadata(chunks=(), extension="mp3"), "mp3")


@pytest.mark.parametrize("header", [b"\x00\x00\x00\x18ftypM4A ", b"\x00\x00\x00\x15ftypM4A \x00\x00\x00\x00", b"\x00\x00\x00\x14ftypisom \x00\x00\x00\x00isom"])
def test_malformed_m4a_rejected(header):
    with pytest.raises(MediaError, match="signature_mismatch"):
        validate_media(header, DownloadMetadata(chunks=(), extension="m4a"), "m4a")


@pytest.mark.parametrize("value,expected", [("华语", "华语"), ("欧美", "欧美"), ("日韩", "日韩"), ("未知", "未知"), ("bad", "未知"), (None, "未知")])
def test_all_language_values(value, expected):
    assert normalize_language(value) == expected


def test_del_and_c1_removed():
    assert sanitize_component("A\x7f\x85B") == "AB"
    for header, code in [(b"", "signature_mismatch"), (ID3, "mime_mismatch")]:
        try:
            validate_media(header, DownloadMetadata(chunks=(), extension="mp3", media_type="audio/flac" if code == "mime_mismatch" else None), "mp3")
        except MediaError as error:
            assert error.code == code
        else:
            raise AssertionError("expected MediaError")
