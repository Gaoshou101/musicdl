import asyncio
from pathlib import Path

import pytest

from musicdl.media import DownloadMetadata, MediaError, download_with_fallback
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult
from musicdl.sources.search import SourceStatus


def candidate(source="a"):
    return Candidate(source_id=source, source_version="1", item_id="i", title="Song", artist="Artist", format="mp3")


async def chunks(*values):
    for value in values:
        yield value

ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"


class Source:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.health_calls = 0

    async def download(self, item):
        if self.fail:
            raise MediaError("download_failed")
        return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg")

    async def health(self):
        self.health_calls += 1
        return True


def test_secret_media_error_is_redacted_in_fallback(tmp_path):
    class Bad(Source):
        async def download(self, item):
            raise MediaError("https://host/?api_key=SECRET")
    events = []
    async def refresh(query, excluded):
        return SearchResult(candidates=(), statuses=(), version="v")
    result = asyncio.run(download_with_fallback(candidate(), {"a": Bad()}, tmp_path, request_id="r", query="q", refresh=refresh, record=events.append))
    assert result.download is None and result.download_error == "download_failed"
    assert all("SECRET" not in repr(x) and "https://" not in repr(x) for x in events)


def result(candidates=()):
    return SearchResult(tuple(candidates), (SourceStatus("a", "1", "ok", len(candidates)),), "v")


def test_success_does_not_enter_fallback(tmp_path):
    source = Source()
    events = []
    calls = []

    async def refresh(query, excluded):
        calls.append((query, excluded))
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.download is not None
    assert outcome.failed_source_id is None
    assert calls == []
    assert source.health_calls == 0
    assert [(e.stage, e.status) for e in events] == [("download", "success")]


def test_failure_refreshes_once_excludes_source_and_checks_health(tmp_path):
    source = Source(fail=True)
    events = []
    calls = []
    replacement = candidate("b")

    async def refresh(query, excluded):
        calls.append((query, excluded))
        return result((replacement,))

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.download is None
    assert outcome.download_error == "download_failed"
    assert outcome.refreshed.candidates == (replacement,)
    assert calls == [("Song", frozenset({"a"}))]
    assert source.health_calls == 1
    assert [(e.stage, e.status) for e in events] == [
        ("download", "failed"), ("refresh", "success"), ("health", "success")
    ]


def test_refresh_failure_still_checks_health_and_redacts_exception(tmp_path):
    source = Source(fail=True)
    events = []

    async def refresh(query, excluded):
        raise RuntimeError("https://x.test/?api_key=SECRET&session=private")

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.refresh_error == "refresh_failed"
    assert outcome.healthy is True
    for secret in ("SECRET", "private", "api_key", "session", "https://x.test/?api_key=SECRET&session=private"):
        assert secret not in str(outcome)
        assert all(secret not in str(event.__dict__) for event in events)
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "download_failed"),
        ("refresh", "failed", "refresh_failed"),
        ("health", "success", None),
    ]


def test_missing_source_refreshes_and_marks_health_unavailable(tmp_path):
    events = []
    calls = []

    async def refresh(query, excluded):
        calls.append(excluded)
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.download_error == "source_unavailable"
    assert outcome.healthy is None
    assert calls == [frozenset({"a"})]
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "source_unavailable"),
        ("refresh", "success", None),
        ("health", "unavailable", "source_unavailable"),
    ]


def test_refresh_containing_failed_source_is_rejected(tmp_path):
    source = Source(fail=True)
    events = []

    async def refresh(query, excluded):
        return result((candidate("a"),))

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.refreshed is None
    assert outcome.refresh_error == "refresh_included_failed_source"
    assert source.health_calls == 1


def test_health_failure_is_redacted(tmp_path):
    class Unhealthy(Source):
        async def health(self):
            self.health_calls += 1
            raise RuntimeError("session=SECRET_API_KEY")

    source = Unhealthy(fail=True)

    async def refresh(query, excluded):
        return result()

    events = []
    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))
    assert outcome.healthy is None
    assert outcome.refresh_error is None
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "download_failed"),
        ("refresh", "success", None),
        ("health", "failed", "health_failed"),
    ]
    for secret in ("SECRET_API_KEY", "session", "api_key"):
        assert secret not in str(outcome)
        assert all(secret not in str(event.__dict__) for event in events)


def test_download_cancellation_does_not_fallback(tmp_path):
    class CancelDownload(Source):
        async def download(self, item):
            raise asyncio.CancelledError

    events = []
    calls = []

    async def refresh(query, excluded):
        calls.append(1)
        return result()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_with_fallback(candidate(), {"a": CancelDownload()}, tmp_path,
            request_id="r", query="Song", refresh=refresh, record=events.append))
    assert calls == []
    assert [(e.stage, e.status) for e in events] == [("download", "failed")]


def test_refresh_cancellation_does_not_check_health(tmp_path):
    source = Source(fail=True)
    events = []

    async def refresh(query, excluded):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
            request_id="r", query="Song", refresh=refresh, record=events.append))
    assert source.health_calls == 0
    assert [(e.stage, e.status) for e in events] == [("download", "failed")]


def test_health_cancellation_stops_after_refresh(tmp_path):
    class CancelHealth(Source):
        async def health(self):
            self.health_calls += 1
            raise asyncio.CancelledError

    source = CancelHealth(fail=True)
    events = []

    async def refresh(query, excluded):
        return result()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
            request_id="r", query="Song", refresh=refresh, record=events.append))
    assert [(e.stage, e.status) for e in events] == [
        ("download", "failed"), ("refresh", "success")
    ]


def test_recorder_failure_does_not_block_fallback_stages(tmp_path):
    source = Source(fail=True)
    calls = []
    async def refresh(query, excluded):
        calls.append(excluded)
        return result()
    def record(_): raise RuntimeError("observer")
    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path, request_id="r", query="q", refresh=refresh, record=record))
    assert outcome.download_error == "download_failed" and outcome.refresh_error is None and outcome.healthy is True
    assert calls == [frozenset({"a"})] and source.health_calls == 1


def test_media_root_resolve_failure_direct_and_fallback(tmp_path, monkeypatch):
    from musicdl.media import download_candidate
    root = Path(tmp_path)
    original = Path.resolve
    def resolve(self, *args, **kwargs):
        if self == root:
            raise OSError("api_key=SECRET")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    events = []
    with pytest.raises(MediaError) as caught:
        asyncio.run(download_candidate(candidate(), Source(), root, request_id="d", record=events.append))
    assert caught.value.code == "download_failed" and caught.value.__suppress_context__
    assert events[0].error_code == "download_failed"
    calls = []
    async def refresh(query, excluded):
        calls.append(excluded)
        return result()
    source = Source()
    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, root, request_id="f", query="q", refresh=refresh))
    assert outcome.download_error == "download_failed" and calls == [frozenset({"a"})] and source.health_calls == 1
    assert "SECRET" not in repr((caught.value, events, outcome))
