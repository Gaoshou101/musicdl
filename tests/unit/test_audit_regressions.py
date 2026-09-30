"""Regression coverage for resource ownership and stable interaction state."""
import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from musicdl.admin.auth import AdminAuth, RateLimiter
from musicdl.admin.logs import LogBuffer
from musicdl.admin.management import SourceManager, BotManager
from musicdl.app import create_app, _Runtime, _admin_probes
from musicdl.config import AppSettings
from musicdl.sources import SourceRegistry, SourceEntry
from musicdl.sources.models import Candidate
from musicdl.sources.platform_search import PlatformSearch, PlatformSearchError
from musicdl.telegram.connector import TelegramConnector
from musicdl.telegram.models import TelegramStatus
from musicdl.wecom.results import selection_pages
from musicdl.wecom.state import SelectionContext
from musicdl.worker.selection import bind_user_selection, get_user_selection
from musicdl.worker.workers import JobWorker
from musicdl.worker.stream import _StreamWorker
from test_app_lifecycle import enabled_settings


def test_revocation_removes_bound_csrf_without_touching_other_sessions():
    auth = AdminAuth()
    first, second = auth.issue_session(), auth.issue_session()
    auth.bind_csrf(first, "first")
    auth.bind_csrf(second, "second")
    auth.revoke_session(first)
    auth.revoke_session(first)
    assert auth.session_user(first) is None
    assert auth.session_csrf(first) is None
    assert not auth.valid_csrf(first, "first")
    assert auth.valid_csrf(second, "second")


def test_limiter_expires_inactive_keys_without_forgetting_live_limits():
    now = [0.0]
    limiter = RateLimiter(limit=2, window_seconds=60, max_keys=2, clock=lambda: now[0])
    assert limiter.allow("old")
    now[0] = 30
    assert limiter.allow("live")
    assert limiter.allow("live")
    assert not limiter.allow("new")
    assert not limiter.allow("live")
    now[0] = 60
    assert limiter.allow("new")
    assert "old" not in limiter._attempts
    assert not limiter.allow("live")
    assert len(limiter._attempts) == 2


@pytest.mark.parametrize("manager_type", [SourceManager, BotManager])
def test_invalid_multi_field_update_leaves_all_values_unchanged(manager_type):
    changes = []
    manager = manager_type(on_change=lambda: changes.append(manager.snapshot()))
    manager.register({"id": "source", "username": "MusicBot"})
    before = manager.snapshot()
    with pytest.raises(ValueError):
        manager.update("source", enabled=False, timeout=-1)
    assert manager.snapshot() == before
    assert not changes
    with pytest.raises(ValueError):
        manager.update("source", priority=8, unexpected=True)
    assert manager.snapshot() == before
    assert not changes


@pytest.mark.parametrize("manager_type", [SourceManager, BotManager])
def test_registration_storage_failure_does_not_leave_a_phantom_entry(manager_type):
    def fail():
        raise OSError("storage unavailable")
    manager = manager_type(on_change=fail)
    with pytest.raises(OSError):
        manager.register({"id": "source", "username": "MusicBot"}, persist=True)
    assert manager.list() == []


@pytest.mark.parametrize("injected_boundary", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError("startup failed"), asyncio.CancelledError()])
def test_startup_failure_always_uninstalls_logging(injected_boundary, failure):
    async def scenario():
        async def broken(_):
            raise failure
        kwargs = {"state_factory" if injected_boundary else "runtime_factory": broken}
        app = create_app(enabled_settings(), **kwargs)
        root_handlers = list(logging.getLogger().handlers)
        propagation = {name: logging.getLogger(name).propagate for name in
                       ("uvicorn", "uvicorn.error", "uvicorn.access")}
        with pytest.raises(type(failure)):
            async with app.router.lifespan_context(app):
                pytest.fail("startup must fail")
        assert logging.getLogger().handlers == root_handlers
        assert not app.state.logs._attached
        for name, original in propagation.items():
            assert logging.getLogger(name).propagate == original
    asyncio.run(scenario())


