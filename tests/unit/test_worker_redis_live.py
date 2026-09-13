"""Live Redis 7 integration coverage for the pipeline-A workers.

These tests intentionally keep all state in a unique namespace and only run when
MUSICDL_TEST_REDIS_URL is supplied by the test invocation.
"""

import asyncio
import hashlib
import json
import os
import secrets
from types import SimpleNamespace

import pytest

from musicdl.sources.models import Candidate
from musicdl.sources.registry import SourceEntry, SourceRegistry
from musicdl.sources.search import SearchResult
from musicdl.wecom.state import RedisStateStore
from musicdl.worker.workers import JobWorker, MessageWorker


REDIS_URL = os.getenv("MUSICDL_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason="MUSICDL_TEST_REDIS_URL is not configured"
)


def _run(coro):
    return asyncio.run(coro)


def _candidate() -> Candidate:
    return Candidate(
        source_id="live-source",
        source_version="1",
        item_id="live-item",
        title="Live Song",
        artist="Live Artist",
        format="mp3",
    )


class _WeCom:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, user: str, content: str):
        self.sent.append((user, content))
        return {}


def _namespace() -> str:
    return "{musicdl-test-" + secrets.token_hex(12) + "}"


async def _new_client():
    redis = pytest.importorskip("redis.asyncio")
    client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
    await client.ping()
    return client


async def _cleanup(client, namespace: str):
    keys = [key async for key in client.scan_iter(match=f"{namespace}*")]
    if keys:
        await client.delete(*keys)


def test_live_redis_search_selection_job_download_path(monkeypatch):
    _run(_test_live_redis_search_selection_job_download_path(monkeypatch))


async def _test_live_redis_search_selection_job_download_path(monkeypatch):
    client = await _new_client()
    namespace = _namespace()
    try:
        state = RedisStateStore(client, namespace=namespace)
        candidate = _candidate()
        async def search(_registry, query, **_kwargs):
            assert query == "live query"
            return SearchResult((candidate,), (), "live-version")

        async def download(_candidate, _sources, _media_root, **_kwargs):
            return SimpleNamespace(
                download=SimpleNamespace(relative_path="Live Song.mp3"),
                download_error=None,
                refresh_error=None,
            )

        monkeypatch.setattr("musicdl.worker.workers.search_sources", search)
        monkeypatch.setattr("musicdl.worker.workers.download_with_fallback", download)
        registry = SourceRegistry((SourceEntry("live-source", "1", lambda _q: []),))
        wecom = _WeCom()
        message_worker = MessageWorker(
            client, registry, wecom, state=state,
            group=namespace + ":message-group", consumer="message-live",
        )
        job_worker = JobWorker(
            client, wecom, {}, "unused", state=state,
            refresh=lambda *_args: SearchResult((), (), "refresh"),
            group=namespace + ":job-group", consumer="job-live",
        )

        search_request = "search-" + secrets.token_hex(6)
        queued = await state.enqueue_message(
            "corp-live", "user-live", search_request,
            {"command": "search", "value": "live query"},
        )
        assert queued.duplicate is False
        assert await message_worker.run_once() == 1
        selection = await client.get(
            f"{namespace}:worker-user:" + hashlib.sha256(b"corp-live:user-live").hexdigest()
        )
        assert selection
        selection_request = "select-" + secrets.token_hex(6)
        await state.enqueue_message(
            "corp-live", "user-live", selection_request,
            {"command": "select", "value": 1},
        )
        assert await job_worker.run_selection_once() == 1
        assert await client.xlen(state.job_stream) == 1
        assert await job_worker.run_once() == 1
        assert wecom.sent[0][0] == "user-live"
        assert wecom.sent[0][1].startswith("1. Live Song")
        assert wecom.sent[-1] == ("user-live", "下载成功：Live Song.mp3")
        job_pending = await client.xpending(state.job_stream, job_worker.group)
        assert job_pending["pending"] == 0
    finally:
        await _cleanup(client, namespace)
        await client.aclose()


def test_live_redis_xautoclaim_retry_dead_letter_is_sanitized(monkeypatch):
    _run(_test_live_redis_xautoclaim_retry_dead_letter_is_sanitized(monkeypatch))


async def _test_live_redis_xautoclaim_retry_dead_letter_is_sanitized(monkeypatch):
    client = await _new_client()
    namespace = _namespace()
    try:
        state = RedisStateStore(client, namespace=namespace)
        async def failing_search(*_args, **_kwargs):
            raise RuntimeError("secret exception detail")

        monkeypatch.setattr("musicdl.worker.workers.search_sources", failing_search)
        wecom = _WeCom()
        worker = MessageWorker(
            client, SourceRegistry(), wecom, state=state,
            group=namespace + ":retry-group", consumer="retry-live",
            pending_idle_ms=1, max_attempts=2,
        )
        request_id = "retry-" + secrets.token_hex(6)
        await state.enqueue_message(
            "corp-retry", "user-retry", request_id,
            {"command": "search", "value": "private query"},
        )
        assert await worker.run_once() == 0
        pending = await client.xpending(state.message_stream, worker.group)
        assert pending["pending"] == 1
        assert wecom.sent == []
        await asyncio.sleep(0.02)
        assert await worker.run_once() == 0
        pending = await client.xpending(state.message_stream, worker.group)
        assert pending["pending"] == 0
        dead_letters = await client.xrange(worker.dead_letter_stream)
        assert len(dead_letters) == 1
        fields = dead_letters[0][1]
        assert fields[b"reason"] == b"business_failure"
        assert fields[b"attempts"] == b"2"
        serialized = json.dumps({key.decode(): value.decode() for key, value in fields.items()})
        assert "private query" not in serialized
        assert "secret exception detail" not in serialized
        assert wecom.sent == [("user-retry", "处理失败，请稍后重试。")]
    finally:
        await _cleanup(client, namespace)
        await client.aclose()
