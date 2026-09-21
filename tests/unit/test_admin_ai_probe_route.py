"""The panel's test button, as the route the browser actually calls."""

from __future__ import annotations

import asyncio

import httpx

from musicdl.admin import portal as admin_portal
from musicdl.ai.diagnose import AIProbeResult
from musicdl.app import create_app
from musicdl.config import AppSettings

LOGIN = {"username": "operator", "password": "correct-horse-battery"}


def prepared_app(tmp_path):
    settings = AppSettings()
    settings.admin.state_path = str(tmp_path / "admin-state.json")
    app = create_app(settings)
    app.state.admin.auth.change_credentials("admin", LOGIN["username"], LOGIN["password"])
    return app


def run(coro):
    return asyncio.run(coro)


def test_the_route_needs_a_session_and_the_double_submit_pair(tmp_path, monkeypatch):
    app = prepared_app(tmp_path)
    calls = []

    async def fake_probe(settings, *, client=None):
        calls.append(settings)
        return AIProbeResult(ok=True, code=None, detail=None, took_ms=812,
                             model="phase5-model", budget_ms=30_000, reply='{"ok": true}')

    monkeypatch.setattr(admin_portal, "probe_endpoint", fake_probe)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://test") as client:
            anonymous = await client.post("/admin/config/ai-test")
            token = (await client.post("/admin/login", json=LOGIN)).json()["csrf_token"]
            unguarded = await client.post("/admin/config/ai-test")
            allowed = await client.post("/admin/config/ai-test", headers={"x-csrf-token": token})
            audit = await client.get("/admin/audit")
            return anonymous, unguarded, allowed, audit

    anonymous, unguarded, allowed, audit = run(scenario())
    # The application's double-submit guard answers both refusals before the
    # route runs: a POST without a valid session/token pair never reaches it,
    # which is why the probe cannot be triggered from another origin at all.
    assert (anonymous.status_code, unguarded.status_code, allowed.status_code) == (403, 403, 200)
    assert allowed.json() == {"ok": True, "code": None, "detail": None, "took_ms": 812,
                              "model": "phase5-model", "budget_ms": 30_000,
                              "reply": '{"ok": true}'}
    assert len(calls) == 1
    # A refusal is a diagnosis too, so the attempt is written down either way.
    assert any(item.get("action") == "test_ai" for item in audit.json()["items"])


def test_the_route_reports_a_refusal_with_its_status_line(tmp_path, monkeypatch):
    app = prepared_app(tmp_path)

    async def fake_probe(settings, *, client=None):
        return AIProbeResult(ok=False, code="provider_error", detail="HTTP 401", took_ms=137,
                             model="phase5-model", budget_ms=30_000, reply=None)

    monkeypatch.setattr(admin_portal, "probe_endpoint", fake_probe)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://test") as client:
            token = (await client.post("/admin/login", json=LOGIN)).json()["csrf_token"]
            return await client.post("/admin/config/ai-test", headers={"x-csrf-token": token})

    reply = run(scenario()).json()
    assert (reply["ok"], reply["code"], reply["detail"], reply["reply"]) == (
        False, "provider_error", "HTTP 401", None)


def test_a_deployment_with_no_endpoint_says_so_without_leaving_the_process(tmp_path):
    """The default deployment, reached without patching anything."""
    app = prepared_app(tmp_path)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://test") as client:
            token = (await client.post("/admin/login", json=LOGIN)).json()["csrf_token"]
            return await client.post("/admin/config/ai-test", headers={"x-csrf-token": token})

    reply = run(scenario()).json()
    assert (reply["ok"], reply["code"], reply["took_ms"]) == (False, "disabled", 0)
