"""The panel's own search box and its download button.

Neither is a WeCom feature: they run inside the application process, over the
same registry the workers search, and they are the way a music source is
exercised without sending the bot a message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import quote

import httpx
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.health import EventLogStore
from musicdl.admin.portal import create_admin_router
from musicdl.config import WorkerSettings
from musicdl.media.models import DownloadMetadata, MediaError, _CloseOnce
from musicdl.sources import SourceEntry, SourceRegistry
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult

ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
AUDIO = ID3 + b"frame-payload"
LOGIN = {"username": "operator", "password": "new-password"}


class Source:
    """An lx-shaped source: it answers search with one hit and streams it back."""

    def __init__(self, source_id: str = "primary", version: str = "1.0.0") -> None:
        self.source_id, self.version = source_id, version
        self.queries: list[str] = []
        self.downloaded: list[str] = []

    async def search(self, query: str):
        self.queries.append(query)
        return [Candidate(source_id=self.source_id, source_version=self.version, item_id="1",
                          title="稻香", artist="周杰伦", album="魔杰座", format="mp3")]

    async def download(self, candidate: Candidate):
        self.downloaded.append(candidate.item_id)

        async def chunks():
            yield AUDIO

        async def close() -> None:
            return None

        return DownloadMetadata(chunks=chunks(), extension="mp3", media_type="audio/mpeg",
                                declared_size=len(AUDIO), _close_once=_CloseOnce(close))


class Runtime:
    def __init__(self, registry, resolvers) -> None:
        self.registry, self.resolvers = registry, resolvers


def build(tmp_path, *, wired: bool = True):
    source = Source()
    auth, events = AdminAuth(), EventLogStore()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    service = (Runtime(SourceRegistry([SourceEntry("primary", "1.0.0", source)]), {"primary": source})
               if wired else None)
    app.include_router(create_admin_router(auth=auth, events=events, runtime=lambda: service,
                                           media_root=tmp_path, worker=WorkerSettings()))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app, source, events


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def signed_in(client) -> str:
    response = await client.post("/admin/login", json=LOGIN)
    return response.json()["csrf_token"]


async def first_candidate(client) -> dict:
    response = await client.get("/admin/search", params={"q": "稻香 周杰伦"})
    return response.json()["candidates"][0]


def run(coro):
    return asyncio.run(coro)


def test_search_requires_a_session(tmp_path):
    async def scenario():
        app, _, _ = build(tmp_path)
        async with client_for(app) as client:
            return await client.get("/admin/search", params={"q": "x"})

    assert run(scenario()).status_code == 401


def test_search_answers_with_candidates_and_a_status_per_source(tmp_path):
    async def scenario():
        app, source, _ = build(tmp_path)
        async with client_for(app) as client:
            await signed_in(client)
            return await client.get("/admin/search", params={"q": "稻香 周杰伦"})

    response = run(scenario())
    payload = response.json()
    assert response.status_code == 200
    assert payload["query"] == "稻香 周杰伦" and payload["count"] == 1 and payload["total"] == 1
    assert payload["candidates"][0]["artist"] == "周杰伦"
    assert payload["sources"] == [{"id": "primary", "status": "ok", "count": 1, "catalogue": False}]
    # Which channels stand behind each row, in the order a download tries them.
    assert payload["offers"] == [["primary"]]
    assert len(payload["version"]) == 64


def test_search_without_an_assembled_runtime_is_reported_as_unavailable(tmp_path):
    async def scenario():
        app, _, _ = build(tmp_path, wired=False)
        async with client_for(app) as client:
            await signed_in(client)
            return await client.get("/admin/search", params={"q": "稻香"})

    response = run(scenario())
    assert response.status_code == 503
    assert "runtime" in response.json()["detail"]


def test_search_refuses_an_empty_query(tmp_path):
    async def scenario():
        app, source, _ = build(tmp_path)
        async with client_for(app) as client:
            await signed_in(client)
            return await client.get("/admin/search", params={"q": "   "})

    assert run(scenario()).status_code == 422


def test_download_writes_the_artifact_below_the_media_root_and_serves_it_back(tmp_path):
    async def scenario():
        app, source, events = build(tmp_path)
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            downloaded = await client.post("/admin/download", json={"candidate": candidate},
                                           headers={"x-csrf-token": token})
            relative = downloaded.json()["relative_path"]
            played = await client.get(f"/admin/media/{quote(relative)}")
            blocked = await client.post("/admin/download", json={"candidate": candidate})
        return downloaded, played, blocked, source, events

    downloaded, played, blocked, source, events = run(scenario())
    payload = downloaded.json()
    assert downloaded.status_code == 200
    assert payload["sha256"] == hashlib.sha256(AUDIO).hexdigest()
    assert payload["size_bytes"] == len(AUDIO) and payload["media_type"] == "audio/mpeg"
    assert payload["relative_path"].endswith(".mp3")
    assert (tmp_path / payload["relative_path"]).read_bytes() == AUDIO
    assert played.status_code == 200 and played.content == AUDIO
    assert source.downloaded == ["1"]
    assert blocked.status_code == 403
    assert events.page()["items"][0]["source_id"] == "primary"


def test_download_of_a_candidate_no_source_can_resolve_is_refused(tmp_path):
    async def scenario():
        app, _, _ = build(tmp_path)
        async with client_for(app) as client:
            token = await signed_in(client)
            return await client.post("/admin/download",
                                     json={"candidate": {"source_id": "ghost", "source_version": "1",
                                                         "item_id": "9", "title": "ghost",
                                                         "artist": "ghost"}},
                                     headers={"x-csrf-token": token})

    assert run(scenario()).status_code == 404


def test_download_refuses_a_body_that_is_not_a_candidate(tmp_path):
    async def scenario():
        app, _, _ = build(tmp_path)
        async with client_for(app) as client:
            token = await signed_in(client)
            return await client.post("/admin/download", json={"candidate": {"title": "only a title"}},
                                     headers={"x-csrf-token": token})

    assert run(scenario()).status_code == 422


def test_media_route_stays_inside_the_media_root(tmp_path):
    (tmp_path.parent / "secret.mp3").write_bytes(b"not yours")

    async def scenario():
        app, _, _ = build(tmp_path)
        async with client_for(app) as client:
            await signed_in(client)
            missing = await client.get("/admin/media/nope.mp3")
            outside = await client.get("/admin/media/../secret.mp3")
        return missing, outside

    missing, outside = run(scenario())
    assert missing.status_code == 404
    assert outside.status_code == 404

# -- the cross-channel retry ---------------------------------------------
#
# A channel can stop answering between the search that listed a track and the
# download that fetches it, and one dead aggregator used to fail every track
# the panel listed. The runtime a deployment assembles carries the same refresh
# callback the background worker retries with, and the panel's download uses it
# for exactly one more attempt.

PRIMARY = {"source_id": "primary", "source_version": "1.0.0", "item_id": "1", "title": "稻香",
           "artist": "周杰伦", "album": "魔杰座", "duration": 210, "format": "mp3"}


class BrokenSource(Source):
    """A channel that lists a track and then cannot serve its audio."""

    def __init__(self, source_id: str = "primary", code: str = "download_failed") -> None:
        super().__init__(source_id)
        self.code = code

    async def download(self, candidate: Candidate):
        self.downloaded.append(candidate.item_id)
        raise MediaError(self.code)


class FallbackRuntime(Runtime):
    """The runtime a real deployment assembles, refresh callback included."""

    def __init__(self, registry, resolvers, refreshed) -> None:
        super().__init__(registry, resolvers)
        self.refreshed, self.queries = refreshed, []

    async def refresh(self, query, failed_source_ids=frozenset()):
        self.queries.append(query)
        return self.refreshed


def copy_of(source: Source, *, duration: int = 210) -> Candidate:
    """One channel's listing of the recording every retry test is about."""
    return Candidate(source_id=source.source_id, source_version=source.version, item_id="1",
                     title="稻香", artist="周杰伦", album="魔杰座", duration=duration, format="mp3")


