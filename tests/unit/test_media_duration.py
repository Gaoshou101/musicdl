"""Real playing time, read out of a finished file, and what it refuses.

Most samples are built by hand so their container declarations stay explicit.
A compact, checked-in FFmpeg FLAC fixture exercises the integrity path against
real encoder output without requiring FFmpeg when the test suite runs.

FLAC and MP4 fixtures include frame headers and sample tables so the download
policy can compare what those structures claim with the bytes actually held.
MP3 fixtures stay focused on frame-chain measurement and its existing limits.
"""

import asyncio
import hashlib
from pathlib import Path

import pytest

from musicdl.media import DownloadMetadata, MediaError, download_candidate
from musicdl.media.duration import (
    RESYNC_SCAN_BYTES,
    SCAN_CHUNK_BYTES,
    bitrate_kbps,
    container_duration,
    duration_matches,
    duration_seconds,
    duration_tolerance,
)
from musicdl.media.models import ArtifactRecord, _CloseOnce
from musicdl.sources.models import Candidate

FLAC_RATE = 44100
MP3_FRAME_LENGTH = 144 * 128 * 1000 // 44100  # MPEG-1 Layer III, 128 kbps, 44.1 kHz
MP3_FRAME_SAMPLES = 1152
# Generated once with:
# `ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 30 -c:a flac \
#   -compression_level 5 -metadata_header_padding 0 -y \
#   tests/fixtures/media/ffmpeg_30s.flac`
ENCODER_FLAC_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "media" / "ffmpeg_30s.flac"
)


def _encoder_flac_bytes() -> bytes:
    """Read the small 30-second encoder-produced FLAC fixture."""
    return ENCODER_FLAC_FIXTURE.read_bytes()


def _flac_crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _flac_utf8_number(value: int) -> bytes:
    if value < 0x80:
        return bytes((value,))
    length = 2
    while value > (1 << (5 * length + 1)) - 1:
        length += 1
    out = bytearray(length)
    for index in range(length - 1, 0, -1):
        out[index] = 0x80 | (value & 0x3F)
        value >>= 6
    out[0] = ((0xFF << (8 - length)) & 0xFF) | value
    return bytes(out)


def _flac_crc16_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_FLAC_CRC16_TABLE = _flac_crc16_table()


def _flac_crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _FLAC_CRC16_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


def flac_frame(number: int, samples: int, *, channels: int = 1, bits_per_sample: int = 8,
               verbatim: bool = False) -> bytes:
    """A FLAC frame containing constant zero samples and valid header/frame CRCs."""
    assignment = channels - 1 if channels <= 8 else 10
    header = b"\xff\xf8\x70" + bytes((assignment << 4,)) + _flac_utf8_number(number) + (samples - 1).to_bytes(2, "big")
    header += bytes((_flac_crc8(header),))
    sample_width = (bits_per_sample + 7) // 8
    subframe = (b"\x02" + bytes(sample_width * samples) if verbatim
                else b"\x00" + bytes(sample_width))
    subframes = subframe * channels
    frame = header + subframes
    return frame + _flac_crc16(frame).to_bytes(2, "big")


def flac_bytes(total_samples: int, rate: int = FLAC_RATE, *, channels: int = 1,
               bits_per_sample: int = 8, verbatim: bool = False,
               padding_bytes: int = 0) -> bytes:
    """A valid silence-only FLAC with a chosen sample count and optional padding."""
    packed = (rate << 44) | total_samples
    packed |= (channels - 1) << 41 | (bits_per_sample - 1) << 36
    min_block = min(4096, total_samples or 4096)
    body = min_block.to_bytes(2, "big") + (4096).to_bytes(2, "big") + b"\x00" * 6
    body += packed.to_bytes(8, "big") + b"\x00" * 16
    assert len(body) == 34
    metadata_blocks = [bytes((0x80 if padding_bytes == 0 else 0,))
                       + len(body).to_bytes(3, "big") + body]
    remaining_padding = padding_bytes
    while remaining_padding > 0:
        length = min(0xFFFFFF, remaining_padding)
        remaining_padding -= length
        metadata_blocks.append(bytes((0x81 if remaining_padding == 0 else 1,))
                                + length.to_bytes(3, "big") + bytes(length))
    frames = bytearray()
    remaining, number = total_samples, 0
    while remaining > 0:
        count = min(4096, remaining)
        frames.extend(flac_frame(number, count, channels=channels, bits_per_sample=bits_per_sample,
                                 verbatim=verbatim))
        remaining -= count
        number += 1
    return b"fLaC" + b"".join(metadata_blocks) + frames


def flac_with_false_strategy_header() -> bytes:
    """A valid fixed-block FLAC whose verbatim samples resemble a bad header."""
    data = bytearray(flac_bytes(8192, rate=8000, verbatim=True))
    first_frame_start = 42
    first_frame_end = first_frame_start + len(flac_frame(0, 4096, verbatim=True))
    fake_offset = first_frame_start + 8 + 1 + 128
    fake_header = b"\xff\xf9\x70\x00\x00\x0f\xff"
    data[fake_offset:fake_offset + 8] = fake_header + bytes((_flac_crc8(fake_header) ^ 1,))
    data[first_frame_end - 2:first_frame_end] = _flac_crc16(
        data[first_frame_start:first_frame_end - 2]).to_bytes(2, "big")
    return bytes(data)


def flac_with_crc8_valid_false_header() -> bytes:
    """A contiguous-looking frame header embedded inside verbatim samples."""
    data = bytearray(flac_bytes(8192, rate=8000, verbatim=True))
    first_frame_start = 42
    first_frame_end = first_frame_start + len(flac_frame(0, 4096, verbatim=True))
    fake_offset = first_frame_start + 8 + 1 + 128
    fake_header = flac_frame(1, 4096, verbatim=True)[:8]
    data[fake_offset:fake_offset + len(fake_header)] = fake_header
    data[first_frame_end - 2:first_frame_end] = _flac_crc16(
        data[first_frame_start:first_frame_end - 2]).to_bytes(2, "big")
    return bytes(data)


def mp3_bytes(frames: int) -> bytes:
    """MPEG-1 Layer III frames with no ``Xing`` tag, so frames are counted."""
    head = bytes([0xFF, 0xFB, (9 << 4) | (0 << 2) | 0, 0x00])
    frame = head + b"\x00" * (MP3_FRAME_LENGTH - 4)
    assert len(frame) == MP3_FRAME_LENGTH
    return frame * frames


def mp3_duration(frames: int) -> float:
    return frames * MP3_FRAME_SAMPLES / FLAC_RATE


def mp3_frames_for(seconds: float) -> int:
    """Whole frames that play for about this long."""
    return round(seconds * FLAC_RATE / MP3_FRAME_SAMPLES)


