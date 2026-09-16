"""Administrator changes must survive a restart."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from musicdl.admin import AdminAuth, AdminStateError, SourceManager
from musicdl.app import create_app
from musicdl.config import AppSettings, WeComSettings
from musicdl.sources import SourceEntry, SourceRegistry

DEFAULT_LOGIN = {"username": "admin", "password": "password"}
CHANGED_LOGIN = {"username": "operator", "password": "correct-horse-battery"}
CHANGE_BODY = {"password": DEFAULT_LOGIN["password"], "username": CHANGED_LOGIN["username"],
               "new_password": CHANGED_LOGIN["password"]}
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


def persisted(tmp_path, name: str = "admin-state.json"):
    """Build settings that persist, bypassing the POSIX-only path validator.

    The validator exists for the container deployment; a Windows checkout can
    only exercise the store against a real local path.
    """
    settings = wecom_enabled()
    settings.admin.state_path = str(tmp_path / name)
    return settings


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def run(coro):
    return asyncio.run(coro)


def test_changed_credentials_survive_a_restart(tmp_path):
    async def scenario():
        first = create_app(persisted(tmp_path))
        async with client_for(first) as client:
            token = (await client.post("/admin/login", json=DEFAULT_LOGIN)).json()["csrf_token"]
            changed = await client.post("/admin/change-credentials", json=CHANGE_BODY,
                                        headers={"x-csrf-token": token})
            assert changed.status_code == 200
        second = create_app(persisted(tmp_path))
        async with client_for(second) as client:
            old = await client.post("/admin/login", json=DEFAULT_LOGIN)
            new = await client.post("/admin/login", json=CHANGED_LOGIN)
        assert old.status_code == 401
        assert new.status_code == 200
        assert new.json()["must_change"] is False

    run(scenario())


def test_source_edits_survive_a_restart_and_beat_the_runtime(tmp_path):
    async def scenario():
        registry = SourceRegistry([SourceEntry("primary", "1", StubSource())])

        def build():
            return create_app(persisted(tmp_path), runtime_factory=lambda _settings: Runtime(registry))

        first = build()
        async with first.router.lifespan_context(first):
            async with client_for(first) as client:
                token = (await client.post("/admin/login", json=DEFAULT_LOGIN)).json()["csrf_token"]
                await client.post("/admin/change-credentials", json=CHANGE_BODY,
                                  headers={"x-csrf-token": token})
                token = client.cookies.get("csrf_token")
                edited = await client.patch("/admin/sources/primary", json={"enabled": False, "priority": 5},
                                            headers={"x-csrf-token": token})
                assert edited.status_code == 200
        second = build()
        async with second.router.lifespan_context(second):
            async with client_for(second) as client:
                assert (await client.post("/admin/login", json=CHANGED_LOGIN)).status_code == 200
                listed = await client.get("/admin/sources")
        assert listed.json()["items"] == [{"id": "primary", "enabled": False, "priority": 5,
                                          "timeout": 10.0, "name": None, "plugin": None}]

    run(scenario())


def test_bot_edits_survive_a_restart(tmp_path):
    async def scenario():
        first = create_app(persisted(tmp_path))
        first.state.admin.bots.register(
            {"id": "custom", "username": "MusicBot", "command_template": "/get {query}"}, persist=True)
        first.state.admin.bots.update("custom", enabled=False, timeout=4.0)
        second = create_app(persisted(tmp_path))
        return second.state.admin.bots.list()

    assert run(scenario()) == [{"id": "custom", "enabled": False, "priority": 0, "timeout": 4.0,
                                "username": "MusicBot", "command_template": "/get {query}"}]


def test_runtime_sources_are_not_written_to_disk(tmp_path):
    """A published source is rebuilt from the runtime, so caching it would go stale."""
    async def scenario():
        registry = SourceRegistry([SourceEntry("primary", "1", StubSource())])
        app = create_app(persisted(tmp_path), runtime_factory=lambda _settings: Runtime(registry))
        async with app.router.lifespan_context(app):
            assert [item["id"] for item in app.state.admin.sources.list()] == ["primary"]
        return (tmp_path / "admin-state.json").exists()

    assert run(scenario()) is False


def test_unreadable_state_file_stops_startup(tmp_path):
    (tmp_path / "admin-state.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(AdminStateError):
        create_app(persisted(tmp_path))


def test_unsupported_state_version_stops_startup(tmp_path):
    (tmp_path / "admin-state.json").write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(AdminStateError):
        create_app(persisted(tmp_path))


def test_unset_state_path_keeps_the_previous_in_memory_behaviour():
    async def scenario():
        first = create_app(AppSettings())
        async with client_for(first) as client:
            token = (await client.post("/admin/login", json=DEFAULT_LOGIN)).json()["csrf_token"]
            assert (await client.post("/admin/change-credentials", json=CHANGE_BODY,
                                      headers={"x-csrf-token": token})).status_code == 200
        second = create_app(AppSettings())
        async with client_for(second) as client:
            return await client.post("/admin/login", json=DEFAULT_LOGIN)

    assert run(scenario()).status_code == 200


def test_failed_write_rolls_the_source_change_back(tmp_path):
    """A change that cannot be stored must not look applied in this process."""
    settings = persisted(tmp_path, name="blocked/admin-state.json")
    app = create_app(settings)
    app.state.admin.sources.register({"id": "primary"})
    blocked = tmp_path / "blocked"

    async def scenario():
        async with client_for(app) as client:
            token = (await client.post("/admin/login", json=DEFAULT_LOGIN)).json()["csrf_token"]
            assert (await client.post("/admin/change-credentials", json=CHANGE_BODY,
                                      headers={"x-csrf-token": token})).status_code == 200
            token = client.cookies.get("csrf_token")
            # The store worked a moment ago; make it unwritable and try again.
            (blocked / "admin-state.json").unlink()
            blocked.rmdir()
            blocked.write_text("not a directory", encoding="utf-8")
            return await client.patch("/admin/sources/primary", json={"enabled": False},
                                      headers={"x-csrf-token": token})

    with pytest.raises(OSError):
        run(scenario())
    assert app.state.admin.sources.list() == [{"id": "primary", "enabled": True, "priority": 0,
                                               "timeout": 10.0, "name": None}]


def test_restore_ignores_malformed_credentials_and_entries():
    auth = AdminAuth()
    auth.restore({"user_id": "operator", "password_hash": "not-a-hash", "must_change": False})
    assert auth.authenticate("admin", "password").ok
    auth.restore({"user_id": "", "password_hash": auth.snapshot()["password_hash"], "must_change": False})
    assert auth.authenticate("admin", "password").ok
    manager = SourceManager()
    manager.restore([{"id": "ok"}, {"id": "bad", "priority": "high"}, "not-a-dict"])
    assert [item["id"] for item in manager.list()] == ["ok"]
