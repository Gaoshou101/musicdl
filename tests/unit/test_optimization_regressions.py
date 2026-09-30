"""Regression coverage for bounded scheduling, caching and WeCom interaction."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError, ResponseError

from musicdl.admin.health import EventLogStore, HealthAggregator
from musicdl.app import _Runtime
from musicdl.sources.lossless_probe import LosslessProbe
from musicdl.sources.models import Candidate
from musicdl.sources.platform_search import PlatformSearch, PlatformSearchError
from musicdl.wecom.client import WeComClient
from musicdl.wecom.results import selection_message, selection_pages
from musicdl.wecom.state import SelectionContext
from musicdl.worker.selection import cancel_user_selection, move_selection_page, get_interaction_selection
from musicdl.worker.stream import _StreamWorker
from musicdl.worker.workers import JobWorker, MessageWorker


def run(coro):
    return asyncio.run(coro)


def candidate(index=1, **changes):
    return Candidate(source_id="src", source_version="1", item_id=str(index),
                     title=changes.get("title", f"Song {index}"),
                     artist=changes.get("artist", "Artist"))


def test_pending_catalogue_fetches_survive_cache_eviction_and_caller_cancellation():
    async def scenario():
        search = PlatformSearch(max_cached_queries=1)
        release = asyncio.Event()
        started = {q: asyncio.Event() for q in ("one", "two")}
        calls = []

        async def fetch(platform, query):
            calls.append(query)
            started[query].set()
            await release.wait()
            return ()

        search._fetch = fetch
        first = asyncio.create_task(search.hits("kw", "one"))
        await started["one"].wait()
        second = asyncio.create_task(search.hits("kw", "two"))
        await started["two"].wait()
        shared = asyncio.create_task(search.hits("kw", "one"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await asyncio.gather(second, shared) == [(), ()]
        assert calls == ["one", "two"]
        assert len(search._cache) == 1 and not search._pending
        await search.aclose()
    run(scenario())


def test_catalogue_failures_retry_and_successes_expire():
    async def scenario():
        search = PlatformSearch(cache_ttl=30)
        fetch = AsyncMock(side_effect=[RuntimeError("unavailable"), (), ()])
        search._fetch = fetch
        with pytest.raises(RuntimeError):
            await search.hits("kw", "song")
        assert await search.hits("kw", "song") == ()
        assert await search.hits("kw", "song") == ()
        assert fetch.await_count == 2
        search._cache[("kw", "song")] = (0, ())
        await search.hits("kw", "song")
        assert fetch.await_count == 3
        await search.aclose()
    run(scenario())


def test_catalogue_admission_is_bounded_and_shutdown_cancels_shared_work():
    async def scenario():
        search = PlatformSearch(max_pending_queries=1, max_concurrency=1)
        started = asyncio.Event()
        async def fetch(*args):
            started.set()
            await asyncio.Event().wait()
        search._fetch = fetch
        task = asyncio.create_task(search.hits("kw", "one"))
        await started.wait()
        with pytest.raises(PlatformSearchError, match="search_busy"):
            await search.hits("kw", "two")
        await search.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not search._pending and not search._cache
        with pytest.raises(PlatformSearchError, match="search_closed"):
            await search.hits("kw", "one")
    run(scenario())


def test_stream_initializes_group_once_and_advances_claim_cursor_per_group():
    async def scenario():
        redis = SimpleNamespace(
            xgroup_create=AsyncMock(),
            xautoclaim=AsyncMock(side_effect=[(b"50-0", []), (b"0-0", [])]),
            xreadgroup=AsyncMock(return_value=[]),
        )
        worker = _StreamWorker()
        worker.redis, worker.consumer = redis, "consumer"
        worker._configure_delivery("{test}", 32000, 3)
        for _ in range(2):
            await worker._ensure_group("stream", "group")
            await worker._read("stream", "group")
        assert redis.xgroup_create.await_count == 1
        assert [call.args[4] for call in redis.xautoclaim.await_args_list] == ["0-0", b"50-0"]
        assert all(call.kwargs["count"] == 1 for call in redis.xreadgroup.await_args_list)
        redis.xautoclaim.side_effect = ResponseError("NOGROUP missing group")
        with pytest.raises(ResponseError):
            await worker._read("stream", "group")
        assert not worker._groups and not worker._claim_cursors
        await worker._ensure_group("stream", "group")
        assert redis.xgroup_create.await_count == 2
    run(scenario())


def test_stream_recovers_connection_errors_but_preserves_cancellation():
    async def scenario():
        worker = _StreamWorker()
        calls = 0
        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RedisConnectionError("unavailable")
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await worker._run_loop(operation, 0.001)
        assert calls == 2
        async def denied():
            raise ResponseError("NOPERM denied")
        with pytest.raises(ResponseError):
            await worker._run_loop(denied, 0.001)
    run(scenario())


def test_health_probes_overlap_and_one_timeout_does_not_hide_others():
    async def scenario():
        started = set()
        all_started = asyncio.Event()
        def probe(name):
            async def call():
                started.add(name)
                if len(started) == 4:
                    all_started.set()
                await all_started.wait()
                if name == "telegram":
                    await asyncio.Event().wait()
                return True
            return call
        report = await HealthAggregator(
            {name: probe(name) for name in ("readyz", "redis", "plugin_runner", "telegram")},
            timeout=0.1).check()
        assert report["checks"] == {"readyz": "ok", "redis": "ok",
                                    "plugin_runner": "ok", "telegram": "unavailable"}
    run(scenario())


def test_event_log_retains_only_newest_records_with_existing_pagination():
    store = EventLogStore(capacity=3)
    for index in range(8):
        store.append({"index": index})
    assert store.page()["items"] == [{"index": i} for i in (5, 6, 7)]
    assert store.page(offset=1, limit=1) == {"items": [{"index": 6}], "total": 3, "offset": 1, "limit": 1}


def test_wecom_reuses_http_client_and_closes_owned_transport():
    async def scenario():
        class Transport(httpx.MockTransport):
            closed = 0
            async def aclose(self):
                self.closed += 1
        transport = Transport(lambda request: httpx.Response(200, json={"errcode": 0}))
        redis = SimpleNamespace(get=AsyncMock(return_value="token"))
        client = WeComClient("corp", "secret", 1, redis, transport=transport)
        await client.send_text("user", "first")
        http = client._http
        await client.send_text("user", "second")
        assert client._http is http and transport.closed == 0
        await client.aclose()
        await client.aclose()
        assert http.is_closed and transport.closed == 1
    run(scenario())


def test_selection_pages_keep_all_numbers_and_fit_utf8_limit():
    rows = [candidate(i, title="歌" * 80, artist="艺" * 60) for i in range(1, 31)]
    pages = selection_pages("query", rows, max_items=10, max_bytes=512)
    numbers = [int(line.split(".", 1)[0]) for page in pages for line in page.splitlines()
               if re.match(r"^[0-9]+[.] ", line)]
    assert numbers == list(range(1, 31))
    assert all(len(page.encode("utf-8")) <= 512 for page in pages)
    assert all("/cancel" in page for page in pages)
    single = [candidate()]
    assert selection_pages("query", single) == (selection_message("query", single),)
    notice = "上一次的结果下载失败，这里是最新的结果："
    refreshed = selection_pages("query", rows, max_items=10, max_bytes=512, notice=notice)
    assert refreshed[0].startswith(notice)
    assert all(not page.startswith(notice) for page in refreshed[1:])
    assert all(len(page.encode("utf-8")) <= 512 for page in refreshed)


def test_selection_mutations_use_atomic_token_scoped_keys_and_original_ttl():
    async def scenario():
        redis = SimpleNamespace(eval=AsyncMock(side_effect=[1, 1]))
        context = SelectionContext("corp", "user", "request", "version", {1: candidate()})
        assert await move_selection_page(redis, "token", context, request_id="reply",
                                         direction=1, page_count=3, namespace="{test}") == 1
        assert await cancel_user_selection(redis, "token", context, namespace="{test}")
        page, cancel = redis.eval.await_args_list
        assert page.args[1] == 4 and cancel.args[1] == 3
        assert all(key.startswith("{test}:") for key in page.args[2:6])
        assert "redis.call('TTL', KEYS[2])" in page.args[0]
        assert all("worker-request" not in key and "selection-job" not in key for key in cancel.args[2:5])
        assert page.args[-3:] == ("token", 1, 3)
    run(scenario())


def test_interaction_reads_frozen_token_instead_of_new_user_route():
    async def scenario():
        redis = SimpleNamespace(eval=AsyncMock(return_value=b"old-token"),
                                get=AsyncMock(return_value=b'{"token":"old-token"}'))
        data = await get_interaction_selection(redis, "corp", "user", "reply", ttl=120)
        assert data == {"token": "old-token"}
        assert redis.eval.await_args.args[-1] == 120
        assert redis.get.await_count == 1
    run(scenario())


def test_runtime_cleanup_attempts_every_dependency_after_failure():
    async def scenario():
        called = []
        def resource(name, broken=False):
            async def close():
                called.append(name)
                if broken:
                    raise RuntimeError("close failed")
            return SimpleNamespace(aclose=close)
        runtime = _Runtime(redis=resource("redis"), state=None, service=None, wecom=resource("wecom"),
                           plugin_client=resource("plugin"), transport=resource("media", True), registry=None,
                           message_worker=None, job_worker=None, lossless_probe=resource("probe"),
                           platform_search=resource("search"))
        with pytest.raises(ExceptionGroup):
            await runtime.aclose()
        assert called == ["probe", "search", "media", "plugin", "wecom", "redis"]
    run(scenario())


def test_lossless_probe_shutdown_cancels_work_before_transport_close():
    async def scenario():
        started = asyncio.Event()
        async def resolve(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        probe = LosslessProbe(resolve, lambda *args, **kwargs: None)
        task = asyncio.create_task(probe.check("source"))
        await started.wait()
        await probe.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not probe._inflight
    run(scenario())
