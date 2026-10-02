"""Measure a finished audio file and conservatively verify supported streams.

The parsers stay plain Python: no ffmpeg, no ffprobe, no native decoder, and
no new dependency. A file this module cannot understand answers ``None`` for
duration and ``unverified`` for stream integrity, because bytes nobody here
recognises are not evidence of anything -- the caller decides what that means.

What is measured is what the file really holds, not what its headers claim:
a 30-second preview or a stream cut off mid-way measures short, which is the
whole point of asking.
"""

from __future__ import annotations

import io
import mmap
import os
import time
from dataclasses import dataclass
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
STREAM_SCAN_TIMEOUT_SECONDS: Final[float] = 5.0
MAX_FLAC_METADATA_BYTES: Final[int] = 64 * 1024 * 1024

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


@dataclass(frozen=True)
class StreamIntegrity:
    """A conservative answer about whether a container holds its declared media."""

    format: str | None
    status: str


def verify_stream_integrity(
    path: str | os.PathLike[str],
    *,
    expected_duration: float | None = None,
    timeout_seconds: float = STREAM_SCAN_TIMEOUT_SECONDS,
) -> StreamIntegrity:
    """Check FLAC frames or MP4 sample sizes against bytes actually in the file.

    ``complete`` and ``incomplete`` are returned only when the relevant
    container declarations and the entire required scan are trustworthy.
    Unsupported shapes, timeouts, and damaged metadata return ``unverified``.
    MP3 stays on its existing frame-chain path in :func:`duration_seconds`.
    """
    file_format: str | None = None
    try:
        with open(os.fspath(path), "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size <= 0:
                return StreamIntegrity(None, "unverified")
            head = handle.read(min(HEAD_BYTES, size))
            if _has_flac_magic(head):
                file_format = "flac"
            elif head[:3] == b"ID3" and len(head) >= 10:
                start = _id3_size(head[:10])
                if start is not None:
                    handle.seek(start)
                    if handle.read(4) == b"fLaC":
                        file_format = "flac"
                    else:
                        file_format = "mp4" if _is_mp4(head) else None
            elif _is_mp4(head):
                file_format = "mp4"
            else:
                return StreamIntegrity(None, "unverified")
            if file_format is None:
                return StreamIntegrity(None, "unverified")
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            if time.monotonic() >= deadline:
                return StreamIntegrity(file_format, "unverified")
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if file_format == "flac":
                    status, _reason = _verify_flac_integrity(data, deadline)
                else:
                    status = _verify_mp4_integrity(data, deadline, expected_duration)
            return StreamIntegrity(file_format, status)
    except Exception:  # noqa: BLE001 - an uncertain parser or I/O result fails open
        return StreamIntegrity(file_format, "unverified")


def _has_flac_magic(data) -> bool:
    if data[:4] == b"fLaC":
        return True
    if data[:3] != b"ID3" or len(data) < 10:
        return False
    start = _id3_size(data[:10])
    return start is not None and data[start:start + 4] == b"fLaC"


def _verify_flac_integrity(data, deadline: float) -> tuple[str, str | None]:
    """Return a FLAC status and a private reason when it is unverified.

    The reason is diagnostic evidence for the conservative parser. The public
    ``StreamIntegrity`` shape deliberately stays unchanged.
    """
    start = 0
    if data[:3] == b"ID3":
        start = _id3_size(data[:10])
        if start is None:
            return "unverified", "invalid_id3v2_prefix"
    if data[start:start + 4] != b"fLaC":
        return "unverified", "flac_magic_missing"

    offset = start + 4
    streaminfo: tuple[int, int, int, int, int, int] | None = None
    metadata_start = offset
    metadata_blocks = 0
    while offset + 4 <= len(data):
        if time.monotonic() >= deadline:
            return "unverified", "metadata_scan_timeout"
        block_header = data[offset]
        last = bool(block_header & 0x80)
        kind = block_header & 0x7F
        length = int.from_bytes(data[offset + 1:offset + 4], "big")
        body = offset + 4
        block_end = body + length
        if block_end > len(data) or block_end - metadata_start > MAX_FLAC_METADATA_BYTES:
            return "unverified", "metadata_block_out_of_bounds_or_limit"
        if metadata_blocks == 0 and (kind != 0 or length != 34):
            return "unverified", "streaminfo_not_first_metadata_block"
        if kind == 0:
            if length != 34 or streaminfo is not None:
                return "unverified", "invalid_or_duplicate_streaminfo"
            packed = int.from_bytes(data[body + 10:body + 18], "big")
            sample_rate = packed >> 44
            channels = ((packed >> 41) & 0x07) + 1
            bits_per_sample = ((packed >> 36) & 0x1F) + 1
            total_samples = packed & ((1 << 36) - 1)
            min_block = int.from_bytes(data[body:body + 2], "big")
            max_block = int.from_bytes(data[body + 2:body + 4], "big")
            if min_block <= 0 or max_block < min_block:
                return "unverified", "invalid_streaminfo_block_sizes"
            streaminfo = sample_rate, total_samples, channels, bits_per_sample, min_block, max_block
        metadata_blocks += 1
        offset = block_end
        if last:
            break
    else:
        return "unverified", "metadata_missing_last_block_marker"

    if streaminfo is None or streaminfo[0] <= 0 or streaminfo[1] <= 0:
        return "unverified", "missing_or_invalid_streaminfo_sample_count"
    sample_rate, total_samples, channels, bits_per_sample, min_block, max_block = streaminfo
    frame_start = offset
    frame_end = len(data)
    if frame_end - frame_start >= 128 and data[frame_end - 128:frame_end - 125] == b"TAG":
        # ID3v1 is a fixed 128-byte suffix after the FLAC frame chain.  Its
        # marker and position make it distinguishable from bytes in a frame;
        # arbitrary trailing data still has to fail the terminal frame CRC.
        frame_end -= 128
    accepted = 0
    actual_samples = 0
    blocking_strategy: int | None = None
    previous_number: int | None = None
    previous_block_samples: int | None = None
    final_frame_start: int | None = None
    final_frame_payload_start: int | None = None
    position = frame_start
    candidates = 0
    while position + 1 < frame_end:
        if time.monotonic() >= deadline:
            return "unverified", "frame_scan_timeout"
        scan_end = min(frame_end, position + SCAN_CHUNK_BYTES)
        candidate = data.find(b"\xff", position, scan_end)
        if candidate < 0:
            position = scan_end
            continue
        position = candidate + 1
        if data[candidate + 1] not in (0xF8, 0xF9):
            continue
        candidates += 1
        if candidates % 256 == 0 and time.monotonic() >= deadline:
            return "unverified", "frame_scan_timeout_after_candidates"
        parsed = _flac_frame_header(data, candidate, sample_rate, channels, bits_per_sample)
        if parsed is None:
            continue
        strategy, number, block_samples, crc_valid, payload_start = parsed
        if not crc_valid:
            if accepted and final_frame_start is not None and final_frame_payload_start is not None:
                boundary_valid, boundary_reason = _flac_terminal_frame_crc_diagnostic(
                    data, final_frame_start, final_frame_payload_start, candidate, deadline)
                if boundary_valid:
                    return "unverified", "invalid_frame_header_at_validated_boundary"
                if boundary_reason != "terminal_frame_crc_mismatch":
                    return "unverified", f"frame_candidate_boundary_{boundary_reason}"
            # A header-shaped sequence inside compressed frame data is not a
            # frame unless its CRC-8 is valid.  If it cannot be the boundary
            # after the preceding frame either, keep scanning for the next one.
            continue
        if accepted == 0:
            if number != 0:
                continue
            blocking_strategy = strategy
        else:
            if strategy != blocking_strategy:
                return "unverified", (
                    f"blocking_strategy_changed:offset={candidate}:header_crc_valid={crc_valid}"
                )
            expected_number = (previous_number + 1 if strategy == 0
                               else previous_number + previous_block_samples)
            if number != expected_number:
                if number > expected_number:
                    # A trusted but non-contiguous frame means the parser did
                    # not account for every frame, so a short count is not proof.
                    return "unverified", "frame_number_gap"
                continue
            # A CRC-8-valid, contiguous header establishes the previous frame's
            # end. Check that frame before accepting the next one: checking only
            # the terminal CRC could otherwise hide corruption in earlier audio.
            if final_frame_start is None or final_frame_payload_start is None:
                return "unverified", "previous_frame_bounds_missing"
            previous_crc_valid, previous_crc_reason = _flac_terminal_frame_crc_diagnostic(
                data, final_frame_start, final_frame_payload_start, candidate, deadline)
            if not previous_crc_valid:
                return "unverified", (
                    f"previous_frame_{previous_crc_reason}:frame_start={final_frame_start}"
                    f":frame_end={candidate}"
                )
            if previous_block_samples is not None and previous_block_samples < min_block:
                return "unverified", "previous_frame_block_below_streaminfo_min"
        if block_samples > max_block:
            return "unverified", "frame_block_exceeds_streaminfo_max"
        accepted += 1
        actual_samples += block_samples
        previous_number, previous_block_samples = number, block_samples
        final_frame_start = candidate
        final_frame_payload_start = payload_start

    if time.monotonic() >= deadline:
        return "unverified", "frame_scan_timeout_at_eof"
    if accepted == 0:
        return "unverified", "no_valid_frames"
    claimed_duration = total_samples / sample_rate
    short_tolerance, _ = duration_tolerance(claimed_duration)
    missing_samples = total_samples - actual_samples
    if missing_samples < 0:
        return "unverified", "frame_samples_exceed_streaminfo"
    if missing_samples > short_tolerance * sample_rate:
        return "incomplete", None
    # Header CRC-8 only proves that the frame's declaration is intact. It says
    # nothing about the subframes or the trailing frame CRC-16. Treat EOF as
    # the terminal boundary and require a non-empty payload plus its checksum
    # to match before its declared samples can count as present.
    if final_frame_start is None or final_frame_payload_start is None:
        return "unverified", "terminal_frame_bounds_missing"
    crc_valid, crc_reason = _flac_terminal_frame_crc_diagnostic(
        data, final_frame_start, final_frame_payload_start, frame_end, deadline)
    if not crc_valid:
        if crc_reason == "terminal_frame_crc_mismatch":
            crc_reason = (f"{crc_reason}:frame_start={final_frame_start}:frame_end={frame_end}"
                          f":actual_samples={actual_samples}:total_samples={total_samples}")
        return "unverified", crc_reason
    return "complete", None


def _flac_frame_header(data, offset: int, stream_rate: int, stream_channels: int,
                       stream_bits: int) -> tuple[int, int, int, bool, int] | None:
    """Parse a FLAC frame header and verify its CRC-8 at its variable offset."""
    if offset + 6 > len(data) or data[offset] != 0xFF or data[offset + 1] not in (0xF8, 0xF9):
        return None
    strategy = data[offset + 1] & 0x01
    block_code, rate_code = data[offset + 2] >> 4, data[offset + 2] & 0x0F
    channel_assignment, sample_size_code, reserved = data[offset + 3] >> 4, (data[offset + 3] >> 1) & 0x07, data[offset + 3] & 1
    if (block_code == 0 or rate_code == 15
            or channel_assignment > 10 or sample_size_code == 3 or reserved):
        return None
    frame_channels = channel_assignment + 1 if channel_assignment <= 7 else 2
    frame_bits = {0: stream_bits, 1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}[sample_size_code]
    if frame_channels != stream_channels or frame_bits != stream_bits:
        return None
    number = _flac_utf8_number_at(data, offset + 4)
    if number is None:
        return None
    frame_number, number_length = number
    cursor = offset + 4 + number_length
    if block_code in (6, 7):
        extra = 1 if block_code == 6 else 2
        if cursor + extra > len(data):
            return None
        block_samples = int.from_bytes(data[cursor:cursor + extra], "big") + 1
        cursor += extra
    elif block_code == 1:
        block_samples = 192
    elif block_code <= 5:
        block_samples = 576 << (block_code - 2)
    else:
        block_samples = 256 << (block_code - 8)
    if rate_code == 0:
        frame_rate = stream_rate
    elif 1 <= rate_code <= 11:
        frame_rate = (88200, 176400, 192000, 8000, 16000, 22050,
                      24000, 32000, 44100, 48000, 96000)[rate_code - 1]
    elif rate_code == 12:
        if cursor >= len(data):
            return None
        frame_rate = data[cursor] * 1000
        cursor += 1
    elif rate_code in (13, 14):
        if cursor + 2 > len(data):
            return None
        frame_rate = int.from_bytes(data[cursor:cursor + 2], "big")
        if rate_code == 14:
            frame_rate *= 10
        cursor += 2
    else:
        return None
    if frame_rate != stream_rate:
        return None
    if cursor >= len(data):
        return None
    header_crc = data[cursor]
    return strategy, frame_number, block_samples, _flac_crc8(data[offset:cursor]) == header_crc, cursor + 1


def _flac_terminal_frame_crc_valid(data, frame_start: int, payload_start: int,
                                   frame_end: int, deadline: float) -> bool:
    """Validate the last frame's payload and CRC-16 at the physical EOF."""
    return _flac_terminal_frame_crc_diagnostic(
        data, frame_start, payload_start, frame_end, deadline)[0]


def _flac_terminal_frame_crc_diagnostic(data, frame_start: int, payload_start: int,
                                        frame_end: int, deadline: float) -> tuple[bool, str | None]:
    """Validate the terminal frame and distinguish a timeout from bad CRC."""
    checksum_start = frame_end - 2
    if payload_start >= checksum_start:
        return False, "terminal_frame_payload_empty"
    crc = 0
    table3, table2, table1, table0 = _FLAC_CRC16_SLICE_TABLES
    chunk_start = frame_start
    while chunk_start < checksum_start:
        if time.monotonic() >= deadline:
            return False, "terminal_frame_crc_timeout"
        chunk_end = min(checksum_start, chunk_start + 65536)
        chunk = data[chunk_start:chunk_end]
        bulk_end = len(chunk) - (len(chunk) % 4)
        for offset in range(0, bulk_end, 4):
            crc = (table3[(crc >> 8) ^ chunk[offset]]
                   ^ table2[(crc & 0xFF) ^ chunk[offset + 1]]
                   ^ table1[chunk[offset + 2]]
                   ^ table0[chunk[offset + 3]])
        for byte in chunk[bulk_end:]:
            crc = table0[(crc >> 8) ^ byte] ^ ((crc & 0xFF) << 8)
        chunk_start = chunk_end
    if crc != int.from_bytes(data[checksum_start:frame_end], "big"):
        return False, "terminal_frame_crc_mismatch"
    return True, None


def _flac_crc16_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_FLAC_CRC16_TABLE: Final[tuple[int, ...]] = _flac_crc16_table()


def _flac_crc16_slice_tables() -> tuple[tuple[int, ...], ...]:
    """Build four-byte slicing tables for FLAC's CRC-16 polynomial."""
    base = _FLAC_CRC16_TABLE

    def advance(crc: int, byte: int) -> int:
        return base[(crc >> 8) ^ byte] ^ ((crc & 0xFF) << 8)

    one_zero = tuple(advance(value, 0) for value in base)
    two_zeros = tuple(advance(value, 0) for value in one_zero)
    three_zeros = tuple(advance(value, 0) for value in two_zeros)
    return three_zeros, two_zeros, one_zero, base


_FLAC_CRC16_SLICE_TABLES: Final = _flac_crc16_slice_tables()


def _flac_utf8_number_at(data, offset: int) -> tuple[int, int] | None:
    if offset >= len(data):
        return None
    first = data[offset]
    if first < 0x80:
        return first, 1
    mask, length = 0x80, 0
    while first & mask:
        length += 1
        mask >>= 1
    if length < 2 or length > 7 or offset + length > len(data):
        return None
    value = first & ((1 << (7 - length)) - 1)
    for index in range(1, length):
        byte = data[offset + index]
        if byte & 0xC0 != 0x80:
            return None
        value = (value << 6) | (byte & 0x3F)
    minimum = (0x80, 0x800, 0x10000, 0x200000, 0x4000000, 0x80000000)[length - 2]
    if value < minimum:
        return None
    return value, length


def _flac_crc8(data) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _verify_mp4_integrity(data, deadline: float, expected_duration: float | None) -> str:
    offset, mdat_bytes, moov = 0, 0, None
    truncated_mdat = False
    while offset < len(data):
        if time.monotonic() >= deadline or offset + 8 > len(data):
            return "unverified"
        box = _mp4_box_at(data, offset, len(data))
        if box is None:
            return "unverified"
        kind, body, end, declared_end = box
        if kind == b"moof":
            return "unverified"
        if kind == b"moov":
            if end - offset > MAX_MOOV_BYTES or declared_end > len(data):
                return "unverified"
            moov = (body, end)
        elif kind == b"mdat":
            mdat_bytes += max(0, end - body)
            if declared_end > len(data):
                # The final mdat header itself proves that the physical file
                # ended before its declared payload did.
                truncated_mdat = True
                offset = len(data)
                break
        offset = end
    if offset != len(data) or moov is None or mdat_bytes <= 0:
        return "unverified"
    # A declared mdat that runs beyond physical EOF makes the box chain
    # incomplete even when its available bytes happen to cover the sample
    # table. Do not call that complete based on byte counts alone.
    if truncated_mdat:
        return "unverified"
    parsed = _mp4_audio_sample_bytes(data, *moov, deadline)
    if parsed is None:
        return "unverified"
    sample_bytes = parsed
    if sample_bytes <= 0:
        return "unverified"
    if mdat_bytes >= sample_bytes:
        return "complete"
    duration = expected_duration
    if duration is None or duration <= 0:
        duration = _mp4_movie_duration(data, *moov, deadline)
    if duration is None or duration <= 0:
        return "unverified"
    short_tolerance, _ = duration_tolerance(duration)
    allowed_missing = sample_bytes * short_tolerance / duration
    if sample_bytes - mdat_bytes > allowed_missing:
        return "incomplete"
    return "complete"


def _mp4_movie_duration(data, start: int, end: int, deadline: float) -> float | None:
    """Read mvhd duration from an already bounded, complete moov box."""
    children = _mp4_children(data, start, end, deadline)
    if children is None:
        return None
    mvhd = next(((body, child_end) for kind, body, child_end in children if kind == b"mvhd"), None)
    if mvhd is None:
        return None
    return _read_mvhd(data[mvhd[0]:mvhd[1]])


def _mp4_box_at(data, offset: int, parent_end: int):
    if offset + 8 > parent_end:
        return None
    size = int.from_bytes(data[offset:offset + 4], "big")
    kind = data[offset + 4:offset + 8]
    body = offset + 8
    if size == 1:
        if body + 8 > parent_end:
            return None
        size = int.from_bytes(data[body:body + 8], "big")
        body += 8
    elif size == 0:
        size = parent_end - offset
    declared_end = offset + size
    if size < body - offset:
        return None
    return kind, body, min(declared_end, parent_end), declared_end


def _mp4_children(data, start: int, end: int, deadline: float):
    children = []
    offset = start
    while offset < end:
        if time.monotonic() >= deadline:
            return None
        box = _mp4_box_at(data, offset, end)
        if box is None or box[3] > end:
            return None
        kind, body, child_end, _ = box
        children.append((kind, body, child_end))
        offset = child_end
    return children if offset == end else None


def _mp4_audio_sample_bytes(data, start: int, end: int, deadline: float):
    children = _mp4_children(data, start, end, deadline)
    if children is None:
        return None
    total_sample_bytes = 0
    audio_tracks = 0
    for kind, body, child_end in children:
        if kind == b"trak":
            track = _mp4_track_sample_bytes(data, body, child_end, deadline)
            if track is None:
                return None
            is_audio, sample_bytes = track
            if is_audio:
                total_sample_bytes += sample_bytes
                audio_tracks += 1
    if audio_tracks == 0:
        return None
    return total_sample_bytes


def _mp4_track_sample_bytes(data, start: int, end: int, deadline: float):
    track_boxes = _mp4_children(data, start, end, deadline)
    if track_boxes is None:
        return None
    mdia = next(((body, child_end) for kind, body, child_end in track_boxes if kind == b"mdia"), None)
    if mdia is None:
        return False, 0
    mdia_boxes = _mp4_children(data, *mdia, deadline)
    if mdia_boxes is None:
        return None
    handler = next((data[body + 8:body + 12] for kind, body, child_end in mdia_boxes
                    if kind == b"hdlr" and child_end - body >= 12), None)
    if handler is None:
        return None
    if handler != b"soun":
        return False, 0
    minf = next(((body, child_end) for kind, body, child_end in mdia_boxes if kind == b"minf"), None)
    if minf is None:
        return None
    minf_boxes = _mp4_children(data, *minf, deadline)
    if minf_boxes is None:
        return None
    stbl = next(((body, child_end) for kind, body, child_end in minf_boxes if kind == b"stbl"), None)
    if stbl is None:
        return None
    stbl_boxes = _mp4_children(data, *stbl, deadline)
    if stbl_boxes is None:
        return None
    tables = [(kind, body, child_end) for kind, body, child_end in stbl_boxes
              if kind in (b"stsz", b"stz2")]
    if len(tables) != 1:
        return None
    return True, _mp4_sample_table_sum(data, *tables[0], deadline)


def _mp4_sample_table_sum(data, kind: bytes, start: int, end: int, deadline: float) -> int | None:
    payload_length = end - start
    if kind == b"stsz":
        if payload_length < 12 or data[start:start + 4] != bytes(4):
            return None
        sample_size = int.from_bytes(data[start + 4:start + 8], "big")
        sample_count = int.from_bytes(data[start + 8:start + 12], "big")
        if sample_count <= 0:
            return None
        if sample_size:
            if payload_length != 12:
                return None
            return sample_size * sample_count
        table_start = start + 12
        if table_start + sample_count * 4 != end:
            return None
        total = 0
        for index in range(sample_count):
            if index % 8192 == 0 and time.monotonic() >= deadline:
                return None
            at = table_start + index * 4
            total += int.from_bytes(data[at:at + 4], "big")
        return total
    if payload_length < 12 or data[start:start + 4] != bytes(4) or data[start + 4:start + 7] != bytes(3):
        return None
    field_size = data[start + 7]
    sample_count = int.from_bytes(data[start + 8:start + 12], "big")
    table_start = start + 12
    if sample_count <= 0:
        return None
    if field_size == 4:
        byte_count = (sample_count + 1) // 2
        if table_start + byte_count != end:
            return None
        total = 0
        for index in range(byte_count):
            if index % 16384 == 0 and time.monotonic() >= deadline:
                return None
            packed = data[table_start + index]
            total += packed >> 4
            if index * 2 + 1 < sample_count:
                total += packed & 0x0F
        return total
    if field_size not in (8, 16):
        return None
    width = field_size // 8
    if table_start + sample_count * width != end:
        return None
    total = 0
    for index in range(sample_count):
        if index % 8192 == 0 and time.monotonic() >= deadline:
            return None
        at = table_start + index * width
        total += int.from_bytes(data[at:at + width], "big")
    return total


def duration_seconds(path: str | os.PathLike[str]) -> float | None:
    """The playing time one file really holds, or ``None`` when it is unknown."""
    try:
        with open(os.fspath(path), "rb") as handle:
            head = handle.read(HEAD_BYTES)
            flac_start = _flac_start(head)
            if flac_start is None and head[:3] == b"ID3" and len(head) >= 10:
                possible_start = _id3_size(head[:10])
                if possible_start is not None:
                    handle.seek(possible_start)
                    if handle.read(4) == b"fLaC":
                        handle.seek(possible_start)
                        head = handle.read(HEAD_BYTES)
                        flac_start = 0
            if flac_start is not None:
                if flac_start >= len(head):
                    handle.seek(flac_start)
                    head = handle.read(HEAD_BYTES)
                    flac_start = 0
                return _flac_duration(head[flac_start:])
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
        flac_start = _flac_start(data)
        if flac_start is not None:
            return _flac_duration(data[flac_start:])
        if _is_mp4(data):
            return _mp4_duration(data)
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


def _flac_start(data: bytes) -> int | None:
    if data[:4] == b"fLaC":
        return 0
    if data[:3] == b"ID3" and len(data) >= 10:
        start = _id3_size(data[:10])
        if start is not None and data[start:start + 4] == b"fLaC":
            return start
    return None


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
    merely hard to read.  A walk that stops at bytes this parser cannot read
    and finds no tag behind them answers "unknown" rather than the count so
    far: the audio may continue past that stretch, and a lower bound reported
    as a length refuses a recording that is merely hard to read.
    """
    handle.seek(start)
    first = handle.read(MAX_MP3_HEAD_BYTES)
    frame = _mpeg_frame(first, 0)
    if frame is None:
        return None
    tagged = _xing_frame_count(first, 4 + frame[3])
    frames, elapsed, ran_out, trailer = _walk_mp3_frames(handle, start)
    if frames == 0:
        return None
    if ran_out:
        # The bytes ended, so the frames that arrived are the recording; a
        # ``Xing`` count larger than them is the encoder's stale or truncated
        # claim rather than a longer song.
        return elapsed
    if not trailer:
        # The walk stopped at bytes that are not a tag, so the stream may hold
        # more audio past them and the count so far is a lower bound.  A lower
        # bound reported as a length refuses a recording that is merely hard to
        # read, so the answer is "unknown" instead.
        return None
    if tagged is not None and frames < tagged:
        # The tail is a tag, so the audio really did end there, but the
        # encoder's own count says more frames were written than arrived.  The
        # two witnesses disagree, so neither of them is a measurement.
        return None
    return elapsed


def _mp3_stream_duration(data: bytes, start: int) -> float | None:
    """The same answer for bytes already in hand, and the shape the tests use."""
    return _mp3_duration(io.BytesIO(data), start)


def _walk_mp3_frames(handle, start: int) -> tuple[int, float, bool]:
    """Every whole frame from ``start``, as ``(frames, seconds, ran_out, trailer)``.

    A frame counts only when all of it is present, so a stream cut mid-frame
    reports only the frames a listener would really hear, and ``ran_out``
    tells a stream whose bytes simply ended apart from one holding a header
    this parser cannot read.  ``trailer`` says whether the bytes that stopped
    the walk are a tag running to the end of the file, which is the one way a
    stop this parser caused still leaves the count complete.  The stream is
    read in chunks and each frame's own rate is added as it goes, so the answer
    depends neither on the file fitting in memory nor on one frame's rate
    standing for the whole recording.
    """
    handle.seek(start)
    # ``base`` is where ``buffer[0]`` really sits in the file.  Looking for
    # frames reads ahead of the buffer, so the buffer's end is not the file's
    # end: the stop's real position is carried along rather than read back out
    # of whichever buffer happens to be in hand.
    buffer, offset, base, frames, elapsed = b"", 0, start, 0, 0.0
    resyncs = 0
    while frames < MAX_MP3_FRAMES:
        if offset + 4 > len(buffer):
            chunk = handle.read(SCAN_CHUNK_BYTES)
            if not chunk:
                return frames, elapsed, True, False
            base, buffer, offset = base + offset, buffer[offset:] + chunk, 0
            continue
        frame = _mpeg_frame(buffer, offset)
        if frame is None:
            # Bytes this parser does not read stand where a frame should be.
            # A stream that only ended finds no frame ahead and keeps the short
            # count it walked; one carrying a tag between its frames resumes at
            # the next frame and measures the audio it really holds.
            ahead = _resynchronize(handle, buffer, offset) if resyncs < MAX_MP3_RESYNCS else None
            if ahead is None:
                return frames, elapsed, False, _trailing_tag(handle, buffer, offset, base + offset)
            base, buffer, offset = base + offset, ahead[0], ahead[1]
            resyncs += 1
            continue
        samples, rate, length, _ = frame
        if offset + length > len(buffer):
            chunk = handle.read(SCAN_CHUNK_BYTES)
            if not chunk:
                return frames, elapsed, True, False
            base, buffer, offset = base + offset, buffer[offset:] + chunk, 0
            continue
        offset += length
        frames += 1
        elapsed += samples / rate
    return frames, elapsed, False, False


def _trailing_tag(handle, buffer: bytes, offset: int, position: int) -> bool:
    """Whether everything from ``position`` to the end of the file is a tag.

    A tag written after the audio is not audio, so a walk that stopped at one
    has still counted the whole recording.  Only the shapes that really end a
    file count: an ``ID3v1`` block, and an ``ID3v2`` tag whose own length runs
    exactly to the end.  Measured 2026-09-28 by independent review, a 600 kB
    ``ID3v2`` block written between two stretches of audio is longer than the
    resynchronisation window; its header sits where the walk stopped, it does
    not reach the end of the file, and the frames behind it must not be read as
    a shorter song.

    ``position`` is where ``buffer[offset]`` really sits.  Looking for frames
    reads ahead of the buffer, so ``len(buffer) - offset`` is the distance to the
    buffer's end and not to the file's: measuring from there read a 600 kB
    trailing tag as the 238 bytes that happened to be left in the buffer, and
    answered "unknown" for a recording whose every frame had been counted.
    """
    handle.seek(0, os.SEEK_END)
    end = handle.tell()
    remaining = end - position
    if remaining <= 0:
        return True
    head = buffer[offset:offset + 10]
    if len(head) < 10:
        handle.seek(position)
        head = handle.read(10)
    if len(head) < 3:
        return False
    if remaining == 128 and head[:3] == b"TAG":
        return True
    return len(head) == 10 and head[:3] == b"ID3" and _id3_size(head) == remaining


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
    """``mvhd`` for a file whose whole box chain fits inside the bytes it has.

    Every box is walked rather than stopping at ``moov``: a faststart file
    leads with ``moov`` and keeps its audio in the ``mdat`` behind it, so a
    recording cut in the middle keeps a header that states its whole length
    while the bytes it states are no longer there.  A box reaching past the
    end of the file is that cut, and the length the header claims is not the
    length the file plays.
    """
    offset, duration = 0, None
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
        if kind == b"moov" and duration is None:
            if box_size > MAX_MOOV_BYTES:
                return None
            handle.seek(body)
            payload = handle.read(box_size - (body - offset))
            mvhd = _find_box(payload, 0, len(payload), b"mvhd")
            if mvhd is None:
                return None
            duration = _read_mvhd(payload[mvhd[0]:mvhd[1]])
            if duration is None:
                return None
        offset += box_size
    # Bytes too few to be a box are a chain that stopped, not a measurement: a
    # cut can land inside the header of the box that never got to state its size,
    # and the length a surviving header claims is not the length the file holds.
    if offset != size:
        return None
    return duration


def _mp4_duration(data: bytes) -> float | None:
    """The same rule for bytes already in hand, and the shape the tests use."""
    offset, duration = 0, None
    while offset + 8 <= len(data):
        box_size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        body = offset + 8
        if box_size == 1:
            if body + 8 > len(data):
                return None
            box_size = int.from_bytes(data[body:body + 8], "big")
            body += 8
        elif box_size == 0:
            box_size = len(data) - offset
        if box_size < body - offset or offset + box_size > len(data):
            return None
        if kind == b"moov" and duration is None:
            if box_size > MAX_MOOV_BYTES:
                return None
            mvhd = _find_box(data, body, offset + box_size, b"mvhd")
            if mvhd is None:
                return None
            duration = _read_mvhd(data[mvhd[0]:mvhd[1]])
            if duration is None:
                return None
        offset += box_size
    if offset != len(data):
        return None
    return duration


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
