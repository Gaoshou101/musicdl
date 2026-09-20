from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

from .models import DownloadMetadata, Language, MediaError

_ILLEGAL: Final[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*]')
_RESERVED: Final[frozenset[str]] = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
_EXTENSIONS: Final[dict[str, tuple[str, frozenset[str]]]] = {
    ".mp3": ("audio/mpeg", frozenset({"mp3"})),
    ".flac": ("audio/flac", frozenset({"flac"})),
    ".m4a": ("audio/mp4", frozenset({"m4a"})),
    ".ogg": ("audio/ogg", frozenset({"ogg"})),
    # Raw ADTS is what a `.aac` link sometimes carries, and the suffix cannot
    # settle which of the two it is: on 2026-09-17 kuwo's car CDN answered
    # `car-bj.kuwo.cn/.../1904613985.aac` with an ISO base media file, and on
    # 2026-09-20 the same shape of link answered with plain ADTS frames.  Only
    # the bytes can name the container, which is why the transport classifies
    # what it reads instead of trusting the answer's own extension.
    ".aac": ("audio/aac", frozenset({"aac"})),
}


def _normalize_for_containment(path: Path) -> Path:
    if os.name != "nt":
        return path
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return path


def normalize_language(value: str | None) -> Language:
    return value if value in {"华语", "欧美", "日韩"} else "未知"  # type: ignore[return-value]


def sanitize_component(value: str | None) -> str:
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(value or ""))
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
        _normalize_for_containment(target.resolve(strict=False)).relative_to(_normalize_for_containment(root_path))
    except ValueError as exc:
        raise MediaError("path_escape") from exc
    return target


def _detected(header: bytes) -> tuple[str, str] | None:
    if _valid_id3(header) or _valid_mpeg(header):
        return ".mp3", "audio/mpeg"
    if header.startswith(b"fLaC"):
        return ".flac", "audio/flac"
    if _valid_m4a(header):
        return ".m4a", "audio/mp4"
    if _valid_adts(header):
        return ".aac", "audio/aac"
    if header.startswith(b"OggS"):
        return ".ogg", "audio/ogg"
    return None


def detect_media(header: bytes) -> tuple[str, str] | None:
    """The container these bytes announce, as ``(extension, media type)``.

    `validate_media` uses this on a finished download, and the transport uses it
    on the head of a response: neither the answer's extension nor its
    `Content-Type` label settles which container arrived, because the same link
    shape has been measured carrying two different containers and upstream CDNs
    have served a real FLAC stream as `audio/mpeg`.  What was read therefore
    decides both the extension the file is published under and the media type
    reported for it, so the two cannot disagree with each other or with the
    bytes.
    """
    return _detected(header)


def detect_container(header: bytes) -> str | None:
    """The same answer as `detect_media`, as a leading-dot extension alone."""
    detected = detect_media(header)
    return detected[0] if detected is not None else None


def _valid_adts(header: bytes) -> bool:
    """Whether these bytes start with a well-formed ADTS frame header.

    0xFFF is the frame sync, the layer bits are 00 for AAC, the sampling
    frequency index has to name one of the 13 defined rates, the channel
    configuration cannot be 0 (that is the in-band PCE case this product has
    never seen), and the frame cannot be shorter than the 7-byte header it just
    declared.  A 4-byte MPEG frame that merely looks like a sync is rejected by
    ``_valid_mpeg`` and stays rejected here because the header is truncated.
    """
    if len(header) < 7 or header[0] != 0xFF or header[1] & 0xF0 != 0xF0 or header[1] & 0x06:
        return False
    if (header[2] >> 2) & 0x0F >= 13:
        return False
    if (((header[2] & 0x01) << 2) | (header[3] >> 6)) == 0:
        return False
    frame_length = ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
    return frame_length >= 7


def _valid_id3(h: bytes) -> bool:
    if len(h) < 10 or h[:3] != b"ID3" or h[3] not in (2, 3, 4) or h[4] == 0xFF:
        return False
    if any(byte & 0x80 for byte in h[6:10]):
        return False
    flags = h[5]
    reserved_mask = {2: 0x3F, 3: 0x1F, 4: 0x0F}[h[3]]
    return not (flags & reserved_mask)


def _valid_mpeg(h: bytes) -> bool:
    if len(h) < 4 or h[0] != 0xFF or h[1] & 0xE0 != 0xE0:
        return False
    version, layer, bitrate, rate, emphasis = (h[1] >> 3) & 3, (h[1] >> 1) & 3, (h[2] >> 4) & 15, (h[2] >> 2) & 3, h[3] & 3
    return version != 1 and layer != 0 and bitrate not in (0, 15) and rate != 3 and emphasis != 2


def _valid_m4a(h: bytes) -> bool:
    if len(h) < 16 or h[4:8] != b"ftyp":
        return False
    size = int.from_bytes(h[:4], "big")
    if size < 16 or size % 4 or size > len(h):
        return False
    brands = [h[8:12]]
    end = size
    if (size - 16) % 4:
        return False
    brands.extend(h[16:end][i:i + 4] for i in range(0, end - 16, 4))
    return all(len(b) == 4 for b in brands) and b"M4A " in brands


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
        if extension == ".aac":
            accepted.add("audio/x-aac")
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