def mp3_with_xing(frames: int, *, declares: int | None = None) -> bytes:
    """Frames whose first one carries a ``Xing`` count, as an encoder writes it.

    ``declares`` states how many frames the encoder meant to write, which is the
    number that survives a stream being cut off half way through.
    """
    head = bytes([0xFF, 0xFB, (9 << 4) | (0 << 2) | 0, 0x00])
    tag = b"\x00" * 32 + b"Xing" + (0x0001).to_bytes(4, "big") + (declares or frames).to_bytes(4, "big")
    frame = head + tag + b"\x00" * (MP3_FRAME_LENGTH - 4 - len(tag))
    assert len(frame) == MP3_FRAME_LENGTH
    return frame * frames


def mp3_frame(rate_index: int, length: int) -> bytes:
    """One MPEG-1 Layer III frame at 128 kbps and this sample rate."""
    head = bytes([0xFF, 0xFB, (9 << 4) | (rate_index << 2) | 0, 0x00])
    return head + b"\x00" * (length - 4)


def mp3_mixed(first: int, second: int) -> bytes:
    """Frames whose first carries a ``Xing`` count and whose rate changes."""
    tag = b"\x00" * 32 + b"Xing" + (0x0001).to_bytes(4, "big") + (first + second).to_bytes(4, "big")
    head = bytes([0xFF, 0xFB, (9 << 4) | (0 << 2) | 0, 0x00])
    opening = head + tag + b"\x00" * (MP3_FRAME_LENGTH - 4 - len(tag))
    assert len(opening) == MP3_FRAME_LENGTH
    return opening + mp3_frame(0, MP3_FRAME_LENGTH) * (first - 1) + mp3_frame(2, 576) * second


def _mp4_box(kind: bytes, body: bytes) -> bytes:
    return (len(body) + 8).to_bytes(4, "big") + kind + body


def mp4_bytes(seconds: float, timescale: int = 1000, *, sample_bytes: int | None = None,
              mdat_bytes: int | None = None, moov_at_end: bool = False,
              co64: bool = False, compact_sizes: bool = False) -> bytes:
    """An ``ftyp`` box with an audio sample table and a matching ``mdat``."""
    duration = int(seconds * timescale)
    mvhd_body = bytes([0, 0, 0, 0]) + b"\x00" * 8 + timescale.to_bytes(4, "big") + duration.to_bytes(4, "big") + b"\x00" * 80
    mvhd = _mp4_box(b"mvhd", mvhd_body)
    tkhd = _mp4_box(b"tkhd", bytes(12) + (1).to_bytes(4, "big") + bytes(8))
    hdlr = _mp4_box(b"hdlr", bytes(8) + b"soun" + bytes(12))
    payload_size = sample_bytes if sample_bytes is not None else int(seconds * 100)
    if compact_sizes:
        assert payload_size <= 255
        stsz = _mp4_box(b"stz2", bytes(7) + b"\x08" + (1).to_bytes(4, "big") + bytes((payload_size,)))
    else:
        stsz = _mp4_box(b"stsz", bytes(4) + payload_size.to_bytes(4, "big") + (1).to_bytes(4, "big"))
    offsets = _mp4_box(b"co64", bytes(4) + (1).to_bytes(4, "big") + (0).to_bytes(8, "big")) if co64 else b""
    stbl = _mp4_box(b"stbl", stsz + offsets)
    minf = _mp4_box(b"minf", stbl)
    mdia = _mp4_box(b"mdia", hdlr + minf)
    trak = _mp4_box(b"trak", tkhd + mdia)
    moov = _mp4_box(b"moov", mvhd + trak)
    ftyp = (20).to_bytes(4, "big") + b"ftypM4A " + b"\x00\x00\x00\x00" + b"M4A "
    audio = b"\x00" * (payload_size if mdat_bytes is None else mdat_bytes)
    mdat = (len(audio) + 8).to_bytes(4, "big") + b"mdat" + audio
    return ftyp + (mdat + moov if moov_at_end else moov + mdat)


class Source:
    def __init__(self, data: bytes, *, extension: str, media_type: str,
                 declared_size: int | None = None):
        self.data, self.extension, self.media_type = data, extension, media_type
        self.declared_size = declared_size

    async def download(self, _candidate):
        async def chunks():
            yield self.data

        return DownloadMetadata(chunks=chunks(), extension=self.extension, media_type=self.media_type,
                                declared_size=self.declared_size,
                                _close_once=_CloseOnce(_noop))

    async def health(self):
        return True


async def _noop() -> None:
    return None


def candidate(fmt: str, *, duration: int | None = None, item: str = "1") -> Candidate:
    return Candidate(source_id="source", source_version="v1", item_id=item, title="Song",
                     artist="Artist", format=fmt, duration=duration)


def download(data: bytes, *, extension: str, media_type: str, fmt: str | None = None,
             duration: int | None = None, verify_duration: str = "lenient", tmp_path, events: list):
    return asyncio.run(download_candidate(
        candidate(fmt or extension, duration=duration), Source(data, extension=extension, media_type=media_type),
        tmp_path, request_id="r", verify_duration=verify_duration, record=events.append))


# --- the three containers, read from bytes -----------------------------------

def test_flac_streaminfo_states_an_exact_length():
    assert container_duration(flac_bytes(int(210.5 * FLAC_RATE))) == pytest.approx(210.5)


def test_a_short_flac_reports_the_short_length_it_really_holds():
    assert container_duration(flac_bytes(int(30 * FLAC_RATE))) == pytest.approx(30.0)


def test_a_flac_cut_after_its_header_still_states_the_header_length():
    # Honest about the limit rather than quiet about it: FLAC puts its length in
    # a header, so a file cut *after* that header still claims the full time.
    # Catching that needs a decoder, which this feature deliberately does not
    # add; what it does catch is the preview, whose header states the short
    # length it really serves.
    assert container_duration(flac_bytes(int(210 * FLAC_RATE))[:42]) == pytest.approx(210.0)


def test_mp3_without_a_xing_tag_is_measured_by_counting_its_frames():
    assert container_duration(mp3_bytes(100)) == pytest.approx(mp3_duration(100))


def test_a_truncated_mp3_reports_only_the_frames_that_arrived():
    # The stream was cut mid-song: the frames that really landed are the only
    # ones counted, so the answer is short rather than the catalogue's.
    assert container_duration(mp3_bytes(20)) == pytest.approx(mp3_duration(20))


def test_a_whole_mp3_with_a_xing_tag_answers_with_its_own_length():
    assert container_duration(mp3_with_xing(50)) == pytest.approx(mp3_duration(50))


def test_a_truncated_mp3_keeps_only_the_frames_that_really_arrived():
    # Encoders write the frame count up front, and a stream cut in half keeps
    # it: the tag says 8,039 frames while eleven ever arrived.
    cut = mp3_with_xing(11, declares=8039)
    assert container_duration(cut) == pytest.approx(mp3_duration(11))


