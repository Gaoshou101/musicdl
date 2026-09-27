"""Read the real playing time out of a finished audio file.

Only container headers are parsed, so this stays plain Python: no ffmpeg, no
ffprobe, no native decoder, and no new dependency.  A file this module cannot
understand answers ``None`` instead of raising, because bytes nobody here
recognises are not evidence of anything -- the caller decides what an unknown
duration means for the download it is holding.

What is measured is what the file really holds, not what its headers claim:
a 30-second preview or a stream cut off mid-way measures short, which is the
whole point of asking.
"""

from __future__ import annotations

import io
import os
from typing import Final

# Enough for a FLAC ``STREAMINFO`` block and for an MP3's first frame plus its
# ``Xing``/``Info`` tag, which is all either container needs from the head.
HEAD_BYTES: Final[int] = 256 * 1024
# An MP3 is measured by walking its frame chain, which needs the whole stream.
# The walk reads that stream in chunks, so a large recording measures like a
# small one; this and the frame ceiling below are what bound a corrupt file's
# work.
SCAN_CHUNK_BYTES: Final[int] = 1024 * 1024
# The first frame plus its ``Xing``/``Info`` tag, which is all the head of a
# stream has to hold.
MAX_MP3_HEAD_BYTES: Final[int] = 4096
# The largest ``moov`` box this parser pulls in, and a frame count no real
# recording reaches, so neither a hostile nor a corrupt file can spin here.
MAX_MOOV_BYTES: Final[int] = 32 * 1024 * 1024
MAX_MP3_FRAMES: Final[int] = 4_000_000
# A stretch this parser cannot read is looked past rather than read as the end
# of the audio: an ``ID3v2`` tag written between two segments is the case that
# happens in the wild, and stopping at it would measure a whole recording as
# the piece before the tag.  Both bounds keep a corrupt file's scan finite.
MAX_MP3_RESYNCS: Final[int] = 32
RESYNC_SCAN_BYTES: Final[int] = 512 * 1024

