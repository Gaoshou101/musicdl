"""Bot definitions are owned by the administration portal."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from musicdl.admin.auth import AdminAuth
from musicdl.admin.management import BotManager
from musicdl.admin.portal import create_admin_router
from musicdl.app import create_app
from musicdl.config import AppSettings, WeComSettings


def build_app(manager: BotManager | None = None) -> FastAPI:
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, bots=manager if manager is not None else BotManager()))
    return app


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def login(client: httpx.AsyncClient) -> str:
    response = await client.post("/admin/login", json={"username": "operator", "password": "new-password"})
    assert response.status_code == 200
    return client.cookies.get("csrf_token")


def test_an_operator_defines_a_public_and_a_custom_bot():
    app = build_app()

    async def scenario():
        async with client_for(app) as client:
            token = await login(client)
            public = await client.post("/admin/bots", headers={"x-csrf-token": token},
                                       json={"id": "public", "username": "MusicBot"})
            custom = await client.post("/admin/bots", headers={"x-csrf-token": token},
                                       json={"id": "custom", "username": "MyBot",
                                             "command_template": "/get {query}", "priority": 1})
            listed = await client.get("/admin/bots")
            return public, custom, listed

    public, custom, listed = asyncio.run(scenario())

    # A definition without a template keeps the built-in public contract.
    assert public.status_code == 200
    assert public.json() == {"id": "public", "enabled": True, "priority": 0, "timeout": 10.0,
                             "username": "MusicBot", "command_template": None}
    assert custom.json()["command_template"] == "/get {query}" and custom.json()["priority"] == 1
    assert {item["id"] for item in listed.json()["items"]} == {"public", "custom"}


def test_invalid_definitions_are_refused_and_nothing_is_stored():
    manager = BotManager()
    app = build_app(manager)

    async def scenario():
        async with client_for(app) as client:
            token = await login(client)
            cases = [
                {"id": "no-username"},
                {"id": "null-username", "username": None},
                {"id": "at-prefix", "username": "@MusicBot"},
                {"id": "too-short", "username": "Bot"},
                {"id": "plain-template", "username": "MusicBot", "command_template": "search it"},
                {"id": "double-placeholder", "username": "MusicBot", "command_template": "{query} {query}"},
                {"id": "over-cap-timeout", "username": "MusicBot", "timeout": 121},
                {"id": "coerced-enabled", "username": "MusicBot", "enabled": "yes"},
                {"id": "coerced-priority", "username": "MusicBot", "priority": "1"},
            ]
            return [await client.post("/admin/bots", headers={"x-csrf-token": token}, json=case)
                    for case in cases]

    responses = asyncio.run(scenario())

    assert [response.status_code for response in responses] == [422] * 9
    assert manager.list() == []


def test_updating_and_removing_a_definition_is_bounded():
    manager = BotManager([{"id": "custom", "username": "MyBot", "command_template": "/get {query}"}])
    app = build_app(manager)

    async def scenario():
        async with client_for(app) as client:
            token = await login(client)
            back_to_public = await client.patch("/admin/bots/custom", headers={"x-csrf-token": token},
                                                json={"command_template": ""})
            renamed = await client.patch("/admin/bots/custom", headers={"x-csrf-token": token},
                                         json={"username": "OtherBot", "timeout": 3})
            unknown_patch = await client.patch("/admin/bots/absent", headers={"x-csrf-token": token},
                                               json={"enabled": False})
            removed = await client.delete("/admin/bots/custom", headers={"x-csrf-token": token})
            gone = await client.delete("/admin/bots/custom", headers={"x-csrf-token": token})
            return back_to_public, renamed, unknown_patch, removed, gone

    back_to_public, renamed, unknown_patch, removed, gone = asyncio.run(scenario())

    # An empty template is how an operator switches a custom bot back to public.
    assert back_to_public.status_code == 200 and back_to_public.json()["command_template"] is None
    assert renamed.json()["username"] == "OtherBot" and renamed.json()["timeout"] == 3.0
    assert unknown_patch.status_code == 404 and gone.status_code == 404
    assert removed.status_code == 200 and removed.json()["id"] == "custom"
    assert manager.list() == []


def test_a_duplicate_id_stays_a_hard_error_through_the_portal():
    manager = BotManager([{"id": "taken", "username": "MusicBot"}])
    app = build_app(manager)

    async def scenario():
        async with client_for(app) as client:
            token = await login(client)
            return await client.post("/admin/bots", headers={"x-csrf-token": token},
                                     json={"id": "taken", "username": "OtherBot"})

    response = asyncio.run(scenario())

    assert response.status_code == 422
    assert [item["username"] for item in manager.list()] == ["MusicBot"]


def test_a_portal_created_definition_reaches_the_persistence_hook():
    saved: list[list[dict]] = []
    manager = BotManager(on_change=lambda: saved.append(manager.snapshot()))
    app = build_app(manager)

    async def scenario():
        async with client_for(app) as client:
            token = await login(client)
            return await client.post("/admin/bots", headers={"x-csrf-token": token},
                                     json={"id": "custom", "username": "MyBot",
                                           "command_template": "/get {query}"})

    assert asyncio.run(scenario()).status_code == 200
    assert saved and saved[-1] == [{"id": "custom", "enabled": True, "priority": 0, "timeout": 10.0,
                                    "username": "MyBot", "command_template": "/get {query}"}]


def test_a_failed_persist_rolls_the_change_back():
    def explode():
        raise OSError("disk full")

    manager = BotManager([{"id": "keep", "username": "MusicBot"}], on_change=explode)

    with pytest.raises(OSError):
        manager.remove("keep")
    assert [item["id"] for item in manager.list()] == ["keep"]

    with pytest.raises(OSError):
        manager.update("keep", username="OtherBot")
    assert manager.list()[0]["username"] == "MusicBot"


def _wecom_settings(tmp_path) -> AppSettings:
    """The application only assembles a runtime when WeCom is enabled."""
    settings = AppSettings(wecom=WeComSettings(
        enabled=True, corp_id="corp", agent_id=7, token=SecretStr("token"),
        secret=SecretStr("secret"),
        encoding_aes_key=SecretStr(base64.b64encode(b"k" * 32).decode().rstrip("=")),
        allowed_users=["user"]))
    # Assigned after construction to bypass the POSIX-only path validator.
    settings.admin.state_path = str(tmp_path / "admin-state.json")
    return settings


async def _enter(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        return None


def test_the_runtime_receives_the_definitions_the_portal_owns(tmp_path, monkeypatch):
    """Startup is the link: the portal owns definitions, the runtime consumes them."""
    import musicdl.app as app_module

    captured: dict = {}

    def spy(settings, clock=None, **options):
        captured.update(options)
        raise RuntimeError("stop after capture")

    app = create_app(_wecom_settings(tmp_path))
    app.state.admin.bots.register(
        {"id": "custom", "username": "MyBot", "command_template": "/get {query}"}, persist=True)
    monkeypatch.setattr(app_module, "_build_runtime", spy)

    with pytest.raises(RuntimeError, match="stop after capture"):
        asyncio.run(_enter(app))

    # The built-in bot ships with every deployment; the operator's own entry
    # rides alongside it, ordered by priority and then id.
    assert captured["bots"] == ({"id": "custom", "enabled": True, "priority": 0, "timeout": 10.0,
                                 "username": "MyBot", "command_template": "/get {query}"},
                                {"id": "music_v1bot", "enabled": True, "priority": 0, "timeout": 10.0,
                                 "username": "music_v1bot", "command_template": None})
