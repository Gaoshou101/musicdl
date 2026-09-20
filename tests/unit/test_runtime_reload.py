"""Runtime changes are adopted in place, not at the next start.

Every setting the panel can write is now one the running process adopts: the
runtime it assembled is rebuilt from the settings in force, the workers that
belonged to it are replaced, and what that cost is reported back to the screen
that asked for it. These tests are about that boundary -- which writes reach
the runtime, what happens to the runtime that was there, and what an operator
reads when the rebuild cannot happen.
"""

from __future__ import annotations

import asyncio

import httpx

from musicdl.app import create_app
from musicdl.config import AppSettings
from musicdl.sources import SourceEntry, SourceRegistry

OPERATOR_LOGIN = {"username": "operator", "password": "correct-horse-battery"}


class IdleWorker:
    """A worker that runs until it is cancelled, like the real ones."""

    async def run_forever(self) -> None:
        await asyncio.Event().wait()


class StubSource:
    async def search(self, query: str):
        return ()


class Runtime:
    """Minimal stand-in for the runtime the application assembles."""

    def __init__(self) -> None:
        self.registry = SourceRegistry([SourceEntry("primary", "1", StubSource())])
        self.plugin_registry = self.registry
        self.message_worker = IdleWorker()
        self.job_worker = IdleWorker()
        self.state = None
        self.service = None
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class Factory:
    """A runtime factory that counts what it built, and can be told to fail."""

    def __init__(self) -> None:
        self.built: list[Runtime] = []
        self.failure: Exception | None = None

    def __call__(self, settings) -> Runtime:
        if self.failure is not None:
            raise self.failure
        runtime = Runtime()
        self.built.append(runtime)
        return runtime


def settings_for(tmp_path) -> AppSettings:
    settings = AppSettings()
    settings.admin.state_path = str(tmp_path / "admin-state.json")
    return settings


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def prepared_app(settings, factory):
    app = create_app(settings, runtime_factory=factory)
    app.state.admin.auth.change_credentials("admin", OPERATOR_LOGIN["username"],
                                            OPERATOR_LOGIN["password"])
    return app


async def login(client) -> str:
    response = await client.post("/admin/login", json=OPERATOR_LOGIN)
    assert response.status_code == 200
    return response.json()["csrf_token"]


def run(coro):
    return asyncio.run(coro)


def test_a_runtime_setting_is_adopted_without_a_restart(tmp_path):
    async def scenario():
        factory = Factory()
        app = prepared_app(settings_for(tmp_path), factory)
        async with app.router.lifespan_context(app):
            assert len(factory.built) == 1
            first = factory.built[0]
            async with client_for(app) as client:
                token = await login(client)
                response = await client.patch("/admin/config", headers={"x-csrf-token": token},
                                              json={"values": {"worker.max_attempts": 5}})
            return response, first, app

    response, first, app = run(scenario())
    assert response.status_code == 200
    assert response.json()["reload"]["status"] == "reloaded"
    assert response.json()["reload"]["generation"] == 1
    # The runtime in force is the new one, and the one it replaced let go of
    # everything it owned -- including its workers, which are now the new
    # runtime's.
    assert app.state.runtime is not first
    assert first.closed is True
    assert app.state.runtime.message_worker is not first.message_worker
    assert app.state.worker_error is None


def test_the_login_limiter_is_retuned_without_rebuilding_anything(tmp_path):
    """A change the process owns directly must not cost a worker restart."""

    async def scenario():
        factory = Factory()
        app = prepared_app(settings_for(tmp_path), factory)
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                token = await login(client)
                response = await client.patch("/admin/config", headers={"x-csrf-token": token},
                                              json={"values": {"admin.login_limit": 3}})
            return response, app

    response, app = run(scenario())
    assert response.status_code == 200
    assert "reload" not in response.json()
    assert app.state.admin.limiter.limit == 3


