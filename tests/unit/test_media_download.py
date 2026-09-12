import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from musicdl.media import DownloadEvent, DownloadMetadata, MediaError, download_candidate
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
    assert events == [DownloadEvent("r", "1", "source", "v1", "download", "success", size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), relative_path="华语/Artist/Song - Artist.mp3")]
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
        base = tmp_path / "华语" / "Artist" / f"Song - Artist.{fmt}"
        base.parent.mkdir(parents=True, exist_ok=True); base.write_bytes(b"original")
        first = asyncio.run(download_candidate(c, Source(DownloadMetadata(chunks=chunks(header), extension=fmt, media_type=mime)), tmp_path, request_id="r", language="华语"))
        second = asyncio.run(download_candidate(c, Source(DownloadMetadata(chunks=chunks(header), extension=fmt, media_type=mime)), tmp_path, request_id="r", language="华语"))
        assert first.relative_path.suffix == "." + fmt
        assert first.relative_path.stem.endswith(" (2)") and second.relative_path.stem.endswith(" (3)")
        assert base.read_bytes() == b"original"
    files = sorted((tmp_path / "华语" / "Artist").glob("Song*"))
    assert len(files) == 12


def test_concurrent_publication_has_unique_targets(tmp_path):
    async def run():
        async def one(i):
            return await download_candidate(candidate(item=str(i)), Source(DownloadMetadata(chunks=chunks(b"ID3"), extension="mp3")), tmp_path, request_id=str(i), language="华语")
        return await asyncio.gather(*(one(i) for i in range(4)))
    results = asyncio.run(run())
    paths = [result.relative_path for result in results]
    assert len(set(paths)) == 4
    assert all((tmp_path / path).read_bytes() == b"ID3" for path in paths)


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


def test_candidate_size_is_not_authoritative(tmp_path):
    c = candidate(); c = c.model_copy(update={"size": 999})
    data = b"ID3payload"
    result = asyncio.run(download_candidate(c, Source(DownloadMetadata(chunks=chunks(data), extension="mp3", declared_size=len(data))), tmp_path, request_id="r"))
    assert result.size_bytes == len(data)


def test_short_writes_are_completed(tmp_path, monkeypatch):
    original = os.write
    def short_write(fd, data): return original(fd, data[:1])
    monkeypatch.setattr(os, "write", short_write)
    data = b"ID3payload"
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(data), extension="mp3")), tmp_path, request_id="r"))
    assert (tmp_path / result.relative_path).read_bytes() == data
    assert result.size_bytes == len(data) and result.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("metadata,code", [
    (DownloadMetadata(chunks=chunks(b"ID3xx"), extension="mp3"), "file_too_large"),
    (DownloadMetadata(chunks=chunks(b"ID3"), extension="mp3", media_type="audio/flac"), "mime_mismatch"),
    (DownloadMetadata(chunks=chunks(b"nope"), extension="mp3"), "signature_mismatch"),
])
def test_failure_matrix_exact_event(tmp_path, metadata, code):
    events = []
    with pytest.raises(MediaError) as exc: asyncio.run(download_candidate(candidate(), Source(metadata), tmp_path, request_id="r", record=events.append, max_bytes=3 if code == "file_too_large" else 100))
    assert exc.value.code == code and len(events) == 1 and events[0].error_code == code
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))


def test_source_and_generator_errors_redacted(tmp_path):
    class BadSource:
        async def download(self, _): raise RuntimeError("token=secret")
    events = []
    with pytest.raises(MediaError, match="download_failed") as exc: asyncio.run(download_candidate(candidate(), BadSource(), tmp_path, request_id="r", record=events.append))
    assert len(events) == 1 and "secret" not in repr(events[0]) and "secret" not in repr(exc.value)
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))

    async def bad_chunks():
        yield b"ID3"
        raise RuntimeError("token=secret")
    events = []
    with pytest.raises(MediaError, match="download_failed") as exc: asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=bad_chunks(), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert len(events) == 1 and "secret" not in repr(events[0]) and "secret" not in repr(exc.value)
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))


def test_fsync_failure_closes_and_cleans(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("token=secret")))
    events = []
    with pytest.raises(MediaError, match="download_failed") as exc: asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(b"ID3"), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert len(events) == 1 and events[0].error_code == "download_failed" and "secret" not in repr(exc.value)
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))


@pytest.mark.parametrize("fmt,header,mime", [("mp3", b"ID3", "audio/mpeg"), ("flac", b"fLaC", "audio/flac"), ("m4a", b"\0\0\0\x18ftypM4A ", "audio/mp4"), ("ogg", b"OggS", "audio/ogg")])
def test_parametrized_collision_matrix(tmp_path, fmt, header, mime):
    base = tmp_path / "华语" / "Artist" / f"Song - Artist.{fmt}"
    base.parent.mkdir(parents=True); base.write_bytes(b"original")
    def run(): return asyncio.run(download_candidate(candidate(fmt), Source(DownloadMetadata(chunks=chunks(header), extension=fmt, media_type=mime)), tmp_path, request_id="r", language="华语"))
    second, third = run(), run()
    assert second.relative_path.stem.endswith(" (2)") and third.relative_path.stem.endswith(" (3)")
    assert base.read_bytes() == b"original"


def test_cleanup_failure_after_primary_is_observable(tmp_path, monkeypatch):
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path.name.startswith(".musicdl-"): raise OSError("token=secret")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    events = []
    with pytest.raises(MediaError, match="empty_download") as exc: asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert exc.value.code == "empty_download" and events[-1].stage == "cleanup" and events[-1].error_code == "cleanup_failed"
    assert "secret" not in repr(events) and "secret" not in repr(exc.value)


def test_cleanup_failure_on_cancellation_is_observable(tmp_path, monkeypatch):
    original = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda path, *a, **k: (_ for _ in ()).throw(OSError("token=secret")) if path.name.startswith(".musicdl-") else original(path, *a, **k))
    async def run():
        async def slow():
            yield b"ID3"
            await asyncio.sleep(10)
        events = []
        task = asyncio.create_task(download_candidate(candidate(), Source(DownloadMetadata(chunks=slow(), extension="mp3")), tmp_path, request_id="r", record=events.append))
        await asyncio.sleep(0); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert events[-1].stage == "cleanup" and "secret" not in repr(events)
    asyncio.run(run())


def test_post_publication_unlink_retry_rolls_back(tmp_path, monkeypatch):
    original = Path.unlink; calls = {"part": 0}
    def fail_once(path, *args, **kwargs):
        if path.name.startswith(".musicdl-"):
            calls["part"] += 1
            if calls["part"] == 1: raise OSError("token=secret")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_once)
    events = []
    with pytest.raises(MediaError, match="cleanup_failed") as exc: asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(b"ID3"), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert not any(e.status == "success" for e in events)
    assert sum(e.stage == "cleanup" and e.error_code == "cleanup_failed" for e in events) == 1
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))
    assert "secret" not in repr(exc.value)


def test_post_publication_unlink_persistent_rolls_back(tmp_path, monkeypatch):
    original = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda path, *a, **k: (_ for _ in ()).throw(OSError("token=secret")) if path.name.startswith(".musicdl-") else original(path, *a, **k))
    events = []
    with pytest.raises(MediaError, match="cleanup_failed"): asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(b"ID3"), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert not any(e.status == "success" for e in events)
    assert sum(e.stage == "cleanup" and e.error_code == "cleanup_failed" for e in events) == 1
    assert not list(tmp_path.rglob("*.mp3"))
