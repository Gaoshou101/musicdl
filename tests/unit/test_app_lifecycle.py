"""RED contract tests for application-owned worker lifecycle orchestration."""

import asyncio
import base64
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from musicdl.app import create_app
from musicdl.config import AppSettings, WeComSettings
from musicdl.sources import SourceEntry, SourceRegistry


AES_KEY = base64.b64encode(b"k" * 32).decode().rstrip("=")


def enabled_settings() -> AppSettings:
    return AppSettings(
        wecom=WeComSettings(
            enabled=True,
            corp_id="corp",
            agent_id=7,
            token=SecretStr("token"),
            secret=SecretStr("outbound-secret"),
            encoding_aes_key=SecretStr(AES_KEY),
            allowed_users=["user"],
        )
    )


class LiveWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def run_forever(self) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class ExitingWorker:
    def __init__(self, error: BaseException | None = None) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.error = error

    async def run_forever(self) -> None:
        self.started.set()
        if self.error is not None:
            raise self.error


class StubSource:
    """A source that answers with nothing; the wiring is what is under test."""

    async def search(self, query: str):
        return ()


@dataclass
class Runtime:
    state: object
    service: object
    message_worker: object
    job_worker: object

    def __post_init__(self) -> None:
        self.closed = False
        self.strict_close = True

    async def aclose(self) -> None:
        if self.strict_close:
            assert self.message_worker.cancelled.is_set()
            assert self.job_worker.cancelled.is_set()
        self.closed = True


def run(coro):
    return asyncio.run(coro)


def test_enabled_app_starts_both_workers_and_readyz_requires_live_tasks():
    async def scenario():
        state = AsyncMock()
        state.ping.return_value = True
        message = LiveWorker()
        jobs = LiveWorker()
        runtime = Runtime(state, object(), message, jobs)
        calls = []

        def factory(settings):
            calls.append(settings)
            return runtime

        app = create_app(enabled_settings(), runtime_factory=factory)
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(
                asyncio.gather(message.started.wait(), jobs.started.wait()), 1
            )
            assert app.state.runtime is runtime
            assert app.state.message_worker is message
            assert app.state.job_worker is jobs
            assert len(app.state.worker_tasks) == 2
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/readyz")
            assert response.status_code == 200
        assert runtime.closed
        assert len(calls) == 1
        assert calls[0].wecom.enabled is True

    run(scenario())


@pytest.mark.parametrize("error", [None, RuntimeError("worker failed")])
def test_readyz_is_503_when_a_worker_exits_normally_or_with_exception(error):
    async def scenario():
        state = AsyncMock()
        state.ping.return_value = True
        message = ExitingWorker(error)
        jobs = LiveWorker()
        runtime = Runtime(state, object(), message, jobs)
        runtime.strict_close = False

        app = create_app(
            enabled_settings(), runtime_factory=lambda _settings: runtime
        )
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(message.started.wait(), 1)
            await asyncio.sleep(0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/readyz")
            assert response.status_code == 503
            if error is not None:
                assert app.state.worker_error is error

    run(scenario())


def test_shutdown_cancels_both_workers_before_runtime_close():
    async def scenario():
        state = AsyncMock()
        state.ping.return_value = True
        message = LiveWorker()
        jobs = LiveWorker()
        runtime = Runtime(state, object(), message, jobs)
        app = create_app(
            enabled_settings(), runtime_factory=lambda _settings: runtime
        )
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(
                asyncio.gather(message.started.wait(), jobs.started.wait()), 1
            )
        assert message.cancelled.is_set()
        assert jobs.cancelled.is_set()
        assert runtime.closed

    run(scenario())


def test_disabled_wecom_starts_no_workers_but_still_assembles_the_source_runtime():
    """The panel's search box is not a WeCom feature.

    With WeCom off there are no workers to start and nothing to poll, but the
    registry still has to exist: it is what the administration portal searches
    and downloads through, and it needs no Redis to answer.
    """

    @dataclass
    class SearchRuntime:
        registry: object
        state: object = None
        service: object = None
        message_worker: object = None
        job_worker: object = None

        def __post_init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    async def scenario():
        calls = []
        runtime = SearchRuntime(SourceRegistry([SourceEntry("primary", "1", StubSource())]))

        def factory(settings):
            calls.append(settings)
            return runtime

        app = create_app(AppSettings(), runtime_factory=factory)
        async with app.router.lifespan_context(app):
            assert app.state.runtime is runtime
            assert not hasattr(app.state, "worker_tasks")
            assert app.state.runtime_error is None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/readyz")
        assert response.status_code == 200
        assert len(calls) == 1 and calls[0].wecom.enabled is False
        assert runtime.closed

    run(scenario())


def test_a_source_runtime_that_cannot_be_assembled_is_reported_not_faked():
    """The panel starts and says so; /readyz must not claim a working search."""

    async def scenario():
        def factory(_settings):
            raise OSError("app data missing")

        app = create_app(AppSettings(), runtime_factory=factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get("/readyz")

    assert run(scenario()).status_code == 503


def test_runtime_factory_failure_fails_closed_without_redis_fallback():
    async def scenario():
        failure = FileNotFoundError("app data missing")

        def factory(_settings):
            raise failure

        app = create_app(enabled_settings(), runtime_factory=factory)
        with pytest.raises(FileNotFoundError) as raised:
            async with app.router.lifespan_context(app):
                pytest.fail("runtime construction should fail before lifespan yields")
        assert raised.value is failure

    run(scenario())

def test_the_running_app_keeps_a_log_window_the_panel_can_read():
    """The service's own log records have to be readable while the app runs.

    The panel shows them from the same buffer the application installs, so the
    handler has to be attached for the life of the process and detached again
    when it stops -- a handler left behind would keep recording into a stopped
    application's window, and would leak into every later test in this process.
    """
    import logging

    async def scenario():
        app = create_app(AppSettings(), runtime_factory=lambda _settings: None)
        async with app.router.lifespan_context(app):
            window = app.state.logs
            assert window in logging.getLogger().handlers
            logging.getLogger("musicdl.worker").warning("hywmusic-beta answered 502")
            return window.page(level="warning")["items"], app

    items, app = run(scenario())
    assert [item["logger"] for item in items] == ["musicdl.worker"]
    assert app.state.logs not in logging.getLogger().handlers
