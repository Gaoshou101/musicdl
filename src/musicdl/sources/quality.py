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


def requested_quality(candidate: Any, *, policy: str = "lossless_first",
                      preference: str | None = None) -> str | None:
    """Choose only from declared tiers; an unsupported preference is advisory."""
    declared = tuple(candidate.qualities)
    preferred = _token(preference)
    if preferred in declared:
        return preferred
    if policy == "best_available" and preferred is None:
        return None
    lossless = [value for value in declared if is_lossless(value)]
    if lossless:
        return max(lossless, key=quality_rank)
    if declared:
        return max(declared, key=quality_rank)
    return candidate.format if is_lossless(candidate.format) else None


def proven_lossy(value: Any) -> bool:
    """Unknown and ambiguous codec labels never prove a downgrade."""
    return _token(value) in {"128k", "192k", "320k", "mp3", "aac", "ogg", "opus"}


def served_quality(stated: Any, container: Any) -> str | None:
    """Reconcile the tier a source stated with the container the bytes prove.

    The file on disk is the only evidence of what was served, so a label the
    bytes contradict is dropped in favour of the container.  That covers both
    shapes a contradiction comes in.  A label that names a container the bytes
    are not -- measured 2026-09-28, a link whose path ended in ``.flac``
    answered MP3 frames, and repeating the path would have named a file that is
    not on disk.  And a bitrate tier, which names a codec without naming a
    container -- measured the same day, a source that answered ``320k`` handed
    over real FLAC, so keeping the tier would name a codec the file is not and
    mark a lossless answer as the downgrade it never was.  A label the container
    can carry keeps its detail (``flac24bit`` on ``flac``, ``320k`` on ``mp3``),
    which is what the success message and the admin report print, and a label
    naming neither (``master``, ``atmos_plus``) claims no codec at all, so
    nothing contradicts it and it stands.
    """
    label = _token(stated)
    verified = _token(container)
    if verified is not None:
        verified = verified.lstrip(".") or None
    if label is None:
        return verified
    named = _CONTAINERS.get(label)
    if named is not None and verified is not None and named != verified:
        return verified
    if named is None and verified is not None and is_lossless(verified) and proven_lossy(label):
        # Naming the tier here would name a codec the file is not, and would
        # mark a lossless answer as the downgrade it never was.
        return verified
    return label


# The revision of the actual-quality verification this product stands behind:
# ``proven_lossy`` above, the answer reconciliation in ``lx_shim.js``, and the
# downmix verdict a download record stores.  A record keeps the revision that
# judged it, so a later, stricter verifier can recognise the old record and
# re-check it instead of trusting a verdict it no longer stands behind.
# MusicBot-Go calls the same pair ``QualityVerified``/``QualityRevision``; bump
# this whenever the rules above change meaning.
QUALITY_REVISION = 1
