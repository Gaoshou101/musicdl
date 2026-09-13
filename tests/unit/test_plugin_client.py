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
        def fetch(self, action, allowed):
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
        def fetch(self, action, allowed):
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
