"""Focused tests for non-destructive administrator source diagnostics."""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import FastAPI

import pytest

from musicdl.admin.diagnostics import (
    DIAGNOSTIC_PENDING_LIMIT, DIAGNOSTIC_WORKERS,
    DiagnosticResultStore, SourceDiagnosticManager,
    classify_exception, normalize_query,
)
from musicdl.admin.auth import AdminAuth, RateLimiter
from musicdl.admin.portal import create_admin_router
from musicdl.media.admission import DownloadAdmission
from musicdl.sources.models import Candidate
from musicdl.sources.registry import SourceEntry, SourceRegistry


def candidate(source_id="demo", version="1", item_id="track-1"):
    return Candidate(source_id=source_id, source_version=version, item_id=item_id,
                     title="晴天", artist="周杰伦")


class FakeSource:
    def __init__(self, *, answers=None, failure=None):
        self.answers = list(answers or ())
        self.failure = failure
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        if self.failure is not None:
            raise self.failure
        return self.answers


class FakeClient:
    def __init__(self, media=None, *, failure=None):
        self.media = media
        self.failure = failure
        self.calls = []

    async def resolve(self, stored, item, *, timeout_ms, quality):
        self.calls.append((item, timeout_ms, quality))
        if self.failure is not None:
            raise self.failure
        return self.media


class FakeResolver:
    def __init__(self, client, fingerprint="a" * 64):
        self.client = client
        self.stored = type("Stored", (), {
            "manifest": type("Manifest", (), {"sha256": fingerprint})(),
        })()


class FakeRuntime:
    def __init__(self, source, resolver=None, *, version="1"):
        self.plugin_registry = SourceRegistry([SourceEntry("demo", version, source)])
        self.registry = self.plugin_registry
        self.resolvers = {"demo": resolver} if resolver is not None else {}
        self.borrowed = 0
        self.borrow_released = 0

    class _Borrow:
        def __init__(self, runtime):
            self.runtime = runtime

        async def __aenter__(self):
            self.runtime.borrowed += 1

        async def __aexit__(self, *_):
            self.runtime.borrow_released += 1

    def borrow(self):
        return self._Borrow(self)


def media(extension="flac", quality="flac"):
    return type("Media", (), {"extension": extension, "quality": quality})()


async def run_until(predicate, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("diagnostic worker did not finish")
        await asyncio.sleep(0.001)


def test_query_normalization_and_exception_classification_are_bounded():
    assert normalize_query(None) == "周杰伦 晴天"
    assert normalize_query("  周杰伦\n晴天  ") == "周杰伦 晴天"
    with pytest.raises(ValueError):
        normalize_query("x" * 501)
    assert classify_exception(RuntimeError("unauthorized"), stage="resolve") == "auth_required"
    rate_limited = RuntimeError("upstream rejected request")
    rate_limited.status_code = 429
    forbidden = RuntimeError("upstream rejected request")
    forbidden.status_code = 403
    assert classify_exception(rate_limited, stage="search") == "rate_limited"
    assert classify_exception(forbidden, stage="resolve") == "auth_required"
    assert classify_exception(RuntimeError("token=secret private details"), stage="search") == "search_failed"
    assert classify_exception(RuntimeError("not valid password=private"), stage="resolve") == "resolve_failed"


def test_non_destructive_search_flac_resolve_fallback_and_version_staleness(tmp_path):
    async def scenario():
        source = FakeSource(answers=[candidate()])
        client = FakeClient(media("mp3", "320k"))
        resolver = FakeResolver(client)
        runtime = FakeRuntime(source, resolver)
        store = DiagnosticResultStore(tmp_path / "diagnostics.json")
        manager = SourceDiagnosticManager(store, DownloadAdmission(), deadline_seconds=1)
        submitted = await manager.submit(runtime, "demo", user_id="operator", query="晴天")
        await run_until(lambda: manager.job(submitted["job_id"])["status"] == "complete")
        job = manager.job(submitted["job_id"])
        assert job["result"]["status"] == "unsupported_flac"
        assert len(client.calls) == 1 and client.calls[0][2] == "flac"
        assert source.queries == ["晴天"]
        assert job["result"]["download_verified"] is False
        assert runtime.borrowed == runtime.borrow_released == 1
        assert store.latest("demo", "1", "a" * 64)["actual_query"] == "晴天"
        version_two = FakeRuntime(source, resolver, version="2")
        assert store.snapshot({"demo": ("2", "a" * 64)})[0]["stale"] is True
        persisted = (tmp_path / "diagnostics.json").read_text(encoding="utf-8")
        assert "token_hash" not in persisted and "password" not in persisted
        await manager.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("extension", "quality"), [("flac", "320k"), ("mp3", "flac")])
