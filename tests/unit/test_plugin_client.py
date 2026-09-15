import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from musicdl.contracts.plugin import (HttpAction, HttpObservation, PluginError, PluginManifest,
                                      PluginRequest, PluginResponse, PluginStep)
from musicdl.plugins.broker import ActionDenied
from musicdl.plugins.client import PluginClient
from musicdl.plugins.store import StoredPlugin
from musicdl.sources.models import Candidate


def stored(tmp_path, *, operations=("search",), source="def handle(request):\n    return {'items': []}\n", enabled=True):
    digest = hashlib.sha256(source.encode()).hexdigest()
    manifest = PluginManifest(plugin_id="demo", version="1", language="python", operations=operations,
                              allowed_hosts=("api.example.com",), sha256=digest)
    path = tmp_path / "plugin.py"
    path.write_bytes(source.encode())
    return StoredPlugin(manifest, path, enabled)


def response(request_id, *, result=None, error=None, operation="search"):
    return PluginStep(response=PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id,
        operation=operation, ok=error is None, result=result if error is None else None,
        error=error))


def make_client(tmp_path, steps, broker=None):
    def handler(request):
        value = steps.pop(0)
        return httpx.Response(200, json=value.model_dump(mode="json"))
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
    return PluginClient("http://runner:8080", broker=broker, http_client=http, timeout=2), http


def test_direct_response_and_source_is_sent(tmp_path):
    request_id = uuid4()
    steps = [response(request_id, result={"items": []})]
    client, http = make_client(tmp_path, steps)
    async def run():
        try:
            result = await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=request_id, operation="search"))
            assert result.ok and result.result == {"items": []}
        finally:
            await http.aclose()
    asyncio.run(run())


def test_four_actions_accumulate_and_fifth_is_rejected(tmp_path):
    request_id = uuid4()
    steps = []
    for i in range(4):
        steps.append(PluginStep(action=HttpAction(action_id=f"a{i}", method="GET", url="https://api.example.com/x")))
    steps.append(PluginStep(action=HttpAction(action_id="a4", method="GET", url="https://api.example.com/x")))
    class Broker:
        def fetch(self, action, allowed, *, timeout=None):
            return HttpObservation(action_id=action.action_id, status_code=200, body="")
    client, http = make_client(tmp_path, steps, Broker())
    async def run():
        try:
            with pytest.raises(RuntimeError, match="action_limit"):
                await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=request_id, operation="search"))
        finally: await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("mode,expected", [("repeat", "action_repeated"), ("mismatch", "observation_mismatch"), ("denied", "action_denied")])
def test_action_ids_and_broker_results_are_bounded(tmp_path, mode, expected):
    request_id = uuid4()
    action = HttpAction(action_id="a", method="GET", url="https://api.example.com/x")
    steps = [PluginStep(action=action)]
    if mode == "repeat":
        steps.append(PluginStep(action=action))
    else:
        steps.append(response(request_id, result={"items": []}))
    class Broker:
        def fetch(self, action, allowed, *, timeout=None):
            if mode == "denied": raise ActionDenied("host_denied", "secret")
            return HttpObservation(action_id="wrong" if mode == "mismatch" else action.action_id, status_code=200, body="")
    client, http = make_client(tmp_path, steps, Broker())
    async def run():
        try:
            with pytest.raises(RuntimeError, match=expected):
                await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=request_id, operation="search"))
        finally: await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["timeout", "http", "json"])
def test_runner_failures_are_stable_and_do_not_leak_body(tmp_path, kind):
    def handler(request):
        if kind == "timeout": raise httpx.ReadTimeout("secret timeout")
        if kind == "http": return httpx.Response(500, content=b"SECRET BODY")
        return httpx.Response(200, content=b"not json SECRET")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)
    client = PluginClient("http://runner:8080", broker=None, http_client=http, timeout=1)
    async def run():
        try:
            with pytest.raises(RuntimeError) as exc:
                await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search"))
            assert "SECRET" not in str(exc.value)
        finally: await http.aclose()
    asyncio.run(run())


def test_runner_response_body_is_capped_before_json_parse(tmp_path):
    async def stream(request):
        yield b"{" + b"x" * 65536
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"{" + b"x" * 65536)), trust_env=False, follow_redirects=False)
    client = PluginClient("http://runner:8080", http_client=http, timeout=1)
    async def run():
        try:
            with pytest.raises(RuntimeError, match="runner_response_too_large") as exc:
                await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search"))
            assert "x" not in str(exc.value)
        finally: await http.aclose()
    asyncio.run(run())


