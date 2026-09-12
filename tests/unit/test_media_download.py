import asyncio
import hashlib
from pathlib import Path

import pytest

from musicdl.media import DownloadMetadata, MediaError, download_candidate
from musicdl.sources.models import Candidate


def candidate(fmt="mp3", item="1"):
    return Candidate(source_id="source", source_version="v1", item_id=item,
                     title="Song", artist="Artist", format=fmt)


class Source:
    def __init__(self, metadata): self.metadata = metadata
    async def download(self, _candidate): return self.metadata
    async def health(self): return True


async def chunks(*values):
    for value in values: yield value


def test_download_publishes_valid_media_atomically(tmp_path):
    data = b"ID3\x04" + b"payload"
    events = []
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(data), extension="mp3", media_type="audio/mpeg")), tmp_path, request_id="r", language="华语", record=events.append))
    target = tmp_path / "华语" / "Artist" / "Song - Artist.mp3"
    assert result.relative_path == Path("华语/Artist/Song - Artist.mp3")
    assert target.read_bytes() == data
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert events[0].stage == "download" and events[0].status == "success"
    assert not list(tmp_path.glob(".musicdl-*.part"))


@pytest.mark.parametrize("metadata,code", [
    (DownloadMetadata(chunks=chunks(), extension="mp3"), "empty_download"),
    (DownloadMetadata(chunks=chunks("ID3"), extension="mp3"), "invalid_chunk"),
    (DownloadMetadata(chunks=chunks(b"ID3"), extension="mp3", declared_size=4), "size_mismatch"),
    (DownloadMetadata(chunks=chunks(b"ID3"), extension="flac"), "extension_mismatch"),
])
def test_download_failures_cleanup(tmp_path, metadata, code):
    events = []
    with pytest.raises(MediaError) as exc:
        asyncio.run(download_candidate(candidate(), Source(metadata), tmp_path, request_id="r", record=events.append, max_bytes=3 if code == "size_mismatch" else 100))
    assert exc.value.code == code
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))
    assert all("secret" not in repr(event) for event in events)


def test_download_collision_and_formats(tmp_path):
    for fmt, header, mime in [("mp3", b"ID3", "audio/mpeg"), ("flac", b"fLaC", "audio/flac"), ("m4a", b"\0\0\0\x18ftypM4A ", "audio/mp4"), ("ogg", b"OggS", "audio/ogg")]:
        c = candidate(fmt, fmt)
        first = asyncio.run(download_candidate(c, Source(DownloadMetadata(chunks=chunks(header), extension=fmt, media_type=mime)), tmp_path, request_id="r", language="华语"))
        asyncio.run(download_candidate(c, Source(DownloadMetadata(chunks=chunks(header), extension=fmt, media_type=mime)), tmp_path, request_id="r", language="华语"))
        assert first.relative_path.suffix == "." + fmt
    files = sorted((tmp_path / "华语" / "Artist").glob("Song*"))
    assert len(files) == 8


def test_download_cancellation_cleans_temp(tmp_path):
    async def never():
        yield b"ID3"
        await asyncio.sleep(10)
    async def run():
        task = asyncio.create_task(download_candidate(candidate(), Source(DownloadMetadata(chunks=never(), extension="mp3")), tmp_path, request_id="r"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert not list(tmp_path.glob(".musicdl-*.part"))