def test_diagnostic_resolve_uses_contradictory_metadata_verdict(extension, quality, tmp_path):
    async def scenario():
        runtime = FakeRuntime(FakeSource(answers=[candidate()]),
                              FakeResolver(FakeClient(media(extension, quality))))
        manager = SourceDiagnosticManager(
            DiagnosticResultStore(tmp_path / f"{extension}-{quality}.json"),
            DownloadAdmission(), deadline_seconds=1,
        )
        submitted = await manager.submit(runtime, "demo", user_id="operator")
        await run_until(lambda: manager.job(submitted["job_id"])["status"] == "complete")
        result = manager.job(submitted["job_id"])["result"]
        assert result["status"] == "unsupported_flac"
        assert result["extension"] == extension and result["quality"] == quality
        await manager.close()

    asyncio.run(scenario())


def test_search_fallback_batch_counts_cooldown_dedup_and_queue_full(tmp_path):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowSource(FakeSource):
            async def search(self, query):
                self.queries.append(query)
                if query == "first":
                    started.set()
                    await release.wait()
                return ()

        source = SlowSource()
        runtime = FakeRuntime(source)
        store = DiagnosticResultStore(tmp_path / "diagnostics.json")
        manager = SourceDiagnosticManager(store, DownloadAdmission(), deadline_seconds=2)
        first = await manager.submit(runtime, "demo", user_id="operator", query="first")
        await started.wait()
        duplicate = await manager.submit(runtime, "demo", user_id="operator", query="different")
        assert duplicate["mode"] == "deduplicated" and duplicate["job_id"] == first["job_id"]
        release.set()
        await run_until(lambda: manager.job(first["job_id"])["status"] == "complete")
        result = manager.job(first["job_id"])["result"]
        assert result["status"] == "empty_search"
        assert result["queries_tried"] == ["first", "晴天 周杰伦", "稻香 周杰伦"]
        cooldown = await manager.submit(runtime, "demo", user_id="operator", query="other")
        assert cooldown["mode"] == "cooldown"
        assert cooldown["actual_query"] == "稻香 周杰伦"
        batch = await manager.submit_many(runtime, runtime.plugin_registry.enabled(),
                                         user_id="operator", query="晴天")
        assert batch["requested"] == 1 and batch["cooldown"] == 1
        await manager.close()

    asyncio.run(scenario())