def test_runner_stream_failure_is_sanitized_and_response_closed(tmp_path):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{}"
            raise RuntimeError("SECRET")
        async def aclose(self):
            self.closed = True
    stream = Broken(); closed = []
    def handler(request): return httpx.Response(200, stream=stream)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)
    client = PluginClient("http://runner:8080", http_client=http, timeout=1)
    async def run():
        try:
            with pytest.raises(RuntimeError, match="runner_http_error") as exc:
                await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search"))
            assert "SECRET" not in str(exc.value)
        finally: await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_successful_action_loops_accumulate_context(tmp_path, count):
    request_id = uuid4(); steps = []; requests = []
    for i in range(count):
        steps.append(PluginStep(action=HttpAction(action_id=f"a{i}", method="GET", url="https://api.example.com/x")))
    steps.append(response(request_id, result={"items": []}))
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=steps.pop(0).model_dump(mode="json"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)
    class Broker:
        def fetch(self, action, allowed, *, timeout=None):
            return HttpObservation(action_id=action.action_id, status_code=200, body="")
    client = PluginClient("http://runner:8080", broker=Broker(), http_client=http, timeout=2)
    async def run():
        try: await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=request_id, operation="search"))
        finally: await http.aclose()
    asyncio.run(run())
    assert [len(item["actions"]) for item in requests] == list(range(count + 1))
    assert [len(item["observations"]) for item in requests] == list(range(count + 1))


@pytest.mark.parametrize("field", ["request_id", "operation"])
def test_final_response_identity_is_checked(tmp_path, field):
    request_id = uuid4(); kwargs = {field: uuid4() if field == "request_id" else "health"}
    step = PluginStep(response=PluginResponse(protocol="musicdl.plugin/v1", request_id=kwargs.get("request_id", request_id), operation=kwargs.get("operation", "search"), ok=True, result={"items": []}))
    client, http = make_client(tmp_path, [step])
    async def run():
        try:
            with pytest.raises(RuntimeError, match="runner_response_mismatch"):
                await client.invoke(stored(tmp_path), PluginRequest(protocol="musicdl.plugin/v1", request_id=request_id, operation="search"))
        finally: await http.aclose()
    asyncio.run(run())


def test_injected_client_must_prove_transport_policy(tmp_path):
    class Unknown:
        pass
    with pytest.raises(ValueError, match="http_client"):
        PluginClient("http://runner:8080", http_client=Unknown())


def test_source_special_file_and_oversize_are_rejected(tmp_path):
    p = stored(tmp_path)
    p.path.unlink()
    p.path.symlink_to(tmp_path / "target.py")
    (tmp_path / "target.py").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="source"):
        PluginClient._source(p)