_MPEG1_SAMPLE_RATES: Final[tuple[int, ...]] = (44100, 48000, 32000)
_MPEG2_SAMPLE_RATES: Final[tuple[int, ...]] = (22050, 24000, 16000)
_MPEG25_SAMPLE_RATES: Final[tuple[int, ...]] = (11025, 12000, 8000)
# Layer III bitrates by MPEG version family: index 0 is "free format" and
# index 15 is invalid, so both are refused rather than guessed at.
_LAYER3_BITRATES: Final[dict[float, tuple[int, ...]]] = {
    1.0: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2.0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_LAYER3_SAMPLES: Final[dict[float, int]] = {1.0: 1152, 2.0: 576}


def duration_seconds(path: str | os.PathLike[str]) -> float | None:
    """The playing time one file really holds, or ``None`` when it is unknown."""
    try:
        with open(os.fspath(path), "rb") as handle:
            head = handle.read(HEAD_BYTES)
            if head.startswith(b"fLaC"):
                return _flac_duration(head)
            if _is_mp4(head):
                return _mp4_file_duration(handle, os.fstat(handle.fileno()).st_size)
            if _is_mp3(head):
                start = _id3_size(head)
                return None if start is None else _mp3_duration(handle, start)
            return None
    except Exception:  # noqa: BLE001 - an unreadable file is an unknown duration
        return None


def container_duration(data: bytes) -> float | None:
    """The same answer for bytes already in hand, and the shape the tests use."""
    try:
        if data.startswith(b"fLaC"):
            return _flac_duration(data)
        if _is_mp4(data):
            moov = _find_box(data, 0, len(data), b"moov", limit=MAX_MOOV_BYTES)
            if moov is None:
                return None
            mvhd = _find_box(data, moov[0], moov[1], b"mvhd")
            return None if mvhd is None else _read_mvhd(data[mvhd[0]:mvhd[1]])
        if _is_mp3(data):
            start = _id3_size(data)
            return None if start is None else _mp3_stream_duration(data, start)
        return None
    except Exception:  # noqa: BLE001 - unparsable bytes are an unknown duration
        return None


def duration_tolerance(expected: float) -> tuple[float, float]:
    """How far off a real file may be, as ``(short_side, long_side)`` seconds.

    The same shape MusicBot-Go uses: both sides scale with the expected length,
    the short side is capped at three seconds so a preview cannot hide inside a
    long song's slack, and the long side keeps a floor of 1.5 s so a short
    track's slack cannot round away to nothing.
    """
    return min(3.0, expected / 20.0), max(1.5, expected / 20.0)


def duration_matches(expected: float, measured: float) -> bool:
    """Whether a measured time is inside the tolerance around the expected one."""
    short, long = duration_tolerance(expected)
    return expected - short <= measured <= expected + long


def bitrate_kbps(size_bytes: int, duration: float) -> int | None:
    """The rate the bytes actually carried, or ``None`` when it cannot be told.

    This is deliberately a measurement rather than a label: it is the real byte
    count over the real playing time, so a caller that shows it is reporting
    what arrived instead of repeating what a channel claimed.
    """
    if size_bytes <= 0 or duration <= 0:
        return None
    return round(size_bytes * 8 / duration / 1000)


def _is_mp4(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] == b"ftyp"


def _is_mp3(head: bytes) -> bool:
    if len(head) < 4:
        return False
    if head[:3] == b"ID3":
        return True
    return _mpeg_frame(head, 0) is not None


def _flac_duration(head: bytes) -> float | None:
    """FLAC states the exact total sample count, so this is not an estimate."""
    offset = 4
    while offset + 4 <= len(head):
        header = head[offset]
        last, kind = bool(header & 0x80), header & 0x7F
        length = int.from_bytes(head[offset + 1:offset + 4], "big")
        body = offset + 4
        if kind == 0:
            if length < 34 or body + 34 > len(head):
                return None
            packed = int.from_bytes(head[body + 10:body + 18], "big")
            sample_rate = packed >> 44
            total_samples = packed & ((1 << 36) - 1)
            if sample_rate <= 0 or total_samples <= 0:
                return None
            return total_samples / sample_rate
        if last:
            return None
        offset = body + length
    return None


def _mp3_duration(handle, start: int) -> float | None:
    """The playing time a frame chain really holds, read one frame at a time.

    Each frame adds its own sample count and rate, so a stream that changes
    rate part way through -- and one whose ``Xing`` count went stale because
    more audio was appended after the tag was written -- both measure the time
    they really play.  The tag is a cross-check rather than the answer: it only
    turns "short" into "unknown" when the bytes stopped before the count it
    claims, because a header this parser cannot follow is not evidence of a
    truncated recording, and reading it as one would refuse a file that is
    merely hard to read.
    """
    handle.seek(start)
    first = handle.read(MAX_MP3_HEAD_BYTES)
    frame = _mpeg_frame(first, 0)
    if frame is None:
        return None
    tagged = _xing_frame_count(first, 4 + frame[3])
    frames, elapsed, ran_out = _walk_mp3_frames(handle, start)
    if frames == 0:
        return None
    if tagged is not None and frames < tagged and not ran_out:
        return None
    return elapsed


def _mp3_stream_duration(data: bytes, start: int) -> float | None:
    """The same answer for bytes already in hand, and the shape the tests use."""
    return _mp3_duration(io.BytesIO(data), start)


def _walk_mp3_frames(handle, start: int) -> tuple[int, float, bool]:
    """Every whole frame from ``start``, as ``(frames, seconds, ran_out)``.

    A frame counts only when all of it is present, so a stream cut mid-frame
    reports only the frames a listener would really hear, and ``ran_out``
    tells a stream whose bytes simply ended apart from one holding a header
    this parser cannot read.  The stream is read in chunks and each frame's own
    rate is added as it goes, so the answer depends neither on the file fitting
    in memory nor on one frame's rate standing for the whole recording.
    """
    handle.seek(start)
    buffer, offset, frames, elapsed = b"", 0, 0, 0.0
    resyncs = 0
    while frames < MAX_MP3_FRAMES:
        if offset + 4 > len(buffer):
            chunk = handle.read(SCAN_CHUNK_BYTES)
            if not chunk:
                return frames, elapsed, True
            buffer, offset = buffer[offset:] + chunk, 0
            continue
        frame = _mpeg_frame(buffer, offset)
        if frame is None:
            # Bytes this parser does not read stand where a frame should be.
            # A stream that only ended finds no frame ahead and keeps the short
            # count it walked; one carrying a tag between its frames resumes at
            # the next frame and measures the audio it really holds.
            ahead = _resynchronize(handle, buffer, offset) if resyncs < MAX_MP3_RESYNCS else None
            if ahead is None:
                return frames, elapsed, False
            buffer, offset = ahead
            resyncs += 1
            continue
        samples, rate, length, _ = frame
        if offset + length > len(buffer):
            chunk = handle.read(SCAN_CHUNK_BYTES)
            if not chunk:
                return frames, elapsed, True
            buffer, offset = buffer[offset:] + chunk, 0
            continue
        offset += length
        frames += 1
        elapsed += samples / rate
    return frames, elapsed, False


def _resynchronize(handle, buffer: bytes, offset: int) -> tuple[bytes, int] | None:
    """Where the frames resume, or ``None`` when nothing ahead is one.

    The window is bounded, so a file full of bytes this parser cannot read
    costs one finite scan instead of a walk to its end.  A candidate only
    counts when the frame after it validates too, which is what keeps a stray
    ``0xFF`` inside compressed audio from reading as the start of a new stream
    and stretching a genuinely short file past the tolerance it is measured by.
    """
    window = buffer[offset:]
    while len(window) < RESYNC_SCAN_BYTES:
        chunk = handle.read(SCAN_CHUNK_BYTES)
        if not chunk:
            break
        window += chunk
    limit = min(len(window), RESYNC_SCAN_BYTES)
    at = 1
    while at + 4 <= limit:
        frame = _mpeg_frame(window, at)
        if frame is not None and _mpeg_frame(window, at + frame[2]) is not None:
            return window, at
        at += 1
    return None


def _mpeg_frame(data: bytes, offset: int) -> tuple[int, int, int, int] | None:
    """One Layer III frame header, as ``(samples, rate, length, side_info)``."""
    if offset < 0 or offset + 4 > len(data):
        return None
    header = data[offset:offset + 4]
    if header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version_bits = (header[1] >> 3) & 0x03
    if version_bits == 0b01 or (header[1] >> 1) & 0x03 != 0b01:  # reserved, or not Layer III
        return None
    version = {0b00: 2.5, 0b10: 2.0, 0b11: 1.0}[version_bits]
    bitrate_index = (header[2] >> 4) & 0x0F
    rate_index = (header[2] >> 2) & 0x03
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    rates = {1.0: _MPEG1_SAMPLE_RATES, 2.0: _MPEG2_SAMPLE_RATES, 2.5: _MPEG25_SAMPLE_RATES}[version]
    rate = rates[rate_index]
    bitrate = _LAYER3_BITRATES[1.0 if version == 1.0 else 2.0][bitrate_index]
    samples = _LAYER3_SAMPLES[1.0 if version == 1.0 else 2.0]
    padding = (header[2] >> 1) & 0x01
    length = (samples // 8) * bitrate * 1000 // rate + padding
    if length < 4:
        return None
    mono = (header[3] >> 6) & 0x03 == 0b11
    side_info = (17 if mono else 32) if version == 1.0 else (9 if mono else 17)
    return samples, rate, length, side_info


def _xing_frame_count(data: bytes, offset: int) -> int | None:
    if offset + 8 > len(data):
        return None
    if data[offset:offset + 4] not in (b"Xing", b"Info"):
        return None
    flags = int.from_bytes(data[offset + 4:offset + 8], "big")
    if not flags & 0x0001 or offset + 12 > len(data):
        return None
    frames = int.from_bytes(data[offset + 8:offset + 12], "big")
    return frames or None


def _id3_size(data: bytes) -> int | None:
    """Where the audio starts, after an ``ID3v2`` tag when there is one."""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0 if _mpeg_frame(data, 0) is not None else None
    if any(byte & 0x80 for byte in data[6:10]):
        return None
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | byte
    footer = 10 if data[5] & 0x10 else 0
    return 10 + size + footer


def _mp4_file_duration(handle, size: int) -> float | None:
    offset = 0
    while offset + 8 <= size:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            return None
        box_size = int.from_bytes(header[:4], "big")
        kind = header[4:8]
        body = offset + 8
        if box_size == 1:
            extra = handle.read(8)
            if len(extra) < 8:
                return None
            box_size = int.from_bytes(extra, "big")
            body += 8
        elif box_size == 0:
            box_size = size - offset
        if box_size < body - offset or offset + box_size > size:
            return None
        if kind == b"moov":
            if box_size > MAX_MOOV_BYTES:
                return None
            handle.seek(body)
            payload = handle.read(box_size - (body - offset))
            mvhd = _find_box(payload, 0, len(payload), b"mvhd")
            return None if mvhd is None else _read_mvhd(payload[mvhd[0]:mvhd[1]])
        offset += box_size
    return None


def _find_box(data: bytes, start: int, end: int, kind: bytes, *, limit: int | None = None):
    """The body of the first ``kind`` box inside ``data[start:end]``."""
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset:offset + 4], "big")
        found = data[offset + 4:offset + 8]
        body = offset + 8
        if size == 1:
            if body + 8 > end:
                return None
            size = int.from_bytes(data[body:body + 8], "big")
            body += 8
        elif size == 0:
            size = end - offset
        if size < body - offset or offset + size > end:
            return None
        if found == kind:
            if limit is not None and size > limit:
                return None
            return body, offset + size
        offset += size
    return None


def _read_mvhd(payload: bytes) -> float | None:
    if not payload:
        return None
    version = payload[0]
    if version == 1:
        if len(payload) < 32:
            return None
        timescale = int.from_bytes(payload[20:24], "big")
        duration = int.from_bytes(payload[24:32], "big")
    elif version == 0:
        if len(payload) < 20:
            return None
        timescale = int.from_bytes(payload[12:16], "big")
        duration = int.from_bytes(payload[16:20], "big")
    else:
        return None
    if timescale <= 0 or duration <= 0:
        return None
    if duration == (0xFFFFFFFF if version == 0 else 0xFFFFFFFFFFFFFFFF):
        # The format writes this when the length is not known up front (a live
        # or fragmented recording).  "Unknown" is the honest answer, and it
        # keeps such a file from being refused for a number it never claimed.
        return None
    return duration / timescale
