"""Durable observations of real downloads, independent of browser state."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.portal import create_admin_router
from musicdl.app import create_app
from musicdl.config import AppSettings
from musicdl.media.history import DownloadHistoryStore, DownloadJournal, HistoryCapacity, HistoryUnavailable
from musicdl.media.models import MediaError
from musicdl.sources import SourceEntry, SourceRegistry
from musicdl.sources.search import SearchResult
from test_admin_search_download import Source, Runtime, LOGIN


def run(coro):
    return asyncio.run(coro)


def candidate():
    from musicdl.sources.models import Candidate
    return Candidate(source_id="primary", source_version="1", item_id="1", title="Song", artist="Artist", format="mp3")


def portal(tmp_path, store, source=None, *, refresh=None, extra=None):
    source = source or Source()
    sources = {"primary": source, **(extra or {})}
    runtime = Runtime(SourceRegistry([SourceEntry(k, "1", v) for k, v in sources.items()]), sources)
    if refresh:
        runtime.refresh = refresh
    auth = AdminAuth()
    auth.change_credentials("admin", LOGIN["username"], LOGIN["password"])
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, runtime=lambda: runtime, media_root=tmp_path, history=store))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def login(c):
    result = await c.post("/admin/login", json=LOGIN)
    return {"x-csrf-token": result.json()["csrf_token"]}


def test_store_reopen_restart_and_bounded_retention(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = DownloadHistoryStore(path, limit=2, event_limit=2)
    store.begin("active", candidate(), origin="panel", query="Song")
    for number in range(3):
        task = f"done{number}"
        store.begin(task, candidate(), origin="panel", query="Song")
        store.finish(task, status="failed", elapsed_ms=1, error_code="media_timeout")
    for _ in range(4):
        store.append("active", {"stage": "resolve", "status": "started"})
    assert store.list()["total"] == 3
    assert store.get("active")["events_truncated"]
    assert [e["seq"] for e in store.get("active")["events"]] == [3, 4]
    store.close()
    store = DownloadHistoryStore(path, limit=3)
    # Opening or reading is not recovery, so router/hot reload is safe.
    assert store.get("active")["status"] == "running"
    store.recover()
    result = store.get("active")
    assert result["status"] == "interrupted" and result["error_code"] == "service_restarted"
    assert result["elapsed_ms"] is None
    assert store.get("done2")["status"] == "failed"
    store.close()


def test_running_capacity_and_duplicate_owner():
    store = DownloadHistoryStore(running_limit=1)
    first = DownloadJournal(store, "first", candidate(), origin="wecom", query="q")
    duplicate = DownloadJournal(store, "first", candidate(), origin="wecom", query="q")
    assert first.enabled and not duplicate.enabled
    duplicate.finish("interrupted", error_code="download_deferred")
    assert store.get("first")["status"] == "running"
    with pytest.raises(HistoryCapacity):
        store.begin("second", candidate(), origin="panel", query="q")
    first.finish("failed", error_code="download_failed")
    assert store.begin("second", candidate(), origin="panel", query="q")


def test_transient_startup_failure_recovers_before_admitting_new_work(monkeypatch):
    import sqlite3
    store = DownloadHistoryStore()
    store.begin("old", candidate(), origin="panel", query="q")
    original = store._db
    def unavailable():
        raise sqlite3.OperationalError("busy")
    monkeypatch.setattr(store, "_db", unavailable)
    with pytest.raises(HistoryUnavailable):
        store.recover()
    monkeypatch.setattr(store, "_db", original)
    store.begin("new", candidate(), origin="panel", query="q")
    assert store.get("old")["status"] == "interrupted"
    assert store.get("new")["status"] == "running"


def test_only_safe_metadata_is_persisted():
    store = DownloadHistoryStore()
    store.begin("task", candidate(), origin="panel", query="Song https://user:secret@host/song?token=secret")
    store.append("task", {"stage": "download", "status": "failed", "error_code": "https://host/secret",
                           "source_id": "primary", "relative_path": "/private/secret", "exception": "secret"})
    store.finish("task", status="failed", elapsed_ms=1, error_code="private secret")
    result = store.get("task")
    assert "secret" not in json.dumps(result)
    assert result["events"][0]["error_code"] == "download_failed"
    assert result["events"][0]["relative_path"] is None
    store.append("task", {"stage": "channel_switch", "status": "selected", "reason": "content_error:incomplete_audio"})
    assert store.get("task")["events"][-1]["reason"] == "content_error:incomplete_audio"


def test_history_session_csrf_pagination_and_missing_record(tmp_path):
    async def scenario():
        store = DownloadHistoryStore()
        app = portal(tmp_path, store)
        async with client(app) as c:
            assert (await c.get("/admin/downloads")).status_code == 401
            assert (await c.get("/admin/downloads/missing")).status_code == 401
            await login(c)
            assert (await c.get("/admin/downloads?limit=101")).status_code == 422
            assert (await c.get("/admin/downloads?offset=-1")).status_code == 422
            assert (await c.get("/admin/downloads/missing")).status_code == 404
            assert (await c.post("/admin/download", json={"candidate": candidate().model_dump()})).status_code == 403
            assert store.list()["total"] == 0
    run(scenario())


def test_success_survives_new_search_router_and_database_reopen(tmp_path):
    async def scenario():
        store = DownloadHistoryStore(tmp_path / "state" / "history.sqlite3")
        async with client(portal(tmp_path / "media", store)) as c:
            headers = await login(c)
            response = await c.post("/admin/download", headers=headers, json={"candidate": candidate().model_dump(), "query": "Song"})
            assert response.status_code == 200
            task_id = response.json()["request_id"]
            await c.get("/admin/search", params={"q": "other song"})
        store.close()
        store = DownloadHistoryStore(tmp_path / "state" / "history.sqlite3")
        async with client(portal(tmp_path / "media", store)) as c:
            await login(c)
            data = (await c.get("/admin/downloads")).json()
            assert data["total"] == 1 and data["items"][0]["status"] == "succeeded"
            detail = (await c.get(f"/admin/downloads/{task_id}")).json()
            assert detail["result"]["relative_path"] == response.json()["relative_path"]
            assert detail["actual_quality"] == "mp3"
            assert detail["created_at"].endswith("Z") and detail["elapsed_ms"] >= 0
            spans = [e for e in detail["events"] if e["stage"] in {"resolve", "transfer"} and e["status"] == "success"]
            assert {e["stage"] for e in spans} == {"resolve", "transfer"}
            assert all(e["elapsed_ms"] >= 0 and e["created_at"].endswith("Z") for e in spans)
        store.close()
    run(scenario())


def test_running_visible_then_cancelled_without_success(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        class Slow(Source):
            async def download(self, *a, **kw):
                entered.set()
                await asyncio.Event().wait()
        store = DownloadHistoryStore()
        async with client(portal(tmp_path, store, Slow())) as c:
            headers = await login(c)
            download = asyncio.create_task(c.post("/admin/download", headers=headers, json={"candidate": candidate().model_dump()}))
            await asyncio.wait_for(entered.wait(), 5)
            listing = (await c.get("/admin/downloads")).json()
            assert listing["items"][0]["status"] == "running"
            download.cancel()
            with pytest.raises(asyncio.CancelledError):
                await download
            detail = store.get(listing["items"][0]["id"])
            assert detail["status"] == "interrupted" and detail["error_code"] == "download_cancelled"
            assert detail["result"] is None
    run(scenario())


def test_failure_and_successful_fallback_retain_source_attempts(tmp_path):
    class Broken(Source):
        async def download(self, *a, **kw):
            raise MediaError("media_timeout")
    async def scenario():
        store = DownloadHistoryStore()
        async with client(portal(tmp_path, store, Broken())) as c:
            headers = await login(c)
            response = await c.post("/admin/download", headers=headers, json={"candidate": candidate().model_dump()})
            assert response.status_code == 504
            failed = store.list()["items"][0]
            assert failed["status"] == "failed" and failed["error_code"] == "media_timeout"
        backup = Source("backup")
        async def refresh(*args):
            other = candidate().model_copy(update={"source_id": "backup"})
            return SearchResult(version="1", candidates=(other,), statuses=())
        async with client(portal(tmp_path, store, Broken(), extra={"backup": backup}, refresh=refresh)) as c:
            headers = await login(c)
            response = await c.post("/admin/download", headers=headers, json={"candidate": candidate().model_dump()})
            assert response.status_code == 200, response.text
            data = store.get(response.json()["request_id"])
            assert data["status"] == "succeeded" and data["source_id"] == "backup"
            assert any(e["source_id"] == "primary" and e["status"] == "failed" for e in data["events"])
            assert any(e["source_id"] == "backup" and e["status"] == "success" for e in data["events"])
    run(scenario())


def test_database_failure_before_start_never_calls_source(tmp_path, monkeypatch):
    async def scenario():
        store = DownloadHistoryStore()
        source = Source()
        def broken(*a, **kw):
            raise HistoryUnavailable("history_unavailable")
        monkeypatch.setattr(store, "begin", broken)
        async with client(portal(tmp_path, store, source)) as c:
            headers = await login(c)
            response = await c.post("/admin/download", headers=headers, json={"candidate": candidate().model_dump()})
            assert response.status_code == 503 and not source.downloaded
    run(scenario())


@pytest.mark.parametrize("failed_write", ["append", "finish"])
def test_journal_failure_after_start_keeps_success_with_warning(tmp_path, monkeypatch, failed_write):
    async def scenario():
        store = DownloadHistoryStore()
        def broken(*a, **kw):
            raise HistoryUnavailable("history_unavailable")
        monkeypatch.setattr(store, failed_write, broken)
        async with client(portal(tmp_path, store)) as c:
            headers = await login(c)
            response = await c.post("/admin/download", headers=headers, json={"candidate": candidate().model_dump()})
            assert response.status_code == 200
            result = response.json()
            assert result["history_warning"] == "history_unavailable"
            assert (tmp_path / result["relative_path"]).is_file()
            assert store.get(result["request_id"])["history_warning"] == "history_unavailable"
    run(scenario())


def test_journal_file_is_never_served_even_when_state_directory_is_inside_media(tmp_path):
    async def scenario():
        store = DownloadHistoryStore(tmp_path / "download-history.sqlite3")
        store.begin("a", candidate(), origin="panel", query="q")
        async with client(portal(tmp_path, store)) as c:
            await login(c)
            assert (await c.get("/admin/media/download-history.sqlite3")).status_code == 404
        store.close()
    run(scenario())


def test_only_application_startup_recovers_pending_history(tmp_path):
    async def scenario():
        settings = AppSettings()
        settings.admin.state_path = str(tmp_path / "admin.json")
        runtime = Runtime(SourceRegistry([]), {})
        runtime.message_worker = runtime.job_worker = runtime.state = None
        app = create_app(settings, runtime_factory=lambda _: runtime)
        store = app.state.download_history
        store.begin("pending", candidate(), origin="panel", query="q")
        assert store.get("pending")["status"] == "running"
        async with app.router.lifespan_context(app):
            assert store.get("pending")["status"] == "interrupted"
            store.begin("new", candidate(), origin="panel", query="q")
            # Creating another router with the same store does not recover.
            create_admin_router(history=store)
            assert store.get("new")["status"] == "running"
    run(scenario())


def test_wecom_history_is_one_record_and_replay_still_uses_artifact_authority(monkeypatch):
    from test_worker_job import State, Redis, WeCom, JobWorker, seed_effect, ok_download, job_payload, effect
    state = State()
    seed_effect(state, "1-0", "download", owner="earlier", fence=1, result='{"ok":true}')
    state.script.hashes["{tenant}:artifact:1-0"] = {
        "job_id": "1-0", "candidate_id": "id", "temporary_relative_path": ".musicdl-staging/a.1.part",
        "target_relative_path": "未知/Artist/Song.mp3", "allocation_slot": "1", "extension": ".mp3",
        "media_type": "audio/mpeg", "size_bytes": "10", "sha256": "a"*64,
        "owner": "earlier", "fence": "1", "state": "published"}
    seen = []
    async def replay(candidate, sources, root, **kwargs):
        seen.append(kwargs["reservation"].state)
        return ok_download()
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback", replay)
    store = DownloadHistoryStore()
    worker = JobWorker(Redis(), WeCom(), {}, "/tmp", state=state, refresh=lambda *a: None, history=store)
    run(worker.handle_job(job_payload(), job_id="1-0"))
    first = store.list()["items"][0]
    assert first["origin"] == "wecom" and first["status"] == "succeeded"
    assert first["result"]["actual_quality"] == "mp3"
    run(worker.handle_job(job_payload(), job_id="1-0"))
    assert store.list()["total"] == 1
    assert seen == ["published", "published"]
    assert effect(state, "1-0", "download")["status"] == "done"


def test_wecom_history_failure_does_not_retry_successful_download(monkeypatch):
    from test_worker_job import State, Redis, WeCom, JobWorker, ok_download, job_payload
    store = DownloadHistoryStore()
    calls = []
    async def download(*a, **kw):
        calls.append(True)
        return ok_download()
    def broken(*a, **kw):
        raise HistoryUnavailable("history_unavailable")
    monkeypatch.setattr("musicdl.worker.workers.download_with_fallback", download)
    monkeypatch.setattr(store, "finish", broken)
    worker = JobWorker(Redis(), WeCom(), {}, "/tmp", state=State(), refresh=lambda *a: None, history=store)
    result = run(worker.handle_job(job_payload(), job_id="1-0"))
    assert result.download is not None and len(calls) == 1
    assert store.list()["items"][0]["history_warning"] == "history_unavailable"


def test_nested_guarded_resolver_is_one_timed_attempt():
    from musicdl.media.download import source_download
    class Guard:
        async def download(self, c, *, quality=None):
            return await source_download(Source(), c, quality=quality)
    async def scenario():
        store = DownloadHistoryStore()
        journal = DownloadJournal(store, "a", candidate(), origin="wecom", query="q")
        with journal.activate():
            result = await source_download(source=Guard(), candidate=candidate())
            await result.close()
        events = store.get("a")["events"]
        assert [(e["stage"], e["status"]) for e in events] == [("resolve", "started"), ("resolve", "success")]
    run(scenario())
