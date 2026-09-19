"""The deployed application must expose its administration portal."""

from __future__ import annotations

import asyncio
import base64

import httpx
from pydantic import SecretStr

from musicdl.app import create_app
from musicdl.config import AppSettings, WeComSettings
from musicdl.sources import SourceEntry, SourceRegistry

DEFAULT_LOGIN = {"username": "admin", "password": "password"}
CHANGED_LOGIN = {"username": "operator", "password": "correct-horse-battery"}
AES_KEY = base64.b64encode(b"k" * 32).decode().rstrip("=")


class IdleWorker:
    async def run_forever(self) -> None:
        await asyncio.Event().wait()


class PingState:
    async def ping(self) -> bool:
        return True


class StubSource:
    async def search(self, query: str):
        return ()


class Runtime:
    """Minimal stand-in for the runtime the application assembles at startup."""

    def __init__(self, registry: SourceRegistry) -> None:
        self.state = PingState()
        self.service = object()
        self.registry = registry
        self.message_worker = IdleWorker()
        self.job_worker = IdleWorker()

    async def aclose(self) -> None:
        return None


def wecom_enabled() -> AppSettings:
    return AppSettings(wecom=WeComSettings(enabled=True, corp_id="corp", agent_id=7,
                                           token=SecretStr("token"), secret=SecretStr("secret"),
                                           encoding_aes_key=SecretStr(AES_KEY),
                                           allowed_users=["user"]))


def build(*, admin: dict | None = None, runtime: Runtime | None = None):
    if runtime is None:
        return create_app(AppSettings(admin=admin or {}))
    return create_app(wecom_enabled(), runtime_factory=lambda _settings: runtime)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def run(coro):
    return asyncio.run(coro)


def test_admin_portal_serves_a_login_page_while_its_api_requires_a_session():
    async def scenario():
        app = build()
        async with client_for(app) as client:
            page = await client.get("/admin/")
            api = await client.get("/admin/sources")
        assert page.status_code == 200
        assert 'action="/admin/login-form"' in page.text
        assert "登录" in page.text
        assert api.status_code == 401

    run(scenario())


def test_default_credentials_force_a_change_on_the_dashboard():
    async def scenario():
        app = build()
        async with client_for(app) as client:
            login = await client.post("/admin/login", json=DEFAULT_LOGIN)
            assert login.status_code == 200
            payload = login.json()
            assert payload["must_change"] is True
            assert payload["csrf_token"]
            dashboard = await client.get("/admin/")
        assert dashboard.status_code == 200
        assert "SECURITY WARNING" in dashboard.text

    run(scenario())


def test_changing_credentials_invalidates_the_default_password():
    async def scenario():
        app = build()
        async with client_for(app) as client:
            payload = (await client.post("/admin/login", json=DEFAULT_LOGIN)).json()
            changed = await client.post(
                "/admin/change-credentials",
                json={"password": DEFAULT_LOGIN["password"], "username": CHANGED_LOGIN["username"],
                      "new_password": CHANGED_LOGIN["password"]},
                headers={"x-csrf-token": payload["csrf_token"]})
            assert changed.status_code == 200
        async with client_for(app) as fresh:
            old = await fresh.post("/admin/login", json=DEFAULT_LOGIN)
            new = await fresh.post("/admin/login", json=CHANGED_LOGIN)
        assert old.status_code == 401
        assert new.status_code == 200
        assert new.json()["must_change"] is False

    run(scenario())


def test_admin_portal_can_be_disabled():
    async def scenario():
        app = build(admin={"enabled": False})
        async with client_for(app) as client:
            response = await client.get("/admin/")
        assert response.status_code == 404

    run(scenario())


def test_mutating_admin_routes_reject_a_missing_csrf_token():
    async def scenario():
        app = build()
        async with client_for(app) as client:
            await client.post("/admin/login", json=DEFAULT_LOGIN)
            response = await client.patch("/admin/sources/primary", json={"enabled": False})
        assert response.status_code == 403

    run(scenario())


def test_admin_csrf_gate_does_not_block_the_wecom_callback():
    async def scenario():
        app = create_app(AppSettings())
        async with client_for(app) as client:
            response = await client.post("/wecom/callback", content=b"event")
        assert response.status_code == 503

    run(scenario())


def test_the_deployed_app_exposes_channel_health():
    """The roll-up is mounted by the application, and a search feeds it.

    The router is tested on its own elsewhere; what this covers is the wiring
    the tests around it cannot see -- that a deployment which really serves
    ``/admin`` answers for its channels, and that the search box is what fills
    the answer in.
    """

    async def scenario():
        runtime = Runtime(SourceRegistry([SourceEntry("primary", "1.0.0", StubSource())]))
        app = build(runtime=runtime)
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                payload = (await client.post("/admin/login", json=DEFAULT_LOGIN)).json()
                assert payload["must_change"] is True
                changed = await client.post(
                    "/admin/change-credentials",
                    json={"password": DEFAULT_LOGIN["password"], "username": CHANGED_LOGIN["username"],
                          "new_password": CHANGED_LOGIN["password"]},
                    headers={"x-csrf-token": payload["csrf_token"]})
                assert changed.status_code == 200
                search = await client.get("/admin/search", params={"q": "稻香"})
                health = await client.get("/admin/sources/health")
                return search.status_code, health.status_code, health.json()

    search, status, report = run(scenario())
    assert search == 200 and status == 200
    # The source the runtime exposed is the one the search queried, so it is the
    # one carrying a verdict -- and the roll-up reached a verdict rather than
    # reporting unknown only because the search ran through the real route.
    assert [item["id"] for item in report["sources"]] == ["primary"]
    assert report["sources"][0]["status"] == "ok"
    assert report["sources"][0]["configured"] is True


def test_admin_lists_the_sources_assembled_by_the_runtime():
    async def scenario():
        registry = SourceRegistry([SourceEntry("primary", "1", StubSource()),
                                   SourceEntry("backup", "1", StubSource(), priority=1)])
        app = build(runtime=Runtime(registry))
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                payload = (await client.post("/admin/login", json=DEFAULT_LOGIN)).json()
                blocked = await client.get("/admin/sources")
                changed = await client.post(
                    "/admin/change-credentials",
                    json={"password": DEFAULT_LOGIN["password"], "username": CHANGED_LOGIN["username"],
                          "new_password": CHANGED_LOGIN["password"]},
                    headers={"x-csrf-token": payload["csrf_token"]})
                response = await client.get("/admin/sources")
        assert blocked.status_code == 403
        assert changed.status_code == 200
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == ["primary", "backup"]

    run(scenario())