def fallback_app(tmp_path, sources, refreshed):
    """Mount the panel over a runtime whose download can fall back."""
    registry = SourceRegistry([SourceEntry(s.source_id, s.version, s) for s in sources])
    service = FallbackRuntime(registry, {s.source_id: s for s in sources}, refreshed)
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, events=EventLogStore(), runtime=lambda: service,
                                           media_root=tmp_path, worker=WorkerSettings()))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app, service


def fetch(app, body: dict):
    """Sign in and press the panel's download button once."""
    async def scenario():
        async with client_for(app) as client:
            token = await signed_in(client)
            return await client.post("/admin/download", json=body, headers={"x-csrf-token": token})

    return run(scenario())


def test_download_retries_on_another_channel_that_has_the_same_track(tmp_path):
    broken, backup = BrokenSource(), Source("backup")
    app, service = fallback_app(tmp_path, [broken, backup], SearchResult((copy_of(backup),), (), "v"))

    response = fetch(app, {"candidate": PRIMARY, "query": "稻香 周杰伦"})

    payload = response.json()
    assert response.status_code == 200
    assert payload["source_id"] == "backup" and payload["fallback_from"] == "primary"
    assert broken.downloaded == ["1"] and backup.downloaded == ["1"]
    assert service.queries == ["稻香 周杰伦"]
    assert (tmp_path / payload["relative_path"]).read_bytes() == AUDIO


