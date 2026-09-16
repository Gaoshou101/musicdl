import asyncio
import hashlib
import json
from uuid import uuid4

import httpx

from musicdl_plugin_runner.app import app
import musicdl_plugin_runner.app as runner_app
from musicdl.contracts.plugin import MAX_INVOCATION_BYTES


def request(method, path, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def body(source="ok"):
    return {"manifest": {"plugin_id": "p", "version": "1", "language": "python", "operations": ["search"], "sha256": hashlib.sha256(source.encode()).hexdigest()}, "source": source, "request": {"protocol": "musicdl.plugin/v1", "request_id": str(uuid4()), "operation": "search"}}


def test_healthz():
    assert request("GET", "/healthz").status_code == 200


def test_invalid_request_is_bounded_error():
    response = request("POST", "/v1/execute", content=json.dumps({"source": "x"}))
    assert response.status_code == 400
    assert "source" not in response.text


def test_endpoint_maps_success_busy_and_failure(monkeypatch):
    class Fake:
        def __init__(self, step): self.step = step
        async def execute(self, _invocation): return self.step
    from musicdl.contracts.plugin import PluginError, PluginResponse, PluginStep
    request_id = uuid4()
    def step(code=None):
        return PluginStep(response=PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id, operation="search", ok=False, error=PluginError(code=code, message="safe")))
    success = PluginStep(response=PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id, operation="search", ok=True, result={"ok": True}))
    monkeypatch.setattr(runner_app, "supervisor", Fake(success))
    assert request("POST", "/v1/execute", json=body()).status_code == 200
    monkeypatch.setattr(runner_app, "supervisor", Fake(step("busy")))
    assert request("POST", "/v1/execute", json=body()).status_code == 429
    monkeypatch.setattr(runner_app, "supervisor", Fake(step("plugin_failed")))
    response = request("POST", "/v1/execute", json=body())
    assert response.status_code == 502 and "safe" in response.text
    for code, expected in (("timeout", 504), ("other", 502)):
        monkeypatch.setattr(runner_app, "supervisor", Fake(step(code)))
        response = request("POST", "/v1/execute", json=body("source-canary"))
        assert response.status_code == expected
        assert all(canary not in response.text for canary in ("source-canary", "payload-canary", "stderr-canary", "internal-exception"))


def test_oversized_stream_rejected():
    source = "x" * MAX_INVOCATION_BYTES
    response = request("POST", "/v1/execute", content=json.dumps(body(source)))
    assert response.status_code == 413