def test_cancellation_and_busy_admission_release_runtime_borrow(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingSource(FakeSource):
            async def search(self, query):
                entered.set()
                await release.wait()
                return [candidate()]

        runtime = FakeRuntime(BlockingSource())
        manager = SourceDiagnosticManager(DiagnosticResultStore(tmp_path / "diagnostics.json"),
                                          DownloadAdmission(), deadline_seconds=2)
        submitted = await manager.submit(runtime, "demo", user_id="operator")
        await entered.wait()
        cancelled = await manager.cancel(submitted["job_id"])
        assert cancelled["cancelled"] is True
        assert runtime.borrowed == runtime.borrow_released == 1
        await manager.close()

        admission = DownloadAdmission(active_limit=1, pending_limit=0,
                                      per_source_limit=1, per_user_limit=1)
        second_runtime = FakeRuntime(FakeSource(answers=[candidate()]))
        busy = SourceDiagnosticManager(DiagnosticResultStore(tmp_path / "busy.json"), admission)
        try:
            async with admission.acquire(source_id="demo", user_id="other", timeout=None):
                job = await busy.submit(second_runtime, "demo", user_id="operator")
                await run_until(lambda: busy.job(job["job_id"])["status"] == "complete")
                assert busy.job(job["job_id"])["result"]["status"] == "busy"
                assert second_runtime.borrowed == second_runtime.borrow_released == 1
        finally:
            await busy.close()

    asyncio.run(scenario())


def test_cancelling_a_queued_diagnostic_returns_pending_capacity(tmp_path):
    async def scenario():
        release = asyncio.Event()

        class BlockingSource(FakeSource):
            async def search(self, query):
                self.queries.append(query)
                await release.wait()
                return ()

        source = BlockingSource()
        entries = [SourceEntry(f"source-{index:02d}", "1", source)
                   for index in range(DIAGNOSTIC_WORKERS + DIAGNOSTIC_PENDING_LIMIT + 1)]
        runtime = FakeRuntime(source)
        runtime.registry = runtime.plugin_registry = SourceRegistry(entries)
        manager = SourceDiagnosticManager(
            DiagnosticResultStore(tmp_path / "queued-cancellation.json"),
            DownloadAdmission(), deadline_seconds=2,
        )
        jobs = []
        try:
            for entry in entries[:DIAGNOSTIC_WORKERS]:
                jobs.append(await manager.submit(runtime, entry.source_id, user_id="operator"))
            await run_until(lambda: sum(manager.job(item["job_id"])["status"] == "running"
                                         for item in jobs) == DIAGNOSTIC_WORKERS)
            for entry in entries[DIAGNOSTIC_WORKERS:DIAGNOSTIC_WORKERS + DIAGNOSTIC_PENDING_LIMIT]:
                scheduled = await manager.submit(runtime, entry.source_id, user_id="operator")
                assert scheduled["mode"] == "scheduled"
                jobs.append(scheduled)
            assert manager.snapshot(runtime)["pending"] == DIAGNOSTIC_PENDING_LIMIT

            overflow = await manager.submit(runtime, entries[-1].source_id, user_id="operator")
            assert overflow["status"] == "queue_full"
            cancelled = await manager.cancel(jobs[DIAGNOSTIC_WORKERS]["job_id"])
            assert cancelled == {"cancelled": True, "status": "cancelled"}
            assert manager.snapshot(runtime)["pending"] == DIAGNOSTIC_PENDING_LIMIT - 1
            admitted = await manager.submit(runtime, entries[-1].source_id, user_id="operator")
            assert admitted["mode"] == "scheduled"
            assert manager.snapshot(runtime)["pending"] == DIAGNOSTIC_PENDING_LIMIT
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_cancel_wait_drains_diagnostic_cleanup_when_its_caller_is_cancelled(tmp_path):
    async def scenario():
        entered, cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class SlowCleanupSource(FakeSource):
            async def search(self, query):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    await finish_cleanup.wait()
                    raise

        runtime = FakeRuntime(SlowCleanupSource())
        manager = SourceDiagnosticManager(
            DiagnosticResultStore(tmp_path / "cancel-drain.json"),
            DownloadAdmission(), deadline_seconds=2,
        )
        submitted = await manager.submit(runtime, "demo", user_id="operator")
        await entered.wait()
        cancel_wait = asyncio.create_task(manager.cancel(submitted["job_id"]))
        await cleanup_started.wait()
        cancel_wait.cancel()
        await asyncio.sleep(0)
        assert not cancel_wait.done()
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await cancel_wait
        assert runtime.borrowed == runtime.borrow_released == 1
        await manager.close()

    asyncio.run(scenario())


def test_routes_require_auth_csrf_and_job_owner_and_cancel_batch(tmp_path):
    async def scenario():
        primary_entered, primary_release = asyncio.Event(), asyncio.Event()
        foreign_entered, foreign_release = asyncio.Event(), asyncio.Event()

        class BlockingSource(FakeSource):
            def __init__(self, source_id, entered, release):
                super().__init__()
                self.source_id = source_id
                self.entered = entered
                self.release = release

            async def search(self, query):
                self.queries.append(query)
                self.entered.set()
                await self.release.wait()
                return [candidate(self.source_id)]

        source = BlockingSource("demo", primary_entered, primary_release)
        client = FakeClient(media())
        runtime = FakeRuntime(source, FakeResolver(client))
        foreign_source = BlockingSource("foreign", foreign_entered, foreign_release)
        runtime.registry = runtime.plugin_registry = SourceRegistry([
            SourceEntry("demo", "1", source), SourceEntry("foreign", "1", foreign_source),
        ])
        manager = SourceDiagnosticManager(
            DiagnosticResultStore(tmp_path / "diagnostics.json"), DownloadAdmission(), deadline_seconds=1,
        )
        auth = AdminAuth()
        auth.change_credentials("admin", "operator", "diagnostic-password")
        session = auth.issue_session(remember=True)
        csrf = auth.session_csrf(session)
        app = FastAPI()
        app.include_router(create_admin_router(
            auth=auth, limiter=RateLimiter(), diagnostics=manager,
            runtime=lambda: runtime, download_admission=manager.admission,
        ))
        user_a = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")
        user_b = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")
        try:
            anon = await user_a.get("/admin/sources/diagnostics")
            assert anon.status_code == 401
            user_a.cookies.set("admin_session", session)
            user_a.cookies.set("csrf_token", csrf)
            read = await user_a.get("/admin/sources/diagnostics")
            assert read.status_code == 200
            no_csrf = await user_a.post("/admin/sources/demo/diagnostics", json={})
            assert no_csrf.status_code == 403
            started = await user_a.post("/admin/sources/demo/diagnostics", json={"query": "晴天"},
                                        headers={"x-csrf-token": csrf})
            assert started.status_code == 200 and started.json()["mode"] == "scheduled"
            job_id = started.json()["job_id"]
            await run_until(lambda: manager.job(job_id)["status"] in {"running", "complete"})
            await primary_entered.wait()
            wrong_owner = await user_b.get(f"/admin/sources/diagnostics/jobs/{job_id}")
            assert wrong_owner.status_code == 401
            user_b.cookies.set("admin_session", auth.issue_session(remember=False))
            foreign = await manager.submit(runtime, "foreign", user_id="another-operator")
            await foreign_entered.wait()
            owner_denied = await user_b.get(
                f"/admin/sources/diagnostics/jobs/{foreign['job_id']}")
            assert owner_denied.status_code == 404
            assert await manager.cancel(foreign["job_id"]) == {
                "cancelled": True, "status": "cancelled",
            }
            cancelled = await user_a.post(f"/admin/sources/diagnostics/jobs/{job_id}/cancel",
                                          headers={"x-csrf-token": csrf})
            assert cancelled.status_code == 200
            assert cancelled.json() == {"cancelled": True, "status": "cancelled"}
            assert manager.job(job_id)["status"] == "cancelled"
            batch = await user_a.post("/admin/sources/diagnostics", json={},
                                      headers={"x-csrf-token": csrf})
            assert batch.status_code == 200 and batch.json()["requested"] == 2
            batch_jobs = [item["job_id"] for item in batch.json()["items"]]
            await run_until(lambda: all(manager.job(item)["status"] == "running"
                                         for item in batch_jobs))
            cancelled_batch = await manager.cancel_batch(batch.json()["batch_id"])
            assert cancelled_batch["cancelled"] == 2
            assert all(manager.job(item)["status"] == "cancelled" for item in batch_jobs)
        finally:
            await user_a.aclose()
            await user_b.aclose()
            await manager.close()

    asyncio.run(scenario())


def test_diagnostic_store_rejects_oversized_corrupt_and_mismatched_rows(tmp_path):
    path = tmp_path / "diagnostics.json"
    path.write_text(json.dumps({"version": 1, "results": [{
        "source_id": "demo", "source_version": "1",
        "fingerprint": "z" * 64, "status": "password=hunter2",
        "tested_at": 1, "query": "safe",
    }]}), encoding="utf-8")
    assert DiagnosticResultStore(path).snapshot({}) == []
    path.write_text("x" * (256 * 1024 + 1), encoding="utf-8")
    assert DiagnosticResultStore(path).snapshot({}) == []
    path.write_text("{broken", encoding="utf-8")
    assert DiagnosticResultStore(path).snapshot({}) == []


def test_diagnostic_store_persists_and_reloads_maximum_result_set(tmp_path):
    path = tmp_path / "maximum-diagnostics.json"
    store = DiagnosticResultStore(path)
    long_query = "q" * 500
    for index in range(256):
        assert store.record({
            "source_id": f"source-{index:03d}", "source_version": "1", "fingerprint": None,
            "status": "ok", "tested_at": index + 1, "query": long_query,
            "actual_query": long_query, "queries_tried": [long_query] * 3,
        })
    assert 256 * 1024 < path.stat().st_size < 4 * 1024 * 1024
    loaded = DiagnosticResultStore(path)
    rows = loaded.snapshot({})
    assert len(rows) == 256
    assert loaded.latest("source-255", "1", None)["queries_tried"] == [long_query] * 3


def test_diagnostic_cooldown_restores_from_persisted_completion_time(tmp_path):
    async def scenario():
        path = tmp_path / "cooldown-diagnostics.json"
        store = DiagnosticResultStore(path)
        assert store.record({
            "source_id": "demo", "source_version": "1", "fingerprint": None,
            "status": "ok", "tested_at": 1_000, "query": "saved query",
            "actual_query": "saved query", "queries_tried": ["saved query"],
        })
        wall_clock = {"now": 1_059.5}
        manager = SourceDiagnosticManager(
            DiagnosticResultStore(path), DownloadAdmission(),
            clock=lambda: wall_clock["now"], monotonic=lambda: 10.0,
            cooldown_seconds=60, deadline_seconds=2,
        )
        runtime = FakeRuntime(FakeSource())
        cooling = await manager.submit(runtime, "demo", user_id="operator", query="new query")
        assert cooling["mode"] == "cooldown" and cooling["retry_after"] == 1
        wall_clock["now"] = 1_060
        after_expiry = await manager.submit(runtime, "demo", user_id="operator", query="new query")
        assert after_expiry["mode"] == "scheduled"
        await manager.close()

    asyncio.run(scenario())