def test_a_xing_tag_stands_when_the_stream_did_not_run_out():
    # Trailing metadata the parser cannot read is not a truncation, so the
    # tag's own length stands instead of the shorter count.
    tagged = mp3_with_xing(50) + b"TAG" + b"\x00" * 125
    assert container_duration(tagged) == pytest.approx(mp3_duration(50))


def test_a_stale_xing_count_does_not_shorten_a_complete_recording():
    # The tag was written when the file held eleven frames and never rewritten
    # as the audio grew: the frames that really arrived are the truth, so a
    # recording that plays its full length is not refused for its tag's mistake.
    assert container_duration(mp3_with_xing(100, declares=11)) == pytest.approx(mp3_duration(100))


def test_a_stream_that_changes_rate_measures_the_time_it_really_plays():
    # Every frame carries its own rate, so a recording that changes rate part
    # way through is measured by what each frame really holds rather than by
    # the first frame's rate standing for all of them.
    data = mp3_mixed(50, 50)
    expected = 50 * MP3_FRAME_SAMPLES / FLAC_RATE + 50 * MP3_FRAME_SAMPLES / 32000
    assert container_duration(data) == pytest.approx(expected)


def test_a_header_the_parser_cannot_read_is_not_a_short_recording():
    # The tag claims more frames than could be walked, but the bytes did not run
    # out: something in the middle is not a frame.  The length is unknown rather
    # than short, because reading it as short would refuse a file that is merely
    # hard to read -- and the file itself is the only witness to its own length.
    assert container_duration(mp3_with_xing(50, declares=500) + b"\x00" * 64) is None


def test_a_lone_frame_header_is_not_a_measurement():
    # Four bytes of a frame header hold no audio at all, so nothing is claimed.
    assert container_duration(bytes([0xFF, 0xFB, (9 << 4) | (0 << 2) | 0, 0x00])) is None


def test_mp4_mvhd_states_its_length():
    assert container_duration(mp4_bytes(180.0)) == pytest.approx(180.0)


def test_a_short_mp4_reports_the_short_length_it_really_holds():
    assert container_duration(mp4_bytes(30.0)) == pytest.approx(30.0)


def test_an_mp4_that_never_states_its_length_is_not_a_long_one():
    # ``0xffffffff`` is how the format writes "not known up front" -- a live or
    # fragmented recording -- so the honest answer is no answer, not a
    # four-million-second track the catalogue would refuse.
    mvhd_body = bytes(12) + (1000).to_bytes(4, "big") + b"\xff" * 4
    mvhd = (len(mvhd_body) + 8).to_bytes(4, "big") + b"mvhd" + mvhd_body
    moov = (len(mvhd) + 8).to_bytes(4, "big") + b"moov" + mvhd
    ftyp = (20).to_bytes(4, "big") + b"ftypM4A " + b"\x00\x00\x00\x00" + b"M4A "
    assert container_duration(ftyp + moov) is None


def test_an_mp4_cut_behind_its_header_is_not_measured():
    # ``mvhd`` states the whole recording and survives a cut, so the header
    # alone reports 210 s for a file that no longer holds 210 s of audio.  A
    # box reaching past the end of the file is that cut, and the honest answer
    # is that the length is unknown rather than the header's claim.
    whole = mp4_bytes(210.0)
    assert container_duration(whole) == pytest.approx(210.0)
    assert container_duration(whole[:-40]) is None


def test_an_mp4_cut_inside_its_last_box_header_is_not_measured():
    # A cut can land inside a box header, leaving a box that never got to
    # state its own size.  Bytes too few to be a box are a chain that stopped
    # rather than a measurement, so the length is unknown instead of the
    # header's claim.
    assert container_duration(mp4_bytes(210.0) + (42).to_bytes(4, "big")) is None


def test_an_mp4_cut_behind_its_header_is_refused_by_strict(tmp_path):
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(mp4_bytes(210.0)[:-1_000], extension="m4a", media_type="audio/mp4", duration=210,
                 verify_duration="strict", tmp_path=tmp_path, events=events)
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_an_mp4_cut_behind_its_header_is_accepted_by_lenient_and_recorded(tmp_path):
    # The missing 40 bytes are within the time scaled tolerance for this small
    # synthetic payload, so this remains an unverified lenient result.
    events = []
    result = download(mp4_bytes(210.0)[:-40], extension="m4a", media_type="audio/mp4", duration=210,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None
    assert [event.error_code for event in events if event.error_code] == ["duration_unverified"]


@pytest.mark.parametrize("data", [b"", b"nope", b"\x00" * 64, b"fLaC", b"fLaC\x00\x00\x00\x02\x00\x00"])
def test_anything_this_module_cannot_read_answers_none(data):
    assert container_duration(data) is None


def test_a_file_it_cannot_read_answers_none_instead_of_raising(tmp_path):
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"fLaC\x80\x00\x00\x22\x00\x00")
    assert duration_seconds(broken) is None
    assert duration_seconds(tmp_path / "missing.mp3") is None


def test_a_real_file_is_measured_from_disk(tmp_path):
    path = tmp_path / "song.mp3"
    path.write_bytes(mp3_bytes(50))
    assert duration_seconds(path) == pytest.approx(mp3_duration(50))


def test_a_recording_past_the_old_scan_ceiling_is_measured_like_any_other(tmp_path):
    # The frame walk reads the stream in chunks instead of holding it, so the
    # ceiling this parser used to stop at is gone: a 70-minute recording still
    # answers with its own length rather than "unknown", which strict would
    # otherwise refuse.
    frames = 160_934
    path = tmp_path / "long.mp3"
    path.write_bytes(mp3_with_xing(frames))
    assert path.stat().st_size > 64 * 1024 * 1024
    assert duration_seconds(path) == pytest.approx(frames * MP3_FRAME_SAMPLES / FLAC_RATE)


# --- the tolerance -----------------------------------------------------------

@pytest.mark.parametrize("expected,short,long", [
    (20.0, 1.0, 1.5),    # the long side keeps its 1.5 s floor
    (60.0, 3.0, 3.0),    # the short side is capped at 3 s, and both scale
    (600.0, 3.0, 30.0),  # a long track's long side keeps scaling
])
def test_tolerance_matches_the_upstream_shape(expected, short, long):
    assert duration_tolerance(expected) == pytest.approx((short, long))


@pytest.mark.parametrize("expected,measured,inside", [
    (60.0, 57.0, True),
    (60.0, 56.999, False),
    (60.0, 63.0, True),
    (60.0, 63.001, False),
    (20.0, 19.0, True),
    (20.0, 18.999, False),
    (20.0, 21.5, True),
    (20.0, 21.501, False),
])
def test_tolerance_boundaries_are_inclusive(expected, measured, inside):
    assert duration_matches(expected, measured) is inside


# --- the bitrate, which is a measurement or nothing --------------------------

def test_bitrate_is_the_real_byte_count_over_the_real_time():
    assert bitrate_kbps(1_411_200, 8.0) == 1411
    assert bitrate_kbps(41_700, mp3_duration(100)) == 128


