from musicdl.media import DownloadMetadata, MediaError, normalize_language, sanitize_component, validate_media, validated_destination


def test_language_and_path_helpers(tmp_path):
    assert normalize_language("华语") == "华语"
    assert normalize_language("other") == "未知"
    assert sanitize_component(" ../AUX/song\x00 ") == "_AUX_song"
    path = validated_destination(tmp_path, "华语", "A/B", "../Song", ".mp3")
    assert path.relative_to(tmp_path).parts == ("华语", "A_B", "Song - A_B.mp3")


def test_validation_contracts():
    metadata = DownloadMetadata(chunks=())
    for header, fmt in [(b"ID3\x04", "mp3"), (b"\xff\xfb\x90", "mp3"), (b"fLaC", "flac"), (b"\x00\x00\x00\x18ftypM4A ", "m4a"), (b"OggS\x00", "ogg")]:
        ext, mime = validate_media(header, DownloadMetadata(chunks=(), extension=fmt, media_type=None), fmt)
        assert ext == "." + fmt and mime
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
