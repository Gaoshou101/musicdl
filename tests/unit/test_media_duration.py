"""Real playing time, read out of a finished file, and what it refuses.

Every sample here is built by hand in a few kilobytes, so the suite stays
honest about what it measures and needs no encoder, no ffmpeg and no fixture
binary in the repository.

One limit is worth stating plainly rather than hiding: FLAC states its length
in a header, so a FLAC file whose header survived and whose audio was cut
afterwards still reports the length it claims.  MP4 is caught when its box
chain reaches past the end of the file, which is what a cut behind the header
leaves behind.  What this module
catches is the file that *is* short -- the 30-second preview a platform serves
in place of the song, or a stream that stopped early in a container that
counts what it really holds (MP3 without a ``Xing`` tag) -- and it answers
``None``, never a guess, for anything it cannot read.
"""

import asyncio
import hashlib

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


def flac_bytes(total_samples: int, rate: int = FLAC_RATE) -> bytes:
    """A ``fLaC`` file whose ``STREAMINFO`` states exactly this many samples."""
    packed = (rate << 44) | total_samples
    body = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
    assert len(body) == 34
    block = b"\x00" + len(body).to_bytes(3, "big") + body
    return b"fLaC" + block + b"\x00" * 64


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


def mp4_bytes(seconds: float, timescale: int = 1000) -> bytes:
    """An ``ftyp`` box, then a ``moov``/``mvhd`` stating this length."""
    duration = int(seconds * timescale)
    mvhd_body = bytes([0, 0, 0, 0]) + b"\x00" * 8 + timescale.to_bytes(4, "big") + duration.to_bytes(4, "big") + b"\x00" * 80
    mvhd = (len(mvhd_body) + 8).to_bytes(4, "big") + b"mvhd" + mvhd_body
    moov = (len(mvhd) + 8).to_bytes(4, "big") + b"moov" + mvhd
    ftyp = (20).to_bytes(4, "big") + b"ftypM4A " + b"\x00\x00\x00\x00" + b"M4A "
    # A real file carries its audio.  Only ``mvhd`` is read here, but a fixture
    # with no payload would not be a file this module could meet.
    audio = b"\x00" * 64
    mdat = (len(audio) + 8).to_bytes(4, "big") + b"mdat" + audio
    return ftyp + moov + mdat


class Source:
    def __init__(self, data: bytes, *, extension: str, media_type: str):
        self.data, self.extension, self.media_type = data, extension, media_type

    async def download(self, _candidate):
        async def chunks():
            yield self.data

        return DownloadMetadata(chunks=chunks(), extension=self.extension, media_type=self.media_type,
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
        download(mp4_bytes(210.0)[:-40], extension="m4a", media_type="audio/mp4", duration=210,
                 verify_duration="strict", tmp_path=tmp_path, events=events)
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_an_mp4_cut_behind_its_header_is_accepted_by_lenient_and_recorded(tmp_path):
    # Lenient fails open on a length it cannot read, which is what keeps the
    # sources that never state one working; the record says so.
    events = []
    result = download(mp4_bytes(210.0)[:-40], extension="m4a", media_type="audio/mp4", duration=210,
                      tmp_path=tmp_path, events=events)
    assert result.duration_seconds is None and result.bitrate_kbps is None
    assert [(event.stage, event.status, event.error_code)
            for event in events if event.stage == "duration"] == [
        ("duration", "unverified", "duration_unverified")]


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
    "flac": (lambda: flac_bytes(int(30 * FLAC_RATE)), "flac", "audio/flac"),
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
        download(flac_bytes(int(30 * FLAC_RATE)), extension="flac", media_type="audio/flac",
                 duration=210, tmp_path=tmp_path, events=events)
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    assert not list(tmp_path.rglob("*.part"))


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
