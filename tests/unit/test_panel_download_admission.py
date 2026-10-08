"""Concurrency and lifecycle contracts for panel download coalescing."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.health import SourceHealthStore
from musicdl.admin.portal import (
    PanelDownloadCapacityError,
    PanelDownloadCoordinator,
    PanelRuntimeRetired,
    panel_download_key,
    create_admin_router,
)
from musicdl.media.admission import DownloadAdmission
from musicdl.media.history import DownloadHistoryStore
from musicdl.media.models import DownloadMetadata, MediaError
from musicdl.app import create_app
from musicdl.config import AppSettings
from musicdl.sources.models import Candidate
from musicdl.sources import SourceRegistry
from musicdl.sources.search import SearchResult, SourceStatus

ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"


def candidate(**changes):
    values = {
        "source_id": "source",
        "source_version": "v1",
        "item_id": "item",
        "title": "Song",
        "artist": "Artist",
        "format": "mp3",
    }
    values.update(changes)
    return Candidate(**values)


class BorrowedRuntime:
    def __init__(self):
        self.borrowed = 0
        self.released = 0

    @asynccontextmanager
    async def borrow(self):
        self.borrowed += 1
        try:
            yield self
        finally:
            self.released += 1


async def wait_for_waiters(coordinator, key, count):
    for _ in range(1000):
        flight = coordinator._flights.get(key)
        if flight is not None and flight.waiters == count:
            return flight
        await asyncio.sleep(0)
    raise AssertionError(f"panel flight did not reach {count} waiters")


class PanelSource:
    def __init__(self, *, hold=False):
        self.calls = 0
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        if not hold:
            self.finish.set()

    async def download(self, item, *, quality=None):
        self.calls += 1
        self.started.set()
        await self.finish.wait()

        async def chunks():
            yield ID3

        return DownloadMetadata(chunks(), extension="mp3", media_type="audio/mpeg",
                                declared_size=len(ID3))


def panel_client(tmp_path, source, admission):
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    runtime = SimpleNamespace(registry=object(), resolvers={"source": source})
    coordinator = PanelDownloadCoordinator(admission)
    history = DownloadHistoryStore(tmp_path / "history.sqlite")
    source_health = SourceHealthStore()
    app = FastAPI()
    app.include_router(create_admin_router(
        auth=auth, runtime=lambda: runtime, media_root=tmp_path / "media",
        history=history, source_health=source_health,
        download_admission=admission, panel_downloads=coordinator,
    ))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app, auth, runtime, coordinator, history, source_health


async def sign_in(client):
    response = await client.post("/admin/login", json={"username": "operator", "password": "new-password"})
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def _chunks(value):
    yield value


def _refresh_result(item):
    return SearchResult((item,), (SourceStatus(item.source_id, item.source_version, "ok", 1),),"v1")


def panel_body():
    return {"candidate": candidate().model_dump(mode="json"), "query": "Song Artist"}


def test_panel_download_key_coalesces_only_the_same_request_identity():
    runtime = object()
    item = candidate()
    key = panel_download_key("user", item, "flac", "Song Artist", runtime)

    assert panel_download_key("user", item, "flac", "Song Artist", runtime) == key
    assert panel_download_key("other", item, "flac", "Song Artist", runtime) != key
    assert panel_download_key("user", item, "mp3", "Song Artist", runtime) != key
    assert panel_download_key("user", item, "flac", "different query", runtime) != key
    assert panel_download_key("user", item.model_copy(update={"source_version": "v2"}),
                              "flac", "Song Artist", runtime) != key
    assert panel_download_key("user", item, "flac", "Song Artist", object()) != key


def test_panel_total_timeout_preserves_a_slow_declared_channel_budget_and_adds_queue_wait():
    from musicdl.admin import portal

    service = SimpleNamespace(resolvers={"slow": SimpleNamespace(stream_budget_seconds=150.0)})
    stream_phases = 2 * (portal.MAX_CHANNEL_SWITCHES + 1) + 1
    expected = max(portal.PANEL_DOWNLOAD_MIN_TIMEOUT_SECONDS,
                   150.0 * stream_phases + 2 * 11.0 + 13.0) + 17.0

    assert portal.panel_download_total_timeout(
        service, resolve_timeout=30.0, search_timeout=11.0, health_timeout=13.0,
        queue_wait_seconds=17.0,
    ) == expected
    assert expected > 120.0


def test_identical_requests_share_one_download_and_cancelled_waiter_keeps_other():
    async def run():
        coordinator = PanelDownloadCoordinator(DownloadAdmission())
        runtime = BorrowedRuntime()
        key = ("user", "source", "v1", "item", "flac", "query")
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = 0
        result = {"relative_path": "Artist/Song.mp3"}

        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()
            return result

        first = asyncio.create_task(coordinator.run(key, runtime, operation))
        await started.wait()
        second = asyncio.create_task(coordinator.run(key, runtime, operation))
        await wait_for_waiters(coordinator, key, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        assert calls == 1
        assert runtime.borrowed == 1 and runtime.released == 0
        flight = coordinator._flights[key]
        assert not flight.task.cancelled() and not flight.closing

        finish.set()
        assert await second is result
        assert calls == 1
        assert runtime.borrowed == runtime.released == 1
        assert key not in coordinator._flights
        await coordinator.close()

    asyncio.run(run())


def test_last_waiter_cancellation_drains_operation_and_runtime_borrow_through_repeated_cancel():
    async def run():
        coordinator = PanelDownloadCoordinator(DownloadAdmission())
        runtime = BorrowedRuntime()
        key = ("unique",)
        started = asyncio.Event()
        cancelling = asyncio.Event()
        finish_cleanup = asyncio.Event()
        finished = asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await finish_cleanup.wait()
                raise
            finally:
                finished.set()

        waiter = asyncio.create_task(coordinator.run(key, runtime, operation))
        await started.wait()
        await wait_for_waiters(coordinator, key, 1)
        waiter.cancel()
        await cancelling.wait()
        waiter.cancel()
        await asyncio.sleep(0)

        assert not waiter.done()
        assert not finished.is_set()
        assert runtime.borrowed == 1 and runtime.released == 0

        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert finished.is_set()
        assert runtime.borrowed == runtime.released == 1
        assert key not in coordinator._flights
        await coordinator.close()

    asyncio.run(run())


def test_runtime_retirement_drains_flight_and_release_allows_new_runtime_work():
    async def run():
        coordinator = PanelDownloadCoordinator(DownloadAdmission())
        runtime = BorrowedRuntime()
        key = ("old-runtime",)
        started = asyncio.Event()
        finished = asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        waiter = asyncio.create_task(coordinator.run(key, runtime, operation))
        await started.wait()
        await coordinator.cancel_runtime(runtime)
        with pytest.raises(PanelRuntimeRetired):
            await waiter
        assert finished.is_set()
        assert runtime.borrowed == runtime.released == 1
        assert key not in coordinator._flights

        coordinator.release_runtime(runtime)
        assert await coordinator.run(key, runtime, lambda: _immediate("new result")) == "new result"
        await coordinator.close()

    asyncio.run(run())


def test_app_runtime_reload_reuses_the_process_download_admission(tmp_path, monkeypatch):
    class Runtime:
        def __init__(self):
            self.registry = SourceRegistry([])
            self.plugin_registry = self.registry
            self.message_worker = None
            self.job_worker = None
            self.state = None
            self.service = None

        async def aclose(self):
            return None

    async def run():
        import musicdl.app as app_module

        settings = AppSettings()
        settings.admin.state_path = str(tmp_path / "admin-state.json")
        built = []
        app_ref = []

        def build(_settings, _clock=None, **kwargs):
            runtime = Runtime()
            runtime.download_admission = kwargs["download_admission"]
            assert runtime.download_admission is app_ref[0].state.download_admission
            built.append(runtime)
            return runtime

        monkeypatch.setattr(app_module, "_build_runtime", build)
        app = create_app(settings)
        app_ref.append(app)
        admission = app.state.download_admission
        async with app.router.lifespan_context(app):
            coordinator = app.state.panel_downloads
            first = app.state.runtime
            assert coordinator.admission is admission
            report = await app.state.reload_runtime("test-admission-reuse")
            assert report["status"] == "reloaded"
            assert app.state.runtime is not first
            assert len(built) == 2
            assert app.state.panel_downloads is coordinator
            assert coordinator.admission is admission

    asyncio.run(run())


async def _immediate(value):
    return value


def test_unique_panel_flights_are_bounded_by_shared_admission_budget():
    async def run():
        admission = DownloadAdmission()
        coordinator = PanelDownloadCoordinator(admission)
        runtime = BorrowedRuntime()
        finish = asyncio.Event()

        async def operation():
            await finish.wait()
            return "done"

        waiters = [asyncio.create_task(coordinator.run(("flight", index), runtime, operation))
                   for index in range(admission.active_limit + admission.pending_limit)]
        for _ in range(1000):
            if len(coordinator._flights) == 20:
                break
            await asyncio.sleep(0)
        assert len(coordinator._flights) == 20

        with pytest.raises(PanelDownloadCapacityError) as caught:
            await coordinator.run(("overflow",), runtime, operation)
        assert caught.value.code == "download_queue_full"
        assert caught.value.snapshot["panel_flights"] == 20
        assert caught.value.snapshot["panel_flight_limit"] == 20

        finish.set()
        assert await asyncio.gather(*waiters) == ["done"] * 20
        assert coordinator._flights == {}
        assert runtime.borrowed == runtime.released == 20
        await coordinator.close()

    asyncio.run(run())


def test_panel_route_returns_one_shared_result_and_history_row_for_identical_requests(tmp_path):
    async def run():
        source = PanelSource(hold=True)
        admission = DownloadAdmission()
        app, auth, runtime, coordinator, history, source_health = panel_client(
            tmp_path, source, admission,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://test") as client:
            token = await sign_in(client)
            headers = {"x-csrf-token": token}
            first = asyncio.create_task(client.post("/admin/download", json=panel_body(), headers=headers))
            await source.started.wait()
            second = asyncio.create_task(client.post("/admin/download", json=panel_body(), headers=headers))
            user_id = auth.session_user(client.cookies.get("admin_session"))
            key = panel_download_key(user_id, candidate(), "flac", "Song Artist", runtime)
            await wait_for_waiters(coordinator, key, 2)
            source.finish.set()
            first_response, second_response = await asyncio.gather(first, second)

        assert first_response.status_code == second_response.status_code == 200
        first_payload, second_payload = first_response.json(), second_response.json()
        assert first_payload == second_payload
        assert first_payload["request_id"]
        assert source.calls == 1
        assert history.list()["total"] == 1
        row = source_health.snapshot([{"id": "source", "name": "Source"}])["sources"][0]
        assert row["downloads"] == 1 and row["attempts"] == 1

    asyncio.run(run())


def test_panel_queue_rejection_has_no_download_history_or_source_metric(tmp_path):
    async def run():
        source = PanelSource()
        admission = DownloadAdmission(active_limit=1, pending_limit=0,
                                      per_source_limit=1, per_user_limit=1)
        app, _, _, _, history, source_health = panel_client(tmp_path, source, admission)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://test") as client:
            token = await sign_in(client)
            async with admission.acquire(source_id="source", user_id="admin"):
                response = await client.post("/admin/download", json=panel_body(),
                                             headers={"x-csrf-token": token})

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["code"] == "download_queue_full"
        assert detail["admission"]["active"] == 1
        assert detail["admission"]["pending"] == 0
        assert "admin" not in repr(detail["admission"])
        assert source.calls == 0
        assert history.list()["total"] == 0
        row = source_health.snapshot([{"id": "source", "name": "Source"}])["sources"][0]
        assert row["downloads"] == 0 and row["attempts"] == 0
        assert row["recent_requests"] == []

    asyncio.run(run())


def test_panel_total_deadline_includes_admission_wait_and_is_shared_by_duplicate_waiters(tmp_path, monkeypatch):
    from musicdl.admin import portal

    async def run():
        source = PanelSource()
        admission = DownloadAdmission(active_limit=1, pending_limit=1,
                                      per_source_limit=1, per_user_limit=1)
        app, auth, runtime, coordinator, history, source_health = panel_client(tmp_path, source, admission)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://test") as client:
            token = await sign_in(client)
            async with admission.acquire(source_id="source", user_id="admin"):
                headers = {"x-csrf-token": token}
                first = asyncio.create_task(client.post("/admin/download", json=panel_body(), headers=headers))
                for _ in range(1000):
                    if admission.snapshot()["pending"] == 1:
                        break
                    await asyncio.sleep(0)
                assert admission.snapshot()["pending"] == 1
                user_id = auth.session_user(client.cookies.get("admin_session"))
                key = panel_download_key(user_id, candidate(), "flac", "Song Artist", runtime)
                second = asyncio.create_task(client.post("/admin/download", json=panel_body(), headers=headers))
                await wait_for_waiters(coordinator, key, 2)
                first_response, second_response = await asyncio.wait_for(
                    asyncio.gather(first, second), timeout=1.0,
                )
                assert admission.snapshot()["active"] == 1
                assert admission.snapshot()["pending"] == 0

        assert first_response.status_code == second_response.status_code == 504
        assert first_response.json()["detail"] == second_response.json()["detail"] == "media_timeout"
        assert admission.snapshot()["active"] == 0
        assert admission.snapshot()["pending"] == 0
        assert source.calls == 0
        assert history.list()["total"] == 0
        row = source_health.snapshot([{"id": "source", "name": "Source"}])["sources"][0]
        assert row["downloads"] == 0 and row["attempts"] == 0

    deadline_calls = []

    def short_deadline(service, **kwargs):
        deadline_calls.append((service, kwargs))
        return 0.02

    monkeypatch.setattr(portal, "PANEL_DOWNLOAD_QUEUE_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(portal, "panel_download_total_timeout", short_deadline)
    asyncio.run(run())
    assert len(deadline_calls) == 1
    assert deadline_calls[0][1]["queue_wait_seconds"] == 0.5


def test_panel_fallback_switch_obeys_source_limit_without_double_counting(tmp_path):
    async def run():
        backup_started = asyncio.Event()
        backup_finish = asyncio.Event()
        calls = {"primary": 0, "backup": 0}

        class Primary(PanelSource):
            async def download(self, item, *, quality=None):
                calls["primary"] += 1
                raise MediaError("incomplete_audio")

        class Backup(PanelSource):
            async def download(self, item, *, quality=None):
                calls["backup"] += 1
                backup_started.set()
                await backup_finish.wait()
                return DownloadMetadata(_chunks(ID3), extension="mp3", media_type="audio/mpeg",
                                        quality="flac")

        primary_item = candidate(qualities=("320k",))
        backup_item = candidate(source_id="backup", source_version="v1", item_id="backup-item",
                                qualities=("flac",))
        admission = DownloadAdmission(active_limit=3, pending_limit=2,
                                      per_source_limit=1, per_user_limit=3)
        app, auth, runtime, coordinator, history, source_health = panel_client(
            tmp_path, Primary(), admission,
        )
        runtime.resolvers = {"source": Primary(), "backup": Backup()}
        runtime.quality_policy = "best_available"

        async def refresh(query, excluded):
            return _refresh_result(backup_item)

        runtime.refresh = refresh
        backup_blocker = await admission.acquire(source_id="backup", user_id="other").__aenter__()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://test") as client:
            token = await sign_in(client)
            headers = {"x-csrf-token": token}
            request = asyncio.create_task(client.post("/admin/download",
                json={"candidate": primary_item.model_dump(mode="json"), "query": "Song Artist"},
                headers=headers))
            for _ in range(1000):
                if admission.snapshot()["source_pending"] == 1:
                    break
                await asyncio.sleep(0)
            assert admission.snapshot()["active"] == 2
            assert admission.snapshot()["source_active"] == 1
            assert admission.snapshot()["source_pending"] == 1
            assert calls == {"primary": 1, "backup": 0}
            await backup_blocker.release()
            await backup_started.wait()
            assert admission.snapshot()["active"] == 1
            assert admission.snapshot()["source_active"] == 1
            assert admission.snapshot()["source_pending"] == 0
            backup_finish.set()
            response = await request

        assert response.status_code == 200
        assert calls == {"primary": 1, "backup": 1}
        assert admission.snapshot()["active"] == 0
        assert admission.snapshot()["source_active"] == 0

    asyncio.run(run())
