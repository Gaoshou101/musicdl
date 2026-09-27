import asyncio

import pytest

from musicdl.media import probe
from musicdl.media.models import MediaError
from test_media_transport import media, redirect_transport


def test_head_probes_size_without_reading_body():
    transport, sockets, *_ = redirect_transport([
        b"HTTP/1.1 200 OK\r\nContent-Length: 12345\r\n\r\n"])
    result = asyncio.run(probe.probe_media(transport, media(), policy=("xn--tst-qla.example",)))
    assert result["size"] == 12345
    assert sockets[0].sent.startswith(b"HEAD ")
    assert sockets[0].closed


@pytest.mark.parametrize("range_header,expected", [("bytes 0-0/3456", 3456), ("bytes 0-0/*", None), ("bad", None)])
def test_range_fallback_uses_total_not_fragment(range_header, expected):
    transport, sockets, *_ = redirect_transport([
        b"HTTP/1.1 405 No\r\nContent-Length: 0\r\n\r\n",
        f"HTTP/1.1 206 Partial\r\nContent-Length: 1\r\nContent-Range: {range_header}\r\n\r\n".encode()])
    result = asyncio.run(probe.probe_media(transport, media(), policy=("xn--tst-qla.example",)))
    assert result["size"] == expected
    assert b"Range: bytes=0-0\r\n" in sockets[1].sent
    assert all(sock.closed for sock in sockets)


def test_probe_redirect_enforces_policy():
    transport, sockets, *_ = redirect_transport([
        b"HTTP/1.1 302 Found\r\nLocation: https://private.example/a\r\n\r\n"])
    with pytest.raises(MediaError):
        asyncio.run(probe.probe_media(transport, media(), policy=("xn--tst-qla.example",)))
    assert sockets[0].closed


def test_probe_never_reads_response_body():
    import http.client
    class HeadersOnly(http.client.HTTPResponse):
        def read(self, *args, **kwargs):
            pytest.fail("probe read the body")
    transport, sockets, *_ = redirect_transport([
        b"HTTP/1.1 200 OK\r\n\r\n",
        b"HTTP/1.1 206 Partial\r\nContent-Length: 1\r\nContent-Range: bytes 0-0/777\r\n\r\nx"],
        response_factory=HeadersOnly)
    assert asyncio.run(probe.probe_media(transport, media(), policy=("xn--tst-qla.example",)))["size"] == 777
    assert all(sock.closed for sock in sockets)


@pytest.mark.parametrize("url,addresses,policy", [
    ("https://xn--tst-qla.example/song.mp3", {"xn--tst-qla.example": "127.0.0.1"}, ("xn--tst-qla.example",)),
    ("https://xn--tst-qla.example:444/song.mp3", {}, ("xn--tst-qla.example",)),
    ("http://xn--tst-qla.example/song.mp3", {}, ("xn--tst-qla.example",)),
])
def test_probe_denies_private_ip_port_and_scheme(url, addresses, policy):
    transport, _, connects, *_ = redirect_transport([], addresses=addresses)
    with pytest.raises(MediaError):
        asyncio.run(probe.probe_media(transport, media(url), policy=policy))
    assert connects == []


def make_source(tmp_path, client=None):
    from musicdl.plugins.source import PluginSource
    from test_plugin_source import plugin, ResolveClient, selected
    transport, *_ = redirect_transport([b"HTTP/1.1 200 OK\r\nContent-Length: 22\r\n\r\n"] * 12)
    return PluginSource(plugin(tmp_path, hosts=("xn--tst-qla.example",)),
                        client or ResolveClient(descriptor=media()), transport), selected()