def test_digest_and_operation_are_checked_before_send(tmp_path):
    client, http = make_client(tmp_path, [])
    async def run():
        try:
            p = stored(tmp_path)
            p.path.write_bytes(b"tampered")
            with pytest.raises(RuntimeError, match="digest"):
                await client.invoke(p, PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search"))
            with pytest.raises(RuntimeError, match="operation"):
                await client.invoke(stored(tmp_path, operations=("health",)), PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search"))
        finally: await http.aclose()
    asyncio.run(run())


def _candidate():
    return Candidate(
        source_id="demo",
        source_version="1",
        item_id="item-1",
        title="Song",
        artist="Artist",
        album="Album",
        duration=123,
        bitrate=320,
        format="mp3",
        size=456,
    )


def _resolve_response(request_id, result, *, operation="resolve"):
    return PluginStep(response=PluginResponse(
        protocol="musicdl.plugin/v1",
        request_id=request_id,
        operation=operation,
        ok=True,
        result=result,
    ))


def test_resolve_sends_public_candidate_and_returns_typed_descriptor(tmp_path):
    candidate = _candidate()
    captured = []
    descriptor = {
        "candidate_id": candidate.item_id,
        "url": "https://media.example.test/song.mp3?token=opaque",
        "extension": "mp3",
        "media_type": "audio/mpeg",
        "declared_size": 456,
    }

    def handler(request):
        captured.append(json.loads(request.content))
        request_id = captured[-1]["request"]["request_id"]
        return httpx.Response(200, json=_resolve_response(request_id, descriptor).model_dump(mode="json"))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=2)

    async def run():
        try:
            resolved = await client.resolve(
                stored(tmp_path, operations=("resolve",)), candidate, timeout_ms=1234
            )
            assert resolved.candidate_id == candidate.item_id
            assert resolved.url == descriptor["url"]
        finally:
            await http.aclose()

    asyncio.run(run())
    assert len(captured) == 1
    request = captured[0]["request"]
    assert request["operation"] == "resolve"
    assert request["timeout_ms"] == 1234
    assert request["payload"] == {"candidate": candidate.public_representation}
    assert "bytes" not in json.dumps(request)
    assert "base64" not in json.dumps(request).lower()


def test_resolve_rejects_candidate_identity_mismatch(tmp_path):
    candidate = _candidate()
    descriptor = {
        "candidate_id": "different-item",
        "url": "https://media.example.test/song.mp3",
        "extension": "mp3",
        "media_type": "audio/mpeg",
    }
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_resolve_response(
                    json.loads(request.content)["request"]["request_id"], descriptor
                ).model_dump(mode="json"),
            )
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=2)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="candidate_mismatch"):
                await client.resolve(stored(tmp_path, operations=("resolve",)), candidate)
        finally:
            await http.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "candidate_id": "item-1",
            "url": "https://media.example.test/song.mp3",
            "extension": "mp3",
            "media_type": "audio/mpeg",
            "unexpected": "secret",
        },
        {
            "candidate_id": "item-1",
            "url": "http://media.example.test/song.mp3",
            "extension": "mp3",
            "media_type": "audio/mpeg",
        },
    ],
)
def test_resolve_rejects_malformed_descriptor_as_stable_invalid_error(tmp_path, descriptor):
    candidate = _candidate()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_resolve_response(
                    json.loads(request.content)["request"]["request_id"], descriptor
                ).model_dump(mode="json"),
            )
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=2)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="plugin_resolve_invalid") as exc:
                await client.resolve(stored(tmp_path, operations=("resolve",)), candidate)
            assert "media.example" not in str(exc.value)
            assert "secret" not in str(exc.value)
        finally:
            await http.aclose()

    asyncio.run(run())


def test_resolve_maps_runner_timeout_and_plugin_failure_without_leaking_details(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("https://user:secret@media.example.test/song.mp3")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=1)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="plugin_resolve_failed") as exc:
                await client.resolve(stored(tmp_path, operations=("resolve",)), _candidate())
            assert "secret" not in str(exc.value)
            assert "media.example" not in str(exc.value)
        finally:
            await http.aclose()

    asyncio.run(run())


def test_resolve_maps_oversized_runner_response_to_invalid_error(tmp_path):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (64 * 1024 + 1))
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=1)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="plugin_resolve_invalid"):
                await client.resolve(stored(tmp_path, operations=("resolve",)), _candidate())
        finally:
            await http.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("result", [True, False])
def test_health_returns_only_valid_boolean_result(tmp_path, result):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=response(
                    json.loads(request.content)["request"]["request_id"],
                    result=result,
                    operation="health",
                ).model_dump(mode="json"),
            )
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=2)

    async def run():
        try:
            assert await client.health(stored(tmp_path, operations=("health",))) is result
        finally:
            await http.aclose()

    asyncio.run(run())


def test_health_rejects_non_boolean_result_and_undeclared_operation(tmp_path):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=response(
                    json.loads(request.content)["request"]["request_id"],
                    result=1,
                    operation="health",
                ).model_dump(mode="json"),
            )
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=2)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="plugin_health_failed"):
                await client.health(stored(tmp_path, operations=("health",)))
            with pytest.raises(RuntimeError, match="plugin_health_failed"):
                await client.health(stored(tmp_path, operations=("search",)))
        finally:
            await http.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["timeout", "malformed"])
def test_health_failures_are_stable_and_redacted(tmp_path, kind):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("secret health response")
        return httpx.Response(200, content=b"not-json secret")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    )
    client = PluginClient("http://runner:8080", http_client=http, timeout=1)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="plugin_health_failed") as exc:
                await client.health(stored(tmp_path, operations=("health",)))
            assert "secret" not in str(exc.value)
        finally:
            await http.aclose()

    asyncio.run(run())
