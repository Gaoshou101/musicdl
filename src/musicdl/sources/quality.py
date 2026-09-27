"""Shared parsing and display rules for source-reported audio quality."""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

LOSSLESS_CONTAINERS = frozenset({"flac", "alac", "ape", "wav", "aiff"})

# Stable panel/search ordering. These are source quality names, not proof of a
# specific codec or byte count; only explicit lossless names below imply one.
QUALITY_RANKS = {
    "master": 70,
    "atmos_plus": 65,
    "flac24bit": 60,
    "flac": 55,
    "alac": 54,
    "ape": 53,
    "wav": 52,
    "aiff": 51,
    "320k": 30,
    "192k": 20,
    "128k": 10,
}

_BITRATES = {"128k": 128, "192k": 192, "320k": 320}
_CONTAINERS = {
    "flac": "flac",
    "flac24bit": "flac",
    "alac": "alac",
    "ape": "ape",
    "wav": "wav",
    "aiff": "aiff",
}
_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(bytes?|b|kib|kb|mib|mb|gib|gb|tib|tb)?$", re.IGNORECASE)
_SIZE_UNITS = {
    None: 1,
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
    "tb": 1024**4,
    "tib": 1024**4,
}


def _token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized or None


def quality_rank(value: Any) -> int:
    """Return a named quality's stable rank, or zero when it is unknown."""
    token = _token(value)
    return QUALITY_RANKS.get(token, 0) if token is not None else 0


def is_lossless(value: Any) -> bool:
    """Whether a container or explicit FLAC quality name is lossless.

    ``master`` and ``atmos_plus`` are quality labels without a codec guarantee,
    so their higher display rank alone does not make them lossless.
    """
    token = _token(value)
    return token in LOSSLESS_CONTAINERS or token == "flac24bit"


def bitrate_kbps(value: Any) -> int | None:
    """Return kbps only for the three named lossy quality tiers."""
    token = _token(value)
    return _BITRATES.get(token) if token is not None else None


def container_of(value: Any) -> str | None:
    """Return a container only when its quality name identifies one."""
    token = _token(value)
    return _CONTAINERS.get(token) if token is not None else None


def parse_size(value: Any) -> int | None:
    """Parse a source size into bytes using 1024-based units.

    Bare numeric values are bytes. Strings may use B/KB/MB/GB/TB or their IEC
    KiB/MiB/GiB/TiB spellings, with case and surrounding/inter-unit spacing
    ignored. Booleans, negative values, and malformed labels are not sizes.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        return int(math.floor(value + 0.5))
    if not isinstance(value, str):
        return None
    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return None
    unit = match.group(2)
    multiplier = _SIZE_UNITS[unit.casefold() if unit is not None else None]
    return int(amount * multiplier + Decimal("0.5"))


def format_bytes(value: Any) -> str:
    """Format a byte count like the web panel; missing or invalid is empty."""
    size = parse_size(value)
    if size is None:
        return ""
    if size < 1024:
        return f"{size} B"
    units = ("KB", "MB", "GB", "TB")
    scaled = size / 1024
    index = 0
    while scaled >= 1024 and index < len(units) - 1:
        scaled /= 1024
        index += 1
    precision = 1 if scaled >= 10 else 2
    return f"{scaled:.{precision}f} {units[index]}"
