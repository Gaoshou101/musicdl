import asyncio
import hashlib
from uuid import uuid4

import pytest

from musicdl.contracts.plugin import PluginManifest, PluginRequest, PluginResponse, ResolvedMedia
from musicdl.media.models import DownloadMetadata, MediaError
from musicdl.plugins.store import StoredPlugin
from musicdl.plugins.source import PluginSource
from musicdl.sources.models import Candidate
from musicdl.sources.registry import SourceEntry, SourceRegistry


class FakeClient:
    def __init__(self, result): self.result = result; self.requests = []
    async def invoke(self, stored, request): self.requests.append(request); return self.result


class ResolveClient:
    """Typed resolve/health client double; the legacy invoke path must stay unused."""

    def __init__(self, *, descriptor=None, resolve_error=None, health_result=False, health_error=None):
        self.descriptor = descriptor
        self.resolve_error = resolve_error
        self.health_result = health_result
        self.health_error = health_error
        self.timeout = 30.0
        self.operations = []
        self.resolve_timeouts = []
        self.qualities = []
        self.health_timeouts = []

    async def invoke(self, stored, request):
        self.operations.append(request.operation)
        raise AssertionError(f"operation {request.operation} must not be invoked")

    async def resolve(self, stored, candidate, *, timeout_ms=None, quality=None):
        self.qualities.append(quality)
        self.resolve_timeouts.append(timeout_ms)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.descriptor

    async def health(self, stored, *, timeout_ms=None):
        self.health_timeouts.append(timeout_ms)
        if self.health_error is not None:
            raise self.health_error
        return self.health_result


class FakeTransport:
    def __init__(self, metadata=None):
        self.metadata = metadata
        self.calls = []

    async def open(self, media, *, policy, timeout_ms=None):
        self.calls.append((media, policy, timeout_ms))
        return self.metadata


def descriptor(candidate_id="1", url="https://cdn.example/song.mp3", expires_at=None):
    return ResolvedMedia(candidate_id=candidate_id, url=url, extension="mp3", media_type="audio/mpeg",
                         expires_at=expires_at)


def selected():
    return Candidate(source_id="demo", source_version="1", item_id="1", title="Song", artist="Artist")


def plugin(tmp_path, enabled=True, hosts=()):
    source = "x"
    manifest = PluginManifest(plugin_id="demo", version="1", language="python", operations=("search", "resolve", "health"), allowed_hosts=hosts, sha256=hashlib.sha256(source.encode()).hexdigest())
    path = tmp_path / "x.py"; path.write_bytes(source.encode())
    return StoredPlugin(manifest, path, enabled)