def test_writing_back_the_value_already_in_force_rebuilds_nothing(tmp_path):
    async def scenario():
        factory = Factory()
        app = prepared_app(settings_for(tmp_path), factory)
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                token = await login(client)
                first = await client.patch("/admin/config", headers={"x-csrf-token": token},
                                           json={"values": {"worker.max_attempts": 5}})
                again = await client.patch("/admin/config", headers={"x-csrf-token": token},
                                           json={"values": {"worker.max_attempts": 5}})
            return first, again

    first, again = run(scenario())
    assert first.json()["reload"]["status"] == "reloaded"
    assert "reload" not in again.json()


def test_a_rebuild_that_fails_leaves_the_running_runtime_alone(tmp_path):
    async def scenario():
        factory = Factory()
        app = prepared_app(settings_for(tmp_path), factory)
        async with app.router.lifespan_context(app):
            running = app.state.runtime
            factory.failure = RuntimeError("plugin storage is unavailable")
            async with client_for(app) as client:
                token = await login(client)
                response = await client.patch("/admin/config", headers={"x-csrf-token": token},
                                              json={"values": {"worker.max_attempts": 5}})
            # Read the flag before the lifespan closes the runtime it kept.
            return response, app, running, running.closed

    response, app, running, closed_by_shutdown = run(scenario())
    # The write is stored -- it is the change, not the report, that the operator
    # asked for -- and the failure is reported rather than raised.
    assert response.status_code == 200
    assert response.json()["reload"]["status"] == "failed"
    assert "plugin storage is unavailable" in response.json()["reload"]["error"]
    assert app.state.runtime is running
    # The failed rebuild touched nothing: the runtime it would have replaced was
    # never closed on its behalf.
    assert closed_by_shutdown is False


def test_a_bot_definition_reaches_the_runtime_that_is_already_running(tmp_path):
    async def scenario():
        factory = Factory()
        app = prepared_app(settings_for(tmp_path), factory)
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                token = await login(client)
                created = await client.post("/admin/bots", headers={"x-csrf-token": token},
                                            json={"id": "second", "username": "SecondBot"})
                listed = await client.get("/admin/sources")
            return created, listed, app

    created, listed, app = run(scenario())
    assert created.status_code == 200
    assert created.json()["reload"]["status"] == "reloaded"
    # The rebuilt runtime published what it assembled, so the operator's own
    # list now shows the source the runtime is actually serving.
    assert "primary" in {item["id"] for item in listed.json()["items"]}


def test_a_source_toggle_rebuilds_only_when_it_changes_something(tmp_path):
    async def scenario():
        factory = Factory()
        app = prepared_app(settings_for(tmp_path), factory)
        async with app.router.lifespan_context(app):
            async with client_for(app) as client:
                token = await login(client)
                # The runtime published "primary" while starting up, so the
                # definition exists and a no-op update has nothing to rebuild.
                unchanged = await client.patch("/admin/sources/primary",
                                               headers={"x-csrf-token": token},
                                               json={"priority": 0})
                changed = await client.patch("/admin/sources/primary",
                                             headers={"x-csrf-token": token},
                                             json={"priority": 4})
            return unchanged, changed, app

    unchanged, changed, app = run(scenario())
    assert unchanged.status_code == 200
    assert "reload" not in unchanged.json()
    assert changed.json()["reload"]["status"] == "reloaded"


def test_the_reload_boundary_is_installed_even_when_nothing_is_assembled(tmp_path):
    """A deployment with the door shut can still turn it on from the panel.

    The reload is what makes ``wecom.enabled`` a hot setting, so the callable
    has to exist before -- and regardless of -- the first successful assembly.
    """

    async def scenario():
        settings = settings_for(tmp_path)
        app = create_app(settings, runtime_factory=Factory())
        async with app.router.lifespan_context(app):
            report = await app.state.reload_runtime("test")
            return report, app

    report, app = run(scenario())
    assert report["status"] == "reloaded"
    assert app.state.runtime is not None


def test_an_injected_state_boundary_reports_that_it_cannot_reload(tmp_path):
    """A caller that owns the WeCom half is told, not silently skipped."""

    async def scenario():
        settings = settings_for(tmp_path)
        app = create_app(settings, state_factory=lambda _settings: object())
        async with app.router.lifespan_context(app):
            return await app.state.reload_runtime("test")

    report = run(scenario())
    assert report == {"status": "skipped", "reason": "injected state boundary"}
