import asyncio
import hashlib
from uuid import uuid4

import pytest

from musicdl.contracts.plugin import PluginManifest, PluginRequest, PluginResponse
from musicdl.plugins.store import StoredPlugin
from musicdl.plugins.source import PluginSource
from musicdl.sources.models import Candidate
from musicdl.sources.registry import SourceEntry, SourceRegistry


class FakeClient:
    def __init__(self, result): self.result = result; self.requests = []
    async def invoke(self, stored, request): self.requests.append(request); return self.result


def plugin(tmp_path, enabled=True):
    source = "x"
    manifest = PluginManifest(plugin_id="demo", version="1", language="python", operations=("search",), sha256=hashlib.sha256(source.encode()).hexdigest())
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
