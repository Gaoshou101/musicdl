import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path

import pytest

from musicdl.media import DownloadEvent, DownloadMetadata, MediaError, download_candidate
from musicdl.media.models import _CloseOnce

ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
M4A = b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A "
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


async def failed_chunks():
    yield ID3
    raise RuntimeError("stream failed")


def tracked_metadata(chunks_value, *, extension="mp3", media_type="audio/mpeg", declared_size=None):
    closed = {"count": 0}

    async def close():
        closed["count"] += 1

    return (DownloadMetadata(chunks=chunks_value, extension=extension, media_type=media_type,
                             declared_size=declared_size, _close_once=_CloseOnce(close)), closed)


def test_download_closes_metadata_once_after_success(tmp_path):
    metadata, closed = tracked_metadata(chunks(ID3))
    result = asyncio.run(download_candidate(candidate(), Source(metadata), tmp_path, request_id="r"))
    assert result.size_bytes == len(ID3)
    assert closed["count"] == 1


@pytest.mark.parametrize("stream_factory,code", [
    (lambda: chunks(), "empty_download"),
    (lambda: chunks("ID3"), "invalid_chunk"),
    (lambda: failed_chunks(), "download_failed"),
])
def test_download_closes_metadata_once_after_failure(tmp_path, stream_factory, code):
    chunks_value = stream_factory()
    metadata, closed = tracked_metadata(chunks_value)
    with pytest.raises(MediaError, match=code):
        asyncio.run(download_candidate(candidate(), Source(metadata), tmp_path, request_id="r"))
    assert closed["count"] == 1


def test_download_closes_metadata_once_after_cancellation(tmp_path):
    closed = {"count": 0}

    async def slow_chunks():
        yield ID3
        await asyncio.sleep(10)

    async def close():
        closed["count"] += 1

    metadata = DownloadMetadata(chunks=slow_chunks(), extension="mp3", _close_once=_CloseOnce(close))

    async def run():
        task = asyncio.create_task(download_candidate(candidate(), Source(metadata), tmp_path, request_id="r"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert closed["count"] == 1


def test_metadata_aclose_is_idempotent_under_concurrency():
    closed = {"count": 0}

    async def close():
        await asyncio.sleep(0)
        closed["count"] += 1

    metadata = DownloadMetadata(chunks=(), _close_once=_CloseOnce(close))

    async def run():
        await asyncio.gather(metadata.aclose(), metadata.aclose(), metadata.close())

    asyncio.run(run())
    assert closed["count"] == 1


def test_download_publishes_valid_media_atomically(tmp_path):
    data = ID3 + b"payload"
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
    (DownloadMetadata(chunks=chunks(ID3), extension="mp3", declared_size=4), "size_mismatch"),
    (DownloadMetadata(chunks=chunks(ID3), extension="flac"), "extension_mismatch"),
])
def test_download_failures_cleanup(tmp_path, metadata, code):
    events = []
    with pytest.raises(MediaError) as exc:
        asyncio.run(download_candidate(candidate(), Source(metadata), tmp_path, request_id="r", record=events.append, max_bytes=3 if code == "file_too_large" else 100))
    assert exc.value.code == code
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))
    assert all("secret" not in repr(event) for event in events)


def test_download_collision_and_formats(tmp_path):
    for fmt, header, mime in [("mp3", ID3, "audio/mpeg"), ("flac", b"fLaC", "audio/flac"), ("m4a", M4A, "audio/mp4"), ("ogg", b"OggS", "audio/ogg")]:
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
            return await download_candidate(candidate(item=str(i)), Source(DownloadMetadata(chunks=chunks(ID3), extension="mp3")), tmp_path, request_id=str(i), language="华语")
        return await asyncio.gather(*(one(i) for i in range(4)))
    results = asyncio.run(run())
    paths = [result.relative_path for result in results]
    assert len(set(paths)) == 4
    assert all((tmp_path / path).read_bytes() == ID3 for path in paths)


def test_download_cancellation_cleans_temp(tmp_path):
    async def never():
        yield ID3
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
    data = ID3 + b"payload"
    result = asyncio.run(download_candidate(c, Source(DownloadMetadata(chunks=chunks(data), extension="mp3", declared_size=len(data))), tmp_path, request_id="r"))
    assert result.size_bytes == len(data)