def result(items):
    return PluginResponse(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search", ok=True, result=items)


def test_search_normalizes_and_validates_candidates(tmp_path):
    p = plugin(tmp_path)
    c = Candidate(source_id="demo", source_version="1", item_id="1", title="Song", artist="Artist")
    client = FakeClient(result([c.model_dump(mode="json")]))
    assert asyncio.run(PluginSource(p, client).search("  song   ")) == (c,)
    assert client.requests[0].payload == {"query": "song"}


def test_candidate_identity_and_count_and_disabled(tmp_path):
    p = plugin(tmp_path, enabled=False)
    client = FakeClient(result([]))
    assert asyncio.run(PluginSource(p, client).search("song")) == ()
    p = plugin(tmp_path)
    bad = Candidate(source_id="other", source_version="1", item_id="1", title="S", artist="A")
    with pytest.raises(RuntimeError, match="candidate"):
        asyncio.run(PluginSource(p, FakeClient(result([bad.model_dump(mode="json")]))).search("song"))
    bad_version = Candidate(source_id="demo", source_version="2", item_id="1", title="S", artist="A")
    with pytest.raises(RuntimeError, match="candidate_identity"):
        asyncio.run(PluginSource(p, FakeClient(result([bad_version.model_dump(mode="json")]))).search("song"))
    with pytest.raises(RuntimeError, match="candidate_limit"):
        asyncio.run(PluginSource(p, FakeClient(result([{}] * 101))).search("song"))


def test_source_registry_registration(tmp_path):
    p = plugin(tmp_path)
    source = PluginSource(p, FakeClient(result([])))
    registry = SourceRegistry([SourceEntry("demo", "1", source)])
    assert registry.enabled()[0].source is source


def test_download_resolves_then_streams_under_one_decreasing_deadline(tmp_path):
    p = plugin(tmp_path, hosts=("cdn.example",))
    metadata = DownloadMetadata(chunks=())
    client = ResolveClient(descriptor=descriptor())
    transport = FakeTransport(metadata=metadata)
    source = PluginSource(p, client, transport, resolve_stream_timeout_ms=15000)

    assert asyncio.run(source.download(selected())) is metadata
    assert client.operations == []
    assert len(client.resolve_timeouts) == 1 and len(transport.calls) == 1
    resolved_media, policy, stream_timeout = transport.calls[0]
    # The transport receives the manifest's whole egress policy, not just its
    # host list, so the port and scheme grants travel with the download.
    assert resolved_media == descriptor() and policy == p.manifest.egress
    assert policy.allowed_hosts == ("cdn.example",)
    assert 0 < stream_timeout <= client.resolve_timeouts[0] <= 15000


def test_download_rejects_mismatched_descriptor_before_transport(tmp_path):
    p = plugin(tmp_path, hosts=("cdn.example",))
    transport = FakeTransport(metadata=DownloadMetadata(chunks=()))
    source = PluginSource(p, ResolveClient(descriptor=descriptor(candidate_id="other")), transport)

    with pytest.raises(RuntimeError, match="candidate_mismatch"):
        asyncio.run(source.download(selected()))
    assert transport.calls == []


def test_download_propagates_stable_resolve_code_without_transport(tmp_path):
    p = plugin(tmp_path, hosts=("cdn.example",))
    transport = FakeTransport(metadata=DownloadMetadata(chunks=()))
    source = PluginSource(p, ResolveClient(resolve_error=RuntimeError("plugin_resolve_failed")), transport)

    with pytest.raises(RuntimeError, match="plugin_resolve_failed"):
        asyncio.run(source.download(selected()))
    assert transport.calls == []


def test_download_without_transport_fails_closed(tmp_path):
    source = PluginSource(plugin(tmp_path), ResolveClient(descriptor=descriptor()))
    with pytest.raises(RuntimeError, match="download_failed"):
        asyncio.run(source.download(selected()))


def test_constructor_rejects_invalid_timeout_budgets(tmp_path):
    p = plugin(tmp_path)
    for kwargs in ({"resolve_stream_timeout_ms": 0}, {"resolve_stream_timeout_ms": 30001}, {"health_timeout_ms": 0}):
        with pytest.raises(ValueError, match="invalid_timeout"):
            PluginSource(p, ResolveClient(), FakeTransport(), **kwargs)


def test_health_returns_valid_false_and_raises_stable_failure(tmp_path):
    p = plugin(tmp_path)
    client = ResolveClient(health_result=False)
    assert asyncio.run(PluginSource(p, client, health_timeout_ms=5000).health()) is False
    assert client.health_timeouts == [5000]

    failing = ResolveClient(health_error=RuntimeError("plugin_health_failed"))
    with pytest.raises(RuntimeError, match="plugin_health_failed"):
        asyncio.run(PluginSource(p, failing, FakeTransport()).health())


@pytest.mark.parametrize("quality", [None, "flac", "320k"])
def test_source_propagates_optional_quality(tmp_path, quality):
    client = ResolveClient(descriptor=descriptor())
    transport = FakeTransport(DownloadMetadata(()))
    asyncio.run(PluginSource(plugin(tmp_path), client, transport).download(selected(), quality=quality))
    assert client.qualities == [quality]
    assert transport.calls[0][0].quality is None


class SequenceResolveClient(ResolveClient):
    """A resolve client that answers a different descriptor every time it is asked."""

    def __init__(self, descriptors):
        super().__init__()
        self.descriptors = list(descriptors)

    async def resolve(self, stored, candidate, *, timeout_ms=None, quality=None):
        self.qualities.append(quality)
        self.resolve_timeouts.append(timeout_ms)
        return self.descriptors.pop(0)


RETIRED = "2000-01-01T00:00:00Z"


def test_download_never_opens_a_descriptor_the_source_already_retired(tmp_path):
    """A retired URL is resolved again, and a second retired answer ends the attempt."""
    p = plugin(tmp_path, hosts=("cdn.example",))
    transport = FakeTransport(metadata=DownloadMetadata(chunks=()))
    client = ResolveClient(descriptor=descriptor(expires_at=RETIRED))
    source = PluginSource(p, client, transport, resolve_stream_timeout_ms=15000)

    with pytest.raises(MediaError) as caught:
        asyncio.run(source.download(selected()))
    assert caught.value.code == "media_url_expired"
    assert transport.calls == []
    assert len(client.resolve_timeouts) == 2
    assert 0 < client.resolve_timeouts[1] <= client.resolve_timeouts[0] <= 15000


def test_download_streams_the_fresh_answer_after_the_first_one_expired(tmp_path):
    """The live second answer is what reaches the transport, under one shrinking budget."""
    p = plugin(tmp_path, hosts=("cdn.example",))
    metadata = DownloadMetadata(chunks=())
    transport = FakeTransport(metadata=metadata)
    fresh = descriptor(url="https://cdn.example/fresh.mp3")
    client = SequenceResolveClient([descriptor(expires_at=RETIRED), fresh])
    source = PluginSource(p, client, transport, resolve_stream_timeout_ms=15000)

    assert asyncio.run(source.download(selected())) is metadata
    assert len(transport.calls) == 1 and transport.calls[0][0] == fresh
    assert len(client.resolve_timeouts) == 2
    assert 0 < client.resolve_timeouts[1] <= client.resolve_timeouts[0] <= 15000


def test_download_without_a_stated_expiry_resolves_once(tmp_path):
    """An unstated lifetime is never treated as expired, so nothing resolves twice."""
    p = plugin(tmp_path, hosts=("cdn.example",))
    metadata = DownloadMetadata(chunks=())
    transport = FakeTransport(metadata=metadata)
    client = ResolveClient(descriptor=descriptor())
    source = PluginSource(p, client, transport, resolve_stream_timeout_ms=15000)

    assert asyncio.run(source.download(selected())) is metadata
    assert len(client.resolve_timeouts) == 1 and len(transport.calls) == 1