@pytest.mark.parametrize("size,duration", [(0, 10.0), (100, 0.0), (-1, 10.0)])
def test_bitrate_is_left_empty_when_it_cannot_be_told(size, duration):
    assert bitrate_kbps(size, duration) is None


# --- the download that refuses a preview -------------------------------------

# Real containers, built by name: pytest ids cannot carry raw bytes, and a
# Windows environment variable is what the current-test marker rides in.
PREVIEW_SAMPLES = {
    "flac": (_encoder_flac_bytes, "flac", "audio/flac"),
    "mp3": (lambda: mp3_bytes(20), "mp3", "audio/mpeg"),
    "m4a": (lambda: mp4_bytes(30.0), "m4a", "audio/mp4"),
}
FULL_SAMPLES = {
    "flac": (lambda: flac_bytes(int(210 * FLAC_RATE)), "flac", "audio/flac"),
    # Frame counting only knows what really arrived, so the sample has to hold
    # a whole song's worth of frames for this to be the full-length case.
    "mp3": (lambda: mp3_bytes(mp3_frames_for(210)), "mp3", "audio/mpeg"),
    "m4a": (lambda: mp4_bytes(210.0), "m4a", "audio/mp4"),
}


@pytest.mark.parametrize("name", sorted(PREVIEW_SAMPLES))
def test_a_preview_is_refused_when_the_catalogue_states_a_longer_track(tmp_path, name):
    build, extension, media_type = PREVIEW_SAMPLES[name]
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(build(), extension=extension, media_type=media_type, duration=210,
                 tmp_path=tmp_path, events=events)
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]


def test_a_truncated_mp3_that_kept_its_xing_tag_is_refused(tmp_path):
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(mp3_with_xing(11, declares=8039), extension="mp3", media_type="audio/mpeg",
                 duration=210, tmp_path=tmp_path, events=events)
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]