def test_consumer_groups_do_not_clear_each_others_retry_budget():
    async def scenario():
        attempts = {}
        async def increment(key, field, value):
            attempts[key] = attempts.get(key, 0) + value
            return attempts[key]
        async def delete(key):
            attempts.pop(key, None)
        redis = SimpleNamespace(hincrby=increment, delete=delete, expire=AsyncMock(),
                                xack=AsyncMock())
        worker = _StreamWorker()
        worker._configure_delivery("{test}", 1000, 3)
        worker.redis, worker.retry_window_seconds = redis, 60
        await worker._record_failure("messages", "search", "1-0")
        await worker._record_failure("messages", "selection", "1-0")
        await worker._ack_then_clear("messages", "selection", "1-0")
        await worker._record_failure("messages", "search", "1-0")
        assert attempts == {worker._retry_key("messages", "search", "1-0"): 2}
        assert worker._retry_key("messages", "search", b"1-0") in attempts
    asyncio.run(scenario())


@pytest.mark.parametrize("new_count", [0, 2, 10])
def test_service_log_generation_resets_old_cursor_even_after_ids_catch_up(new_count):
    previous = LogBuffer()
    record = logging.LogRecord("musicdl", logging.INFO, __file__, 1, "entry", (), None)
    for _ in range(5):
        previous.append(record)
    cursor = previous.page()
    current = LogBuffer()
    for _ in range(new_count):
        current.append(record)
    page = current.page(after=cursor["last_id"], generation=cursor["generation"])
    assert page["reset"] is True
    assert len(page["items"]) == new_count
    assert page["last_id"] == new_count
    followup = current.page(after=page["last_id"], generation=page["generation"])
    assert followup["reset"] is False
    if new_count:
        assert followup["items"] == []


def test_telegram_concurrent_factory_and_connect_are_serialized(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        active = 0
        async def connect():
            nonlocal active
            active += 1
            assert active == 1
            await asyncio.sleep(0)
            active -= 1
        client = SimpleNamespace(connect=connect, disconnect=AsyncMock())
        async def factory(_):
            started.set()
            await release.wait()
            return client
        factory_mock = AsyncMock(side_effect=factory)
        connector = TelegramConnector(tmp_path, 1, "hash", factory_mock)
        first = asyncio.create_task(connector._client("profile"))
        await started.wait()
        others = [asyncio.create_task(connector._client("profile")) for _ in range(5)]
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, *others) == [client] * 6
        assert factory_mock.await_count == 1
        await connector.disconnect()
    asyncio.run(scenario())