def test_probe_preserves_source_local_item_ids(tmp_path):
    from musicdl.plugins.store import StoredPlugin

    async def scenario():
        source, candidate = make_source(tmp_path)
        other, _ = make_source(tmp_path)
        other.stored = StoredPlugin(
            other.stored.manifest.model_copy(update={"plugin_id": "other"}),
            other.stored.path, True,
        )
        other.transport, *_ = redirect_transport([
            b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n"])
        results = await probe.probe_candidates(
            [candidate, candidate.model_copy(update={"source_id": "other"})],
            {"demo": source, "other": other},
        )
        assert results == {
            "demo": {"1": {**probe.UNKNOWN, "size": 22, "extension": "mp3", "media_type": "audio/mpeg"}},
            "other": {"1": {**probe.UNKNOWN, "size": 99, "extension": "mp3", "media_type": "audio/mpeg"}},
        }

    asyncio.run(scenario())


def test_cache_ttl_capacity_and_quality_context(tmp_path):
    async def scenario():
        source, candidate = make_source(tmp_path)
        now = [0.0]
        cache = probe.ProbeCache(capacity=2, clock=lambda: now[0])
        async def get(c=candidate):
            return await probe.probe_candidates([c], {"demo": source}, cache=cache)
        assert (await get())["demo"]["1"]["size"] == 22
        await get()
        assert len(source.client.resolve_timeouts) == 1
        await get(candidate.model_copy(update={"qualities": ("flac",)}))
        assert len(source.client.resolve_timeouts) == 2
        now[0] = 61
        await get()
        assert len(source.client.resolve_timeouts) == 3
        await get(candidate.model_copy(update={"qualities": ("320k",)}))
        assert len(cache.cache) == 2
    asyncio.run(scenario())


def test_cache_does_not_cross_source_or_egress_changes(tmp_path):
    from musicdl.plugins.store import StoredPlugin
    async def scenario():
        source, candidate = make_source(tmp_path)
        cache = probe.ProbeCache()
        await probe.probe_candidates([candidate], {"demo": source}, cache=cache)
        source.stored = StoredPlugin(source.stored.manifest.model_copy(update={"allowed_hosts": ("other.example",)}), source.stored.path, True)
        result = await probe.probe_candidates([candidate], {"demo": source}, cache=cache)
        assert result["demo"]["1"] == probe.UNKNOWN
        assert len(source.client.resolve_timeouts) == 2
    asyncio.run(scenario())


def test_concurrency_timeout_and_cleanup(tmp_path, monkeypatch):
    from test_plugin_source import ResolveClient
    monkeypatch.setattr(probe, "ITEM_TIMEOUT", 0.03)
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT", 0.05)
    class Slow(ResolveClient):
        active = 0
        peak = 0
        async def resolve(self, *args, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(20)
            finally:
                self.active -= 1
    async def scenario():
        client = Slow()
        source, candidate = make_source(tmp_path, client)
        cache = probe.ProbeCache()
        candidates = [candidate.model_copy(update={"item_id": str(i)}) for i in range(10)]
        result = await probe.probe_candidates(candidates, {"demo": source}, cache=cache)
        await asyncio.sleep(0.01)
        assert all(value == probe.UNKNOWN for value in result["demo"].values())
        assert client.peak == 4 and client.active == 0
        assert not cache.tasks
    asyncio.run(scenario())


def test_cancellation_closes_acquired_socket():
    import threading
    import time
    from musicdl.media.transport import SecureMediaTransport
    from test_media_transport import FakeSocket
    import socket
    started = threading.Event()
    sock = FakeSocket(b"")
    def connect(*args):
        started.set()
        time.sleep(0.03)
        return sock
    transport = SecureMediaTransport(resolver=lambda *_: [(socket.AF_INET, ("93.184.216.34", 443))], connector=connect)
    async def scenario():
        task = asyncio.create_task(probe.probe_media(transport, media(), policy=("xn--tst-qla.example",)))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert sock.closed


@pytest.mark.parametrize("status", [403, 405, 501])
def test_head_rejection_falls_back_then_returns_unknown_on_failure(status):
    transport, sockets, *_ = redirect_transport([
        f"HTTP/1.1 {status} Error\r\nContent-Length: 0\r\n\r\n".encode(),
        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"])
    assert asyncio.run(probe.probe_media(transport, media(), policy=("xn--tst-qla.example",))) == probe.UNKNOWN
    assert all(sock.closed for sock in sockets)


def test_request_deadline_does_not_wait_for_late_cleanup(tmp_path, monkeypatch):
    import time
    from test_plugin_source import ResolveClient
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT", 0.03)
    class SlowCleanup(ResolveClient):
        async def resolve(self, *args, **kwargs):
            try:
                await asyncio.sleep(20)
            finally:
                await asyncio.sleep(0.08)
    async def scenario():
        source, candidate = make_source(tmp_path, SlowCleanup())
        cache = probe.ProbeCache()
        start = time.monotonic()
        result = await probe.probe_candidates([candidate], {"demo": source}, cache=cache)
        assert time.monotonic() - start < 0.075
        assert result["demo"]["1"] == probe.UNKNOWN
        assert cache.tasks  # cleanup retains ownership and its concurrency slot
        await asyncio.gather(*cache.tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert not cache.tasks and cache.slots._value == 4
    asyncio.run(scenario())


def test_admission_limit_across_parallel_requests(tmp_path, monkeypatch):
    from test_plugin_source import ResolveClient
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT", 0.03)
    class Waiting(ResolveClient):
        async def resolve(self, *args, **kwargs):
            await asyncio.sleep(20)
    async def scenario():
        source, candidate = make_source(tmp_path, Waiting())
        cache = probe.ProbeCache()
        calls = [asyncio.create_task(probe.probe_candidates([candidate] * 10, {"demo": source}, cache=cache)) for _ in range(10)]
        await asyncio.sleep(0.01)
        assert len(cache.tasks) <= 40 and cache.slots._value == 0
        await asyncio.gather(*calls)
        await asyncio.sleep(0.01)
        assert not cache.tasks
    asyncio.run(scenario())


def test_slow_dns_cleanup_keeps_global_probe_slots(tmp_path, monkeypatch):
    import socket
    import threading
    from musicdl.media.transport import SecureMediaTransport
    monkeypatch.setattr(probe, "ITEM_TIMEOUT", 0.02)
    monkeypatch.setattr(probe, "REQUEST_TIMEOUT", 0.04)
    release = threading.Event()
    calls = []
    def resolver(*args):
        calls.append(args)
        release.wait(1)
        return [(socket.AF_INET, ("93.184.216.34", 443))]
    async def scenario():
        source, candidate = make_source(tmp_path)
        source.transport = SecureMediaTransport(resolver=resolver, connector=lambda *_: pytest.fail("cancelled DNS must not connect"))
        cache = probe.ProbeCache()
        try:
            result = await probe.probe_candidates([candidate] * 10, {"demo": source}, cache=cache)
            assert result["demo"]["1"] == probe.UNKNOWN
            assert len(calls) == 4 and cache.slots._value == 0
        finally:
            release.set()
            await asyncio.gather(*cache.tasks, return_exceptions=True)
        assert cache.slots._value == 4
    asyncio.run(scenario())


def test_queued_item_expires_without_starting_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ITEM_TIMEOUT", 0.02)

    async def scenario():
        source, candidate = make_source(tmp_path)
        cache = probe.ProbeCache()
        for _ in range(4):
            await cache.slots.acquire()
        task = asyncio.create_task(cache.one(candidate, source))
        try:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            assert task in done, "queued item must expire while admission slots remain occupied"
            assert task.result() == probe.UNKNOWN
            assert source.client.resolve_timeouts == []
            assert cache.slots._value == 0
        finally:
            for _ in range(4):
                cache.slots.release()
            await asyncio.gather(task, return_exceptions=True)
        assert cache.slots._value == 4

    asyncio.run(scenario())