def test_a_complete_recording_whose_tag_understates_it_is_delivered(tmp_path):
    # A stale tag is the tag's mistake, not the listener's: the frames really
    # present play the whole track, so strict delivers it.
    events = []
    result = download(mp3_with_xing(mp3_frames_for(210), declares=11), extension="mp3",
                      media_type="audio/mpeg", duration=210, verify_duration="strict",
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds == pytest.approx(210.0, abs=1.0)
    assert [event.status for event in events if event.stage == "download"] == ["success"]


@pytest.mark.parametrize("name", sorted(FULL_SAMPLES))
def test_a_full_length_file_is_delivered_with_its_measured_time(tmp_path, name):
    build, extension, media_type = FULL_SAMPLES[name]
    events = []
    result = download(build(), extension=extension, media_type=media_type, duration=210,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds == pytest.approx(210.0, abs=1.0)
    assert result.bitrate_kbps is not None
    assert (tmp_path / result.relative_path).stat().st_size == result.size_bytes
    assert [event.status for event in events if event.stage == "download"] == ["success"]


def test_a_refused_preview_leaves_no_file_behind(tmp_path):
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(_encoder_flac_bytes(), extension="flac", media_type="audio/flac",
                 duration=210, tmp_path=tmp_path, events=events)
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    assert not list(tmp_path.rglob("*.part"))


def test_encoder_generated_flac_fixture_is_complete(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    data = _encoder_flac_bytes()
    path = tmp_path / "ffmpeg-30s.flac"
    path.write_bytes(data)
    assert container_duration(data) == pytest.approx(30.0)
    assert verify_stream_integrity(path, expected_duration=30).status == "complete"


@pytest.mark.parametrize(("fraction", "label"), [(0.5, "half"), (0.125, "eighth")])
def test_a_truncated_encoder_generated_flac_is_incomplete(tmp_path, fraction, label):
    from musicdl.media.duration import verify_stream_integrity

    full = _encoder_flac_bytes()
    cut = full[:int(len(full) * fraction)]
    path = tmp_path / f"ffmpeg-30s-{label}.flac"
    path.write_bytes(cut)
    assert verify_stream_integrity(path, expected_duration=30).status == "incomplete"
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(cut, extension="flac", media_type="audio/flac", duration=30,
                 verify_duration="strict", tmp_path=tmp_path, events=events)
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]


def test_a_streaminfo_only_flac_is_unverified_under_lenient_policy(tmp_path):
    events = []
    data = flac_bytes(210 * FLAC_RATE)[:42]
    result = download(data, extension="flac", media_type="audio/flac", duration=210,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None
    assert [(event.stage, event.status, event.error_code) for event in events
            if event.stage == "duration"] == [("duration", "unverified", "duration_unverified")]


def test_a_flac_with_no_declared_sample_count_is_unverified(tmp_path):
    events = []
    result = download(flac_bytes(0), extension="flac", media_type="audio/flac", duration=210,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None
    assert [event.error_code for event in events if event.error_code] == ["duration_unverified"]


def test_flac_streaminfo_duration_is_untrusted_when_first_frame_header_conflicts(tmp_path):
    data = bytearray(_encoder_flac_bytes())
    packed = int.from_bytes(data[18:26], "big")
    rate_mask = ((1 << 20) - 1) << 44
    data[18:26] = ((packed & ~rate_mask) | (22050 << 44)).to_bytes(8, "big")
    events = []
    result = download(bytes(data), extension="flac", media_type="audio/flac", duration=30,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None
    assert [(event.status, event.error_code) for event in events if event.stage == "duration"] == [
        ("unverified", "duration_unverified")]


def test_a_flac_with_an_id3v2_prefix_is_found_and_scanned(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    prefix = b"ID3\x04\x00\x00\x00\x00\x00\x00"
    path = tmp_path / "prefixed.flac"
    path.write_bytes(prefix + flac_bytes(210 * FLAC_RATE))
    assert container_duration(path.read_bytes()) == pytest.approx(210)
    assert verify_stream_integrity(path, expected_duration=210).status == "complete"


def test_flac_false_strategy_header_in_frame_payload_does_not_stop_scan(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    path = tmp_path / "false-strategy-header.flac"
    path.write_bytes(flac_with_false_strategy_header())
    assert verify_stream_integrity(path, expected_duration=8192 / 8000).status == "complete"


def test_flac_crc8_valid_contiguous_header_in_payload_does_not_stop_download(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    data = flac_with_crc8_valid_false_header()
    path = tmp_path / "crc8-valid-false-header.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=8192 / 8000).status == "complete"
    events = []
    result = download(data, extension="flac", media_type="audio/flac", duration=1,
                      verify_duration="strict", tmp_path=tmp_path, events=events)
    assert result.duration_seconds == pytest.approx(8192 / 8000)
    assert [event.status for event in events if event.stage == "download"] == ["success"]


def test_a_flac_with_an_id3v1_suffix_is_complete(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    path = tmp_path / "id3v1.flac"
    path.write_bytes(_encoder_flac_bytes() + b"TAG" + bytes(125))
    assert verify_stream_integrity(path, expected_duration=30).status == "complete"


def test_tag_at_id3v1_offset_inside_verbatim_flac_is_sample_data(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    data = bytearray(flac_bytes(4096, rate=8000, verbatim=True))
    assert data[-128:-125] != b"TAG"
    data[-128:-125] = b"TAG"
    frame_start = 42
    data[-2:] = _flac_crc16(data[frame_start:-2]).to_bytes(2, "big")
    path = tmp_path / "tag-at-id3v1-offset-in-frame.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=4096 / 8000).status == "complete"


@pytest.mark.parametrize(("corruption", "expected_status"), [
    ("payload", "unverified"),
    ("crc16", "incomplete"),
])
def test_a_flac_with_a_corrupt_nonterminal_frame_and_id3v1_reports_parser_evidence(
    tmp_path, corruption, expected_status,
):
    from musicdl.media.duration import _flac_frame_header, verify_stream_integrity

    data = bytearray(_encoder_flac_bytes())
    frame_headers = []
    for offset in range(42, len(data) - 8):
        if data[offset:offset + 2] != b"\xff\xf8":
            continue
        parsed = _flac_frame_header(data, offset, 44100, 2, 16)
        if parsed is not None and parsed[3]:
            frame_headers.append((offset, parsed))
            if len(frame_headers) == 2:
                break
    assert len(frame_headers) == 2
    first_start, first_header = frame_headers[0]
    second_start = frame_headers[1][0]
    corrupt_at = first_header[4] if corruption == "payload" else second_start - 1
    data[corrupt_at] ^= 0x01
    data.extend(b"TAG" + bytes(125))

    path = tmp_path / f"corrupt-first-frame-{corruption}-id3v1.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=30).status == expected_status


def test_flac_unsupported_subframe_probe_remains_unverified(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    data = bytearray(flac_bytes(4096, rate=8000, verbatim=True))
    data[42 + 8] = 0x04  # Reserved subframe type; do not infer corruption from its bytes.
    data[-1] ^= 0x01
    path = tmp_path / "unsupported-terminal-subframe.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=4096 / 8000).status == "unverified"


def test_flac_probe_bit_limit_remains_unverified(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    samples = 63_000
    packed = (8000 << 44) | samples | ((32 - 1) << 36)
    streaminfo = (samples.to_bytes(2, "big") * 2 + bytes(6)
                  + packed.to_bytes(8, "big") + bytes(16))
    data = bytearray(b"fLaC\x80\x00\x00\x22" + streaminfo
                     + flac_frame(0, samples, bits_per_sample=32, verbatim=True))
    data[-1] ^= 0x01
    path = tmp_path / "probe-bit-limit.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=samples / 8000).status == "unverified"


def test_flac_probe_timeout_remains_unverified(tmp_path, monkeypatch):
    import musicdl.media.duration as duration_module

    full = flac_bytes(8192, rate=8000, verbatim=True)
    final_frame_start = 42 + len(flac_frame(0, 4096, verbatim=True))
    data = full[:final_frame_start + 8]
    path = tmp_path / "probe-timeout.flac"
    path.write_bytes(data)
    state = {"expired": False}
    monkeypatch.setattr(duration_module.time, "monotonic",
                        lambda: 6.0 if state["expired"] else 1.0)
    real_probe = duration_module._flac_frame_structural_end

    def expire_at_probe(*args, **kwargs):
        state["expired"] = True
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(duration_module, "_flac_frame_structural_end", expire_at_probe)
    assert duration_module.verify_stream_integrity(
        path, expected_duration=8192 / 8000).status == "unverified"


def test_flac_reader_timeout_wins_over_truncation_classification(monkeypatch):
    import musicdl.media.duration as duration_module

    full = flac_bytes(4096, rate=8000, verbatim=True)
    data = full[:42 + 8]
    ticks = iter((1.0, 6.0))
    monkeypatch.setattr(duration_module.time, "monotonic", lambda: next(ticks))
    with pytest.raises(duration_module._FlacFrameProbeError) as exc:
        duration_module._flac_frame_structural_end(
            data, 42, len(data), 8000, 1, 8, deadline=5.0)
    assert exc.value.reason == "timeout"


def test_random_trailing_data_never_makes_a_flac_complete(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    path = tmp_path / "random-tail.flac"
    path.write_bytes(_encoder_flac_bytes() + b"\x91\x04random-tail\x00")
    assert verify_stream_integrity(path, expected_duration=30).status != "complete"


def test_a_complete_flac_passes_the_strict_download_policy(tmp_path):
    events = []
    result = download(_encoder_flac_bytes(), extension="flac", media_type="audio/flac",
                      duration=30, verify_duration="strict", tmp_path=tmp_path, events=events)
    assert result.duration_seconds == pytest.approx(30)
    assert [event.status for event in events if event.stage == "download"] == ["success"]


def test_flac_header_crc_failure_is_unverified_not_incomplete(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    data = bytearray(flac_bytes(210 * FLAC_RATE))
    first_frame = 42
    second_frame = first_frame + len(flac_frame(0, 4096))
    data[second_frame + 7] ^= 0x01
    path = tmp_path / "bad-header-crc.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=210).status == "unverified"


@pytest.mark.parametrize("corrupt_frame", ["terminal", "nonterminal"])
def test_a_supported_flac_frame_crc_mismatch_is_incomplete(tmp_path, corrupt_frame):
    from musicdl.media.duration import verify_stream_integrity

    data = bytearray(flac_bytes(8192, rate=8000, verbatim=True))
    if corrupt_frame == "terminal":
        data[-1] ^= 0x01
    else:
        first_frame_end = 42 + len(flac_frame(0, 4096, verbatim=True))
        data[first_frame_end - 1] ^= 0x01
    path = tmp_path / f"{corrupt_frame}-frame-crc-mismatch.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=8192 / 8000).status == "incomplete"


@pytest.mark.parametrize("cut_final_frame", ["after_header", "last_four_bytes", "last_crc_byte"])
def test_flac_final_frame_payload_and_crc_are_verified(tmp_path, cut_final_frame):
    from musicdl.media.duration import verify_stream_integrity

    data = flac_bytes(8192, rate=8000, verbatim=True)
    final_frame_start = 42 + len(flac_frame(0, 4096, verbatim=True))
    if cut_final_frame == "after_header":
        data = data[:final_frame_start + 8]
    elif cut_final_frame == "last_crc_byte":
        data = data[:-1]
    else:
        data = data[:-4]
    path = tmp_path / f"truncated-final-frame-{cut_final_frame}.flac"
    path.write_bytes(data)
    assert verify_stream_integrity(path, expected_duration=8192 / 8000).status == "incomplete"


def test_flac_scan_timeout_is_unverified(tmp_path, monkeypatch):
    import musicdl.media.duration as duration_module

    data = flac_bytes(210 * FLAC_RATE)
    path = tmp_path / "timeout.flac"
    path.write_bytes(data)
    times = iter((1.0, 1.0, 7.0))
    monkeypatch.setattr(duration_module.time, "monotonic", lambda: next(times, 7.0))
    result = duration_module.verify_stream_integrity(path, expected_duration=210, timeout_seconds=5)
    assert result.status == "unverified"


def test_a_matching_content_length_does_not_skip_integrity_scan(tmp_path):
    data = _encoder_flac_bytes() + b"unsupported-trailer"
    events = []
    result = asyncio.run(download_candidate(
        candidate("flac", duration=30),
        Source(data, extension="flac", media_type="audio/flac", declared_size=len(data)),
        tmp_path, request_id="r", verify_duration="lenient", record=events.append))
    assert result.duration_seconds is None
    assert [(event.stage, event.status, event.error_code) for event in events
            if event.stage == "duration"] == [("duration", "unverified", "duration_unverified")]


def test_unknown_flac_tail_is_leniently_accepted_but_strictly_refused(tmp_path):
    data = _encoder_flac_bytes() + b"unsupported-trailer"
    result = download(data, extension="flac", media_type="audio/flac", duration=30,
                      tmp_path=tmp_path / "lenient", events=[])
    assert result.duration_seconds is None

    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(data, extension="flac", media_type="audio/flac", duration=30,
                 verify_duration="strict", tmp_path=tmp_path / "strict", events=events)
    assert [event.error_code for event in events if event.error_code] == [
        "duration_unverified", "incomplete_audio"]


def test_exact_content_length_does_not_hide_a_damaged_flac_crc(tmp_path):
    data = bytearray(_encoder_flac_bytes())
    data[-1] ^= 0x01
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        asyncio.run(download_candidate(
            candidate("flac", duration=30),
            Source(bytes(data), extension="flac", media_type="audio/flac",
                   declared_size=len(data)),
            tmp_path, request_id="r", record=events.append))
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]


def test_a_catalogue_mismatch_is_checked_when_flac_integrity_is_unverified(tmp_path):
    events = []
    data = _encoder_flac_bytes() + b"unsupported-trailer"
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(data, extension="flac", media_type="audio/flac", duration=10,
                 tmp_path=tmp_path, events=events)
    assert [event.error_code for event in events if event.error_code] == [
        "duration_unverified", "incomplete_audio"]


def test_untrusted_flac_metadata_does_not_reject_on_its_duration(tmp_path):
    events = []
    data = bytearray(_encoder_flac_bytes())
    assert data[42] & 0x80 and (data[42] & 0x7F) == 4
    data[42] &= 0x7F  # Hide the Vorbis comment block's last-block marker.
    result = download(bytes(data), extension="flac", media_type="audio/flac", duration=10,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None
    assert [(event.stage, event.status, event.error_code) for event in events
            if event.stage == "duration"] == [("duration", "unverified", "duration_unverified")]


def test_mp4_sample_table_larger_than_mdat_is_refused(tmp_path):
    events = []
    data = mp4_bytes(210, sample_bytes=10_000, mdat_bytes=100)
    with pytest.raises(MediaError, match="incomplete_audio"):
        asyncio.run(download_candidate(
            candidate("m4a", duration=210),
            Source(data, extension="m4a", media_type="audio/mp4", declared_size=len(data)),
            tmp_path, request_id="r", record=events.append))
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]


def test_mp4_short_mdat_is_refused_without_catalogue_duration(tmp_path):
    data = mp4_bytes(210, sample_bytes=10_000, mdat_bytes=100)
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(data, extension="m4a", media_type="audio/mp4", duration=None,
                 tmp_path=tmp_path, events=[])


def test_mp4_stz2_sample_table_larger_than_mdat_is_refused(tmp_path):
    events = []
    data = mp4_bytes(210, sample_bytes=200, mdat_bytes=10, compact_sizes=True)
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(data, extension="m4a", media_type="audio/mp4", duration=210,
                 tmp_path=tmp_path, events=events)
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]


def test_truncated_mp4_mdat_is_not_complete_when_sample_table_fits(tmp_path):
    from musicdl.media.duration import verify_stream_integrity

    data = bytearray(mp4_bytes(210, sample_bytes=100, mdat_bytes=1_000))
    mdat_header = data.index(b"mdat") - 4
    data[mdat_header:mdat_header + 4] = (2_008).to_bytes(4, "big")
    path = tmp_path / "truncated-mdat.m4a"
    path.write_bytes(data)
    assert verify_stream_integrity(path).status == "unverified"


def test_mp4_mdat_at_least_as_large_as_its_sample_table_is_delivered(tmp_path):
    data = mp4_bytes(210, sample_bytes=100, mdat_bytes=1_000)
    result = download(data, extension="m4a", media_type="audio/mp4", duration=210,
                      tmp_path=tmp_path, events=[])
    assert result.duration_seconds == pytest.approx(210)


def test_mp4_sample_table_is_parsed_with_moov_at_end_and_co64(tmp_path):
    data = mp4_bytes(210, sample_bytes=100, mdat_bytes=1_000, moov_at_end=True, co64=True)
    result = download(data, extension="m4a", media_type="audio/mp4", duration=210,
                      tmp_path=tmp_path, events=[])
    assert result.duration_seconds == pytest.approx(210)


def test_fragmented_mp4_is_unverified_under_lenient_policy(tmp_path):
    ftyp = (20).to_bytes(4, "big") + b"ftypM4A " + bytes(4) + b"M4A "
    moof = _mp4_box(b"moof", bytes(8))
    mdat = _mp4_box(b"mdat", bytes(64))
    events = []
    result = download(ftyp + moof + mdat, extension="m4a", media_type="audio/mp4", duration=210,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None
    assert [event.error_code for event in events if event.error_code] == ["duration_unverified"]


def test_a_50mb_flac_integrity_scan_finishes_within_five_seconds(tmp_path):
    import time

    from musicdl.media.duration import verify_stream_integrity

    path = tmp_path / "large.flac"
    data = flac_bytes(210 * FLAC_RATE, channels=2, bits_per_sample=24, verbatim=True)
    path.write_bytes(data)
    assert len(data) >= 50 * 1024 * 1024
    started = time.monotonic()
    result = verify_stream_integrity(path, expected_duration=210)
    assert time.monotonic() - started < 5
    assert result.status == "complete"


def _published(reservation_root, data: bytes) -> ArtifactRecord:
    """An artifact this job already published, with its real size and hash."""
    target = reservation_root / "Song.mp3"
    target.write_bytes(data)
    return ArtifactRecord(job_id="job", candidate_id="1", temporary_relative_path=".staging/t.part",
                          target_relative_path="Song.mp3", allocation_slot=0, extension="mp3",
                          media_type="audio/mpeg", size_bytes=len(data),
                          sha256=hashlib.sha256(data).hexdigest(), state="published")


def _replay(tmp_path, reservation, *, duration: int | None = None, verify_duration: str = "lenient",
            events: list | None = None):
    data = (tmp_path / "Song.mp3").read_bytes()
    return asyncio.run(download_candidate(
        candidate("mp3", duration=duration), Source(data, extension="mp3", media_type="audio/mpeg"),
        tmp_path, request_id="r", reservation=reservation, verify_duration=verify_duration,
        record=(events if events is not None else []).append))


def test_a_published_artifact_is_measured_but_not_refused(tmp_path):
    # The gate belongs to publication.  An artifact already marked ``published``
    # may already be served and referred to, so a replay of it reports what the
    # bytes really measure rather than refusing them: a refusal here would leave
    # behind the very bytes it refused, which is the one outcome this feature
    # must never produce.  What the policy governs is what gets published.
    data = mp3_bytes(20)
    reservation = _published(tmp_path, data)
    events = []
    result = _replay(tmp_path, reservation, duration=210, events=events)
    assert (tmp_path / "Song.mp3").read_bytes() == data
    assert result.duration_seconds == pytest.approx(mp3_duration(20))
    assert [event.error_code for event in events if event.error_code] == []
    assert [path for path in tmp_path.rglob("*.part")] == []


def test_a_replay_measures_the_file_and_says_what_it_could_not_compare(tmp_path):
    reservation = _published(tmp_path, mp3_bytes(100))
    events = []
    result = _replay(tmp_path, reservation, events=events)
    assert result.duration_seconds == pytest.approx(mp3_duration(100))
    assert result.bitrate_kbps is not None
    assert [(event.stage, event.status) for event in events if event.stage == "duration"] == [
        ("duration", "unverified")]


def test_a_replay_of_a_published_artifact_is_not_refused_even_under_strict(tmp_path):
    # ``strict`` refuses what it cannot measure, but that refusal is a gate on
    # publication as well: these bytes are already library content, so the
    # policy measures and reports them rather than refusing them.
    reservation = _published(tmp_path, mp3_bytes(100))
    events = []
    result = _replay(tmp_path, reservation, verify_duration="strict", events=events)
    assert (tmp_path / "Song.mp3").exists()
    assert result.duration_seconds == pytest.approx(mp3_duration(100))
    assert [event.error_code for event in events if event.error_code] == ["duration_unverified"]


class _Unused:
    """A source a replay must never reach."""

    async def download(self, _candidate, quality=None):
        raise AssertionError("a replay must not call the source")

    async def health(self):
        return True


def test_a_replay_reports_the_tier_that_was_asked_for_and_the_container_it_holds(tmp_path):
    # A replay answers the same question a fresh download does, so it names the
    # tier the job asked for next to the container the bytes prove.  The record
    # and the success notice read both, and neither is inferred from the other.
    data = mp3_bytes(200)
    reservation = _published(tmp_path, data)
    result = asyncio.run(download_candidate(
        candidate("mp3"), _Unused(), tmp_path, request_id="r", reservation=reservation,
        quality="320k"))
    assert (result.requested_quality, result.actual_quality) == ("320k", "mp3")
    assert result.size_bytes == len(data)
    assert result.relative_path.as_posix() == "Song.mp3"


def _staged(tmp_path, data: bytes) -> ArtifactRecord:
    """An artifact this job streamed to its staging path but never published."""
    staged = tmp_path / ".staging" / "t.part"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    return ArtifactRecord(job_id="job", candidate_id="1", temporary_relative_path=".staging/t.part",
                          target_relative_path="Song.mp3", allocation_slot=0, extension="mp3",
                          media_type="audio/mpeg", size_bytes=len(data),
                          sha256=hashlib.sha256(data).hexdigest(), state="stream_complete")


def _replay_staged(tmp_path, reservation, *, duration: int | None = None,
                   verify_duration: str = "lenient", events: list | None = None):
    return asyncio.run(download_candidate(
        candidate("mp3", duration=duration), Source(b"", extension="mp3", media_type="audio/mpeg"),
        tmp_path, request_id="r", reservation=reservation, verify_duration=verify_duration,
        record=(events if events is not None else []).append))


def test_a_refused_replay_of_a_staged_stream_leaves_nothing_behind(tmp_path):
    # The staged bytes answer to the policy before anything is published, so a
    # refusal cannot be the reason a new file appeared in the library -- and
    # the stage itself is scratch that failed that policy, so it goes too.
    reservation = _staged(tmp_path, mp3_bytes(20))
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        _replay_staged(tmp_path, reservation, duration=210, events=events)
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]
    assert not (tmp_path / "Song.mp3").exists()
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    assert not list(tmp_path.rglob("*.part"))


def test_a_staged_replay_publishes_and_measures_the_file_it_holds(tmp_path):
    data = mp3_bytes(mp3_frames_for(210))
    reservation = _staged(tmp_path, data)
    result = _replay_staged(tmp_path, reservation, duration=210)
    assert (tmp_path / "Song.mp3").read_bytes() == data
    assert not (tmp_path / ".staging" / "t.part").exists()
    assert result.duration_seconds == pytest.approx(210.0, abs=1.0)
    assert result.bitrate_kbps is not None


# --- the policies ------------------------------------------------------------

def test_a_missing_catalogue_duration_is_accepted_and_recorded(tmp_path):
    events = []
    result = download(mp3_bytes(100), extension="mp3", media_type="audio/mpeg",
                      duration=None, tmp_path=tmp_path, events=events)
    assert result.duration_seconds == pytest.approx(mp3_duration(100))
    assert (tmp_path / result.relative_path).exists()
    assert [(event.stage, event.status, event.error_code)
            for event in events if event.stage == "duration"] == [("duration", "unverified", "duration_unverified")]


def test_an_unreadable_container_is_accepted_and_recorded(tmp_path):
    # The bytes validate as mp3 but hold no frame this parser can read, so the
    # length is unknown -- lenient keeps the file and says so.
    events = []
    result = download(b"ID3\x04\x00\x00\x00\x00\x00\x00", extension="mp3", media_type="audio/mpeg",
                      duration=210, tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None and result.bitrate_kbps is None
    assert [(event.stage, event.status, event.error_code)
            for event in events if event.stage == "duration"] == [("duration", "unverified", "duration_unverified")]


UNVERIFIABLE = {
    # A playable mp3 the catalogue gives no length for, and bytes that validate
    # as mp3 but hold no frame this parser can read.
    "no_catalogue_duration": (lambda: mp3_bytes(100), None),
    "unreadable_container": (lambda: b"ID3\x04\x00\x00\x00\x00\x00\x00", 210),
}


@pytest.mark.parametrize("name", sorted(UNVERIFIABLE))
def test_strict_refuses_what_it_cannot_verify(tmp_path, name):
    build, duration = UNVERIFIABLE[name]
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        download(build(), extension="mp3", media_type="audio/mpeg", duration=duration,
                 verify_duration="strict", tmp_path=tmp_path, events=events)
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_an_unknown_policy_is_refused(tmp_path):
    with pytest.raises(MediaError, match="invalid_verify_duration"):
        download(mp3_bytes(100), extension="mp3", media_type="audio/mpeg",
                 verify_duration="whatever", tmp_path=tmp_path, events=[])


# --- a tag between the frames, and the lengths nobody really stated ----------

def id3v2_tag(total: int) -> bytes:
    """An ``ID3v2`` header plus the padding a tag of this total size carries."""
    body = total - 10
    head = bytearray(b"ID3\x04\x00\x00")
    for shift in (21, 14, 7, 0):
        head.append((body >> shift) & 0x7F)
    assert len(head) == 10
    return bytes(head) + b"\x00" * body


def mp3_segmented(first: int, second: int, *, tag_bytes: int = 10) -> bytes:
    """Frames with an ``ID3v2`` tag written between two segments of audio."""
    return mp3_bytes(first) + id3v2_tag(tag_bytes) + mp3_bytes(second)


def test_a_tag_between_two_segments_of_audio_is_not_a_short_recording():
    # The tag sits between two stretches of audio and every frame of both is
    # whole.  Reading the walk as ending at the tag would measure a full song as
    # the piece before it and refuse it, so the walk resumes at the next frame.
    assert container_duration(mp3_segmented(20, 8019)) == pytest.approx(mp3_duration(8039))


def test_a_tag_longer_than_the_resync_window_is_not_a_short_recording():
    # The tag is real and every frame on both sides of it is whole, but it is
    # longer than the window this parser will scan past, so the walk stops on
    # bytes it cannot read and cannot see whether audio continues behind them.
    # Reporting the 0.5 s walked before the tag measured 209.998 seconds of
    # framed audio as a 0.5 s recording and refused it; unknown is the honest
    # answer, and unknown is what ``lenient`` accepts.
    data = mp3_segmented(20, 8019, tag_bytes=600_000)
    assert len(data) > RESYNC_SCAN_BYTES
    assert container_duration(data) is None
    assert mp3_duration(8039) == pytest.approx(209.998, abs=0.001)


def test_a_tag_that_runs_to_the_end_is_a_tag_even_past_the_read_chunk():
    # The same 600 kB tag, but nothing follows it, and the first read chunk
    # ends inside it.  The tag's own length runs to the end of the file, so
    # every frame before it is the recording: measuring the stop from the
    # buffer's end instead of the stop's real position left 238 bytes to
    # compare against a 600 kB tag, and answered "unknown" for a song whose
    # frames had all been counted.
    frames = 2514
    data = mp3_bytes(frames) + id3v2_tag(600_000)
    assert len(data) > SCAN_CHUNK_BYTES
    assert container_duration(data) == pytest.approx(mp3_duration(frames))


def test_a_midstream_tag_is_not_read_as_the_end_of_the_recording():
    # The shape an independent review measured on 2026-09-28: the mid-stream
    # tag is longer than the resync window, the first read chunk ends inside
    # it, and the file ends with a small tag of its own.  Reading the stop as
    # the end of the audio would measure the 65 s before the tag and refuse a
    # 210 s recording.
    data = mp3_segmented(2514, 5525, tag_bytes=600_000) + id3v2_tag(238)
    assert container_duration(data) is None
    assert mp3_duration(2514 + 5525) == pytest.approx(209.998, abs=0.001)


def test_a_tag_that_really_ends_the_file_still_lets_the_audio_be_counted():
    # A tag written after the audio is not audio, so a walk that stopped at one
    # has counted the whole recording -- and it is only trusted when the tag
    # really runs to the end of the file.
    assert container_duration(mp3_bytes(50) + id3v2_tag(4096)) == pytest.approx(mp3_duration(50))


def test_a_tag_between_the_frames_of_a_recording_is_still_delivered(tmp_path):
    # The catalogue states the length and the file really holds it, tag in the
    # middle or not, so nothing about the policy refuses it.
    data = mp3_segmented(20, mp3_frames_for(210) - 20)
    result = download(data, extension="mp3", media_type="audio/mpeg", duration=210,
                      tmp_path=tmp_path, events=[])
    assert result.duration_seconds == pytest.approx(210.0, abs=1.0)
    assert (tmp_path / result.relative_path).exists()


def test_a_catalogue_that_states_no_length_does_not_refuse_a_full_recording(tmp_path):
    # Telegram reports an unknown length as ``0``, and reading that as an
    # expectation would refuse a complete recording for a number the channel
    # never claimed.
    events = []
    result = download(mp3_bytes(mp3_frames_for(210)), extension="mp3", media_type="audio/mpeg",
                      duration=0, tmp_path=tmp_path, events=events)
    assert result.duration_seconds == pytest.approx(210.0, abs=1.0)
    assert (tmp_path / result.relative_path).exists()
    assert [event.error_code for event in events if event.error_code] == ["duration_unverified"]


def test_a_refused_replay_of_a_linked_artifact_leaves_nothing_servable(tmp_path):
    # A crash between linking the target and marking it published leaves a
    # ``publishing`` record.  Resuming it can refuse the file, and a refusal
    # must not leave bytes behind that the library would go on serving.
    from dataclasses import replace

    from fastapi import HTTPException

    from musicdl.admin.portal import _media_target

    reservation = replace(_published(tmp_path, mp3_bytes(mp3_frames_for(210))), state="publishing")
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        _replay(tmp_path, reservation, verify_duration="strict", events=events)
    # Strict says why it could not verify before it refuses, so both codes are
    # part of the record the operator sees.
    assert [event.error_code for event in events if event.error_code] == [
        "duration_unverified", "incomplete_audio"]
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    with pytest.raises(HTTPException):
        _media_target(tmp_path, "Song.mp3")

def test_a_refusal_that_cannot_remove_the_bytes_reports_the_failed_cleanup(tmp_path, monkeypatch):
    # A refusal only leaves nothing behind when the filesystem lets the bytes go.
    # When it does not, the record has to say so rather than report a clean
    # refusal that the library contradicts.
    from dataclasses import replace
    from pathlib import Path as RealPath

    real_unlink = RealPath.unlink

    def refuse(self, *args, **kwargs):
        if self.name == "Song.mp3":
            raise OSError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(RealPath, "unlink", refuse)
    reservation = replace(_published(tmp_path, mp3_bytes(mp3_frames_for(210))), state="publishing")
    events = []
    with pytest.raises(MediaError, match="incomplete_audio"):
        _replay(tmp_path, reservation, verify_duration="strict", events=events)
    assert (tmp_path / "Song.mp3").exists()
    assert [event.error_code for event in events if event.error_code] == [
        "duration_unverified", "cleanup_failed", "incomplete_audio"]
