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


class Source:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.health_calls = 0

    async def download(self, item):
        if self.fail:
            raise MediaError("download_failed")
        return DownloadMetadata(chunks(b"ID3\x04"), extension="mp3", media_type="audio/mpeg")

    async def health(self):
        self.health_calls += 1
        return True


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
