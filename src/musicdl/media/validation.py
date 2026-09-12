from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from .models import DownloadMetadata, Language, MediaError

_ILLEGAL: Final[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED: Final[frozenset[str]] = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
_EXTENSIONS: Final[dict[str, tuple[str, frozenset[str]]]] = {
    ".mp3": ("audio/mpeg", frozenset({"mp3"})),
    ".flac": ("audio/flac", frozenset({"flac"})),
    ".m4a": ("audio/mp4", frozenset({"m4a"})),
    ".ogg": ("audio/ogg", frozenset({"ogg"})),
}


def normalize_language(value: str | None) -> Language:
    return value if value in {"华语", "欧美", "日韩"} else "未知"  # type: ignore[return-value]


def sanitize_component(value: str | None) -> str:
    text = re.sub(r"[\x00-\x1f]", "", str(value or ""))
    text = _ILLEGAL.sub("_", text)
    text = text.strip(" .").rstrip(" .")
    if not text:
        return "未知"
    stem = text.split(".", 1)[0].upper()
    if stem in _RESERVED:
        text = "_" + text
    return text


def validated_destination(root: str | Path, language: str | None, artist: str, title: str, extension: str) -> Path:
    root_path = Path(root).resolve(strict=False)
    def clean_name(value: str) -> str:
        # Traversal components are discarded before separator sanitization.
        value = re.sub(r"(?:^|[/\\])\.\.(?=[/\\]|$)", "", value).lstrip("/\\")
        return sanitize_component(value)
    lang = sanitize_component(normalize_language(language))
    artist_name = clean_name(artist)
    title_name = clean_name(title)
    ext = str(extension).lower()
    if not ext.startswith("."):
        ext = "." + ext
    if ext not in _EXTENSIONS:
        raise MediaError("unsupported_extension")
    target = root_path / lang / artist_name / f"{title_name} - {artist_name}{ext}"
    try:
        target.resolve(strict=False).relative_to(root_path)
    except ValueError as exc:
        raise MediaError("path_escape") from exc
    return target


def _detected(header: bytes) -> tuple[str, str] | None:
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return ".mp3", "audio/mpeg"
    if header.startswith(b"fLaC"):
        return ".flac", "audio/flac"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] == b"M4A ":
        return ".m4a", "audio/mp4"
    if header.startswith(b"OggS"):
        return ".ogg", "audio/ogg"
    return None


def validate_media(header: bytes, metadata: DownloadMetadata, candidate_format: str | None) -> tuple[str, str]:
    declared_ext = metadata.extension.lower() if metadata.extension else None
    if declared_ext and not declared_ext.startswith("."):
        declared_ext = "." + declared_ext
    if declared_ext is not None and declared_ext not in _EXTENSIONS:
        raise MediaError("unsupported_extension")
    detected = _detected(header)
    if detected is None:
        raise MediaError("signature_mismatch")
    extension, media_type = detected
    if declared_ext and declared_ext != extension:
        raise MediaError("extension_mismatch")
    if metadata.media_type:
        accepted = {media_type}
        if extension == ".m4a":
            accepted.add("audio/x-m4a")
        if extension == ".ogg":
            accepted.add("application/ogg")
        if metadata.media_type.lower() not in accepted:
            raise MediaError("mime_mismatch")
    if candidate_format:
        candidate_ext = "." + candidate_format.lstrip(".").lower()
        if candidate_ext not in _EXTENSIONS:
            raise MediaError("unsupported_extension")
        if candidate_ext != extension:
            raise MediaError("extension_mismatch")
    return extension, media_type