def test_telegram_shutdown_waits_for_factory_and_rejects_new_clients(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
        async def factory(_):
            started.set()
            await release.wait()
            return client
        connector = TelegramConnector(tmp_path, 1, "hash", factory)
        opening = asyncio.create_task(connector._client("profile"))
        await started.wait()
        closing = asyncio.create_task(connector.disconnect())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.gather(opening, closing)
        assert client.disconnect.await_count == 1
        assert not connector._clients
        with pytest.raises(RuntimeError, match="closed"):
            await connector._client("profile")
    asyncio.run(scenario())


def test_telegram_shutdown_attempts_every_client_after_failure(tmp_path):
    async def scenario():
        bad = SimpleNamespace(disconnect=AsyncMock(side_effect=OSError("failed")))
        good = SimpleNamespace(disconnect=AsyncMock())
        connector = TelegramConnector(tmp_path, 1, "hash", lambda _: None)
        connector._clients.update(first=bad, second=good)
        with pytest.raises(ExceptionGroup):
            await connector.disconnect()
        assert good.disconnect.await_count == bad.disconnect.await_count == 1
        assert not connector._clients
        await connector.disconnect()
        assert good.disconnect.await_count == 1
    asyncio.run(scenario())


def test_telegram_pending_fallback_is_profile_scoped_and_newest_wins(tmp_path, monkeypatch):
    connector = TelegramConnector(tmp_path, 1, "hash", lambda _: None)
    connector._remember_login("alice", "+100", "old")
    def unavailable(*args, **kwargs):
        raise OSError("read only")
    monkeypatch.setattr("musicdl.telegram.connector.os.open", unavailable)
    connector._remember_login("alice", "+101", "new")
    assert connector.pending_login("alice")["phone_code_hash"] == "new"
    assert connector.pending_login("bob") == {}
    connector._remember_login("bob", "+200", "bob-code")
    connector._forget_login("alice")
    assert connector.pending_login("bob")["phone_code_hash"] == "bob-code"


def test_navigation_reuses_frozen_notice_and_page_size(monkeypatch):
    async def scenario():
        values = {}
        async def set_value(key, value, **kwargs):
            values[key] = value
        async def get_value(key):
            return values.get(key)
        redis = SimpleNamespace(set=set_value, get=get_value)
        rows = [Candidate(source_id="src", source_version="1", item_id=str(i),
                          title="song" * 70, artist="artist" * 20) for i in range(30)]
        context = SelectionContext("corp", "user", "request", "v1",
                                   dict(enumerate(rows, 1)), query="query")
        notice = "Refresh failed. Choose from the updated results."
        await bind_user_selection(redis, "token", context, notice=notice, page_size=7)
        route = await get_user_selection(redis, "corp", "user")
        assert route["notice"] == notice and route["page_size"] == 7
        expected = selection_pages("query", rows, max_items=7, notice=notice)
        assert len(expected) > 1
        move = AsyncMock(return_value=1)
        monkeypatch.setattr("musicdl.worker.workers.move_selection_page", move)
        wecom = SimpleNamespace(send_text=AsyncMock())
        worker = JobWorker(redis, wecom, {}, "/tmp", max_results=3)
        await worker._handle_interaction({"corp_id": "corp", "from_user": "user",
                                          "request_id": "next", "command": "next"})
        assert wecom.send_text.call_args.args == ("user", expected[1])
        assert move.call_args.kwargs["page_count"] == len(expected)
    asyncio.run(scenario())


def test_catalogue_timeout_does_not_free_a_still_running_broker_slot():
    async def scenario():
        started, release = threading.Event(), threading.Event()
        calls = []
        def fetch(*args, **kwargs):
            calls.append(1)
            started.set()
            assert release.wait(5), "test must release broker"
            raise OSError("upstream failed after cancellation")
        engine = PlatformSearch(broker=SimpleNamespace(fetch=fetch), timeout=0.05,
                                max_concurrency=1)
        try:
            first = asyncio.create_task(engine.hits("kw", "first"))
            while not started.is_set():
                await asyncio.sleep(0.001)
            with pytest.raises(TimeoutError):
                await first
            with pytest.raises(TimeoutError):
                await engine.hits("kw", "second")
            assert len(calls) == 1
            assert len(engine._broker_tasks) == 1
            await engine.aclose()
            with pytest.raises(PlatformSearchError, match="search_closed"):
                await engine.hits("kw", "third")
        finally:
            release.set()
            await asyncio.gather(*engine._broker_tasks, return_exceptions=True)
            await asyncio.sleep(0)
        assert not engine._broker_tasks
    asyncio.run(scenario())


def test_telegram_logout_waits_for_connection_initialization(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        client = SimpleNamespace(disconnect=AsyncMock())
        async def connect():
            started.set()
            await release.wait()
        client.connect = connect
        connector = TelegramConnector(tmp_path, 1, "hash", lambda _: client)
        opening = asyncio.create_task(connector._client("profile"))
        await started.wait()
        logout = asyncio.create_task(connector.logout("profile"))
        await asyncio.sleep(0)
        assert not logout.done()
        client.disconnect.assert_not_called()
        release.set()
        await asyncio.gather(opening, logout)
        assert client.disconnect.await_count == 1
        assert not connector._clients
        await connector.disconnect()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_reload", [False, True])
def test_runtime_reload_preserves_inflight_search_and_closes_after_release(cancel_reload):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        old_transport = SimpleNamespace(aclose=AsyncMock())
        async def old_search(query):
            started.set()
            await release.wait()
            old_transport.aclose.assert_not_called()
            return [Candidate(source_id="source", source_version="1", item_id="one",
                              title="Song", artist="Artist")]
        def runtime(source, transport=None):
            return _Runtime(redis=None, state=None, service=None, wecom=None, plugin_client=None,
                            transport=transport, registry=SourceRegistry([SourceEntry("source", "1", source)]),
                            message_worker=None, job_worker=None)
        old = runtime(old_search, old_transport)
        new = runtime(AsyncMock(return_value=[]))
        built = iter((old, new))
        app = create_app(AppSettings(), runtime_factory=lambda _: next(built))
        auth = app.state.admin.auth
        auth.change_credentials("admin", "operator", "new-password")
        session = auth.issue_session()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test",
                                         cookies={"admin_session": session}) as client:
                searching = asyncio.create_task(client.get("/admin/search", params={"q": "song"}))
                await asyncio.wait_for(started.wait(), 2)
                reloading = asyncio.create_task(app.state.reload_runtime())
                while app.state.runtime is old:
                    await asyncio.sleep(0)
                assert app.state.runtime is new
                assert app.state.runtime_generation == 1
                assert not reloading.done()
                old_transport.aclose.assert_not_called()
                assert (await client.get("/healthz")).status_code == 200
                if cancel_reload:
                    reloading.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await reloading
                release.set()
                response = await searching
                assert response.status_code == 200
                assert response.json()["count"] == 1
                if not cancel_reload:
                    assert (await reloading)["status"] == "reloaded"
        assert old_transport.aclose.await_count == 1
        assert old._users == 0 and old._closed
    asyncio.run(scenario())


def test_cancelled_request_releases_runtime_and_close_is_idempotent():
    async def scenario():
        transport = SimpleNamespace(aclose=AsyncMock())
        runtime = _Runtime(redis=None, state=None, service=None, wecom=None, plugin_client=None,
                           transport=transport, registry=None, message_worker=None, job_worker=None)
        started = asyncio.Event()
        async def request():
            async with runtime.borrow():
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(request())
        await started.wait()
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        await runtime.aclose()
        transport.aclose.assert_awaited_once()
        assert runtime._users == 0
        with pytest.raises(RuntimeError, match="closing"):
            async with runtime.borrow():
                pytest.fail("closed runtime must reject new borrowers")
    asyncio.run(scenario())


@pytest.mark.parametrize("dependency", ["redis", "plugin_runner", "telegram"])
def test_health_probe_keeps_its_dependency_alive_during_reload(dependency):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def check(*args):
            started.set()
            await release.wait()
            assert not runtime._closed
            return SimpleNamespace(status=TelegramStatus.READY) if dependency == "telegram" else True
        resource = SimpleNamespace(ping=check, service_health=check, restore=check,
                                   aclose=AsyncMock(), disconnect=AsyncMock())
        runtime = _Runtime(redis=resource, state=resource, service=None, wecom=None,
                           plugin_client=resource, transport=None, registry=None,
                           message_worker=None, job_worker=None, telegram=resource,
                           telegram_sources=1)
        settings = SimpleNamespace(telegram=SimpleNamespace(enabled=True, profile="default"))
        app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
        probing = asyncio.create_task(_admin_probes(settings, app)[dependency]())
        await asyncio.wait_for(started.wait(), 2)
        app.state.runtime = None
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        assert runtime._users == 1
        release.set()
        assert await probing is True
        await closing
        assert runtime._closed and runtime._users == 0
    asyncio.run(scenario())


def test_shutdown_waits_for_reload_assembly_and_closes_the_replacement():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        def runtime():
            return _Runtime(redis=None, state=None, service=None, wecom=None, plugin_client=None,
                            transport=None, registry=None, message_worker=None, job_worker=None)
        old, new = runtime(), runtime()
        calls = 0
        async def factory(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return old
            started.set()
            await release.wait()
            return new
        app = create_app(AppSettings(), runtime_factory=factory)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        reloading = asyncio.create_task(app.state.reload_runtime())
        await asyncio.wait_for(started.wait(), 2)
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(reloading, shutdown), 2)
        assert old._closed and new._closed
        assert app.state.runtime is new
        result = await app.state.reload_runtime()
        assert result == {"status": "skipped", "reason": "application is shutting down"}
        assert calls == 2
    asyncio.run(scenario())