def test_short_writes_are_completed(tmp_path, monkeypatch):
    original = os.write
    def short_write(fd, data): return original(fd, data[:1])
    monkeypatch.setattr(os, "write", short_write)
    data = ID3 + b"payload"
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(data), extension="mp3")), tmp_path, request_id="r"))
    assert (tmp_path / result.relative_path).read_bytes() == data
    assert result.size_bytes == len(data) and result.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("metadata,code", [
    (DownloadMetadata(chunks=chunks(b"ID3xx"), extension="mp3"), "file_too_large"),
    (DownloadMetadata(chunks=chunks(ID3), extension="mp3", media_type="audio/flac"), "mime_mismatch"),
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
        yield ID3
        raise RuntimeError("token=secret")
    events = []
    with pytest.raises(MediaError, match="download_failed") as exc: asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=bad_chunks(), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert len(events) == 1 and "secret" not in repr(events[0]) and "secret" not in repr(exc.value)
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))


def test_fsync_failure_closes_and_cleans(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("token=secret")))
    events = []
    with pytest.raises(MediaError, match="download_failed") as exc: asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(ID3), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert len(events) == 1 and events[0].error_code == "download_failed" and "secret" not in repr(exc.value)
    assert not list(tmp_path.rglob("*.mp3")) and not list(tmp_path.glob(".musicdl-*.part"))


@pytest.mark.parametrize("fmt,header,mime", [("mp3", ID3, "audio/mpeg"), ("flac", b"fLaC", "audio/flac"), ("m4a", M4A, "audio/mp4"), ("ogg", b"OggS", "audio/ogg")])
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
            yield ID3
            await asyncio.sleep(10)
        events = []
        task = asyncio.create_task(download_candidate(candidate(), Source(DownloadMetadata(chunks=slow(), extension="mp3")), tmp_path, request_id="r", record=events.append))
        await asyncio.sleep(0); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert events[-1].stage == "cleanup" and "secret" not in repr(events)
    asyncio.run(run())


def test_post_publication_unlink_retry_rolls_back(tmp_path, monkeypatch):
    original = Path.unlink; calls = {"part": 0}; unlinked = []
    def fail_once(path, *args, **kwargs):
        unlinked.append(path)
        if path.name.startswith(".musicdl-"):
            calls["part"] += 1
            if calls["part"] == 1: raise OSError("token=secret")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_once)
    events = []
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(ID3), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert result.size_bytes == len(ID3) and any(e.status == "success" for e in events)
    assert sum(e.stage == "cleanup" and e.error_code == "cleanup_failed" for e in events) == 1
    assert len(list(tmp_path.rglob("*.mp3"))) == 1 and not list(tmp_path.glob(".musicdl-*.part"))
    assert not any(path.suffix in {".mp3", ".flac", ".m4a", ".ogg"} for path in unlinked)


def test_post_publication_unlink_persistent_rolls_back(tmp_path, monkeypatch):
    original = Path.unlink; unlinked = []
    def refuse(path, *a, **k):
        unlinked.append(path)
        if path.name.startswith(".musicdl-"): raise OSError("token=secret")
        return original(path, *a, **k)
    monkeypatch.setattr(Path, "unlink", refuse)
    events = []
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(ID3), extension="mp3")), tmp_path, request_id="r", record=events.append))
    assert result.size_bytes == len(ID3) and any(e.status == "success" for e in events)
    assert sum(e.stage == "cleanup" and e.error_code == "cleanup_failed" for e in events) == 1
    assert len(list(tmp_path.rglob("*.mp3"))) == 1
    assert not any(path.suffix in {".mp3", ".flac", ".m4a", ".ogg"} for path in unlinked)


def test_recorder_failure_does_not_change_success(tmp_path):
    def record(_): raise RuntimeError("observer")
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(ID3), extension="mp3")), tmp_path, request_id="r", record=record))
    assert result.size_bytes == len(ID3)
    assert (tmp_path / result.relative_path).exists()