def test_the_retry_searches_the_title_when_the_panel_hands_no_query(tmp_path):
    broken, backup = BrokenSource(), Source("backup")
    app, service = fallback_app(tmp_path, [broken, backup], SearchResult((copy_of(backup),), (), "v"))

    assert fetch(app, {"candidate": PRIMARY}).status_code == 200
    assert service.queries == ["稻香"]


def test_download_reports_the_failure_when_no_other_channel_has_the_track(tmp_path):
    broken = BrokenSource()
    app, service = fallback_app(tmp_path, [broken, Source("backup")], SearchResult((), (), "v"))

    response = fetch(app, {"candidate": PRIMARY})

    assert response.status_code == 502 and response.json()["detail"] == "download_failed"
    assert service.queries == ["稻香"]
    assert broken.downloaded == ["1"]


def test_a_failed_retry_is_reported_and_no_third_channel_is_tried(tmp_path):
    broken, also_broken, third = BrokenSource(), BrokenSource("backup", "media_url_denied"), Source("third")
    refreshed = SearchResult((copy_of(also_broken), copy_of(third)), (), "v")
    app, _ = fallback_app(tmp_path, [broken, also_broken, third], refreshed)

    response = fetch(app, {"candidate": PRIMARY})

    assert response.status_code == 502 and response.json()["detail"] == "media_url_denied"
    assert also_broken.downloaded == ["1"] and third.downloaded == []


def test_a_candidate_whose_own_channel_cannot_resolve_still_downloads(tmp_path):
    """A search-only channel lists tracks it can never serve; another one can."""
    backup = Source("backup")
    app, _ = fallback_app(tmp_path, [BrokenSource(), backup], SearchResult((copy_of(backup),), (), "v"))

    response = fetch(app, {"candidate": dict(PRIMARY, source_id="searcher")})

    payload = response.json()
    assert response.status_code == 200
    assert payload["source_id"] == "backup" and payload["fallback_from"] == "searcher"


def test_a_closer_duration_wins_when_two_channels_offer_the_track(tmp_path):
    broken, short, long = BrokenSource(), Source("short"), Source("long")
    refreshed = SearchResult((copy_of(long, duration=260), copy_of(short, duration=212)), (), "v")
    app, _ = fallback_app(tmp_path, [broken, short, long], refreshed)

    response = fetch(app, {"candidate": PRIMARY})

    assert response.status_code == 200
    assert response.json()["source_id"] == "short" and long.downloaded == []


def test_a_failed_download_leaves_a_warning_in_the_service_log(tmp_path, caplog):
    app, _ = fallback_app(tmp_path, [BrokenSource()], SearchResult((), (), "v"))

    with caplog.at_level(logging.WARNING, logger="musicdl.admin"):
        response = fetch(app, {"candidate": PRIMARY})

    assert response.status_code == 502
    assert any("panel download failed" in record.getMessage() for record in caplog.records)

class SlowSource(Source):
    """A channel that streams through something slower than a CDN.

    A Telegram Bot hands a file over at whatever the account's link allows; it
    says so with ``stream_budget_seconds``, and the panel has to read that
    rather than cut the download off at the CDN-sized budget.
    """

    def __init__(self, budget: float) -> None:
        super().__init__("bot")
        self.stream_budget_seconds = budget
        self.served = False

    async def download(self, candidate: Candidate):
        await asyncio.sleep(0.3)
        self.served = True
        return await super().download(candidate)


def app_with(tmp_path, source):
    auth, events = AdminAuth(), EventLogStore()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    service = Runtime(SourceRegistry([SourceEntry(source.source_id, "1.0.0", source)]),
                      {source.source_id: source})
    app.include_router(create_admin_router(auth=auth, events=events, runtime=lambda: service,
                                           media_root=tmp_path, worker=WorkerSettings()))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app


def test_the_panel_gives_a_slow_channel_the_budget_it_asks_for(tmp_path):
    """The configured 15s is for a CDN; a channel that names its own budget gets it."""

    async def scenario(budget):
        source = SlowSource(budget)
        async with client_for(app_with(tmp_path, source)) as client:
            token = await signed_in(client)
            candidate = (await client.get("/admin/search", params={"q": "稻香"})).json()["candidates"][0]
            response = await client.post("/admin/download", json={"candidate": candidate},
                                         headers={"x-csrf-token": token})
        return response, source

    cut_off, ignored = run(scenario(0.05))
    served, slow = run(scenario(60.0))

    assert cut_off.status_code == 504 and cut_off.json()["detail"] == "media_timeout"
    assert ignored.served is False
    assert served.status_code == 200 and slow.served is True