def test_concurrent_thread_publication(tmp_path, monkeypatch):
    barrier = threading.Barrier(4)
    calls = {"n": 0}
    lock = threading.Lock()
    original = os.link
    def link(src, dst):
        with lock:
            calls["n"] += 1
            first = calls["n"] <= 4
        if first: barrier.wait(timeout=10)
        return original(src, dst)
    monkeypatch.setattr(os, "link", link)
    def run(i):
        return asyncio.run(download_candidate(candidate(item=str(i)), Source(DownloadMetadata(chunks=chunks(ID3), extension="mp3")), tmp_path, request_id=str(i), language="华语"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert len({r.relative_path for r in results}) == 4
    assert all((tmp_path / r.relative_path).read_bytes() == ID3 for r in results)


def test_direct_secret_error_is_redacted(tmp_path):
    class Bad:
        async def download(self, _): raise MediaError("https://host/?api_key=SECRET")
    events = []
    with pytest.raises(MediaError) as caught:
        asyncio.run(download_candidate(candidate(), Bad(), tmp_path, request_id="r", record=events.append))
    assert caught.value.code == "download_failed" and caught.value.__suppress_context__
    assert events[0].error_code == "download_failed"
    assert "SECRET" not in str(caught.value) and "https://" not in repr(events)


@pytest.mark.parametrize("limit,code", [(0, "invalid_max_bytes"), (-1, "invalid_max_bytes"), (True, "invalid_max_bytes"), (1.5, "invalid_max_bytes")])
def test_invalid_max_bytes(limit, code, tmp_path):
    with pytest.raises(MediaError, match=code):
        asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks=chunks(ID3), extension="mp3")), tmp_path, request_id="r", max_bytes=limit))
    assert not list(tmp_path.rglob("*"))


def test_fsync_not_called_during_cancellation_or_stream_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), (_ for _ in ()).throw(AssertionError("fsync")))[1])
    async def cancelled():
        yield ID3
        raise asyncio.CancelledError
    async def failed():
        yield ID3
        raise MediaError("size_mismatch")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(cancelled(), extension="mp3")), tmp_path, request_id="c"))
    with pytest.raises(MediaError, match="size_mismatch"):
        asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(failed(), extension="mp3")), tmp_path, request_id="f"))
    assert calls == [] and not list(tmp_path.rglob("*.mp3"))


def test_max_bytes_exact_boundary_and_one_over(tmp_path):
    data = ID3 + b"payload"
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks(data), extension="mp3")), tmp_path, request_id="e", max_bytes=len(data)))
    assert result.size_bytes == len(data)
    with pytest.raises(MediaError, match="file_too_large"):
        asyncio.run(download_candidate(candidate(item="over"), Source(DownloadMetadata(chunks(data), extension="mp3")), tmp_path, request_id="o", max_bytes=len(data)-1))
    assert len(list(tmp_path.rglob("*.mp3"))) == 1 and not list(tmp_path.glob(".musicdl-*.part"))

def test_m4a_with_large_ftyp_box_succeeds(tmp_path):
    ftyp = (68).to_bytes(4, "big") + b"ftypM4A " + (0).to_bytes(4, "big") + (b"M4A " * 13)
    data = ftyp + b"audio_data"
    result = asyncio.run(download_candidate(candidate(fmt="m4a"), Source(DownloadMetadata(chunks(data), extension="m4a")), tmp_path, request_id="m4a"))
    assert result.extension == ".m4a"


def test_download_candidate_falls_back_when_link_unsupported(tmp_path, monkeypatch):
    data = ID3 + b"payload"
    def fake_link(src, dst):
        err = OSError("Invalid cross-device link")
        err.errno = 18
        raise err
    monkeypatch.setattr(os, "link", fake_link)
    result = asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks(data), extension="mp3")), tmp_path, request_id="fallback"))
    assert result.size_bytes == len(data)
    assert list(tmp_path.rglob("*.mp3"))


def test_download_candidate_early_stops_when_exceeding_declared_size(tmp_path):
    data = ID3 + b"payload"
    with pytest.raises(MediaError, match="size_mismatch"):
        asyncio.run(download_candidate(candidate(), Source(DownloadMetadata(chunks(data), extension="mp3", declared_size=5)), tmp_path, request_id="early"))
    assert not list(tmp_path.rglob("*.mp3"))
