"""The panel's own search box and its download button.

Neither is a WeCom feature: they run inside the application process, over the
same registry the workers search, and they are the way a music source is
exercised without sending the bot a message.
"""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import quote

import httpx
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.health import EventLogStore
from musicdl.admin.portal import create_admin_router
from musicdl.config import WorkerSettings
from musicdl.media.models import DownloadMetadata, _CloseOnce
from musicdl.sources import SourceEntry, SourceRegistry
from musicdl.sources.models import Candidate

ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
AUDIO = ID3 + b"frame-payload"
LOGIN = {"username": "operator", "password": "new-password"}


class Source:
    """An lx-shaped source: it answers search with one hit and streams it back."""

    source_id, version = "primary", "1.0.0"

    def __init__(self) -> None:
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
    assert payload["sources"] == [{"id": "primary", "status": "ok", "count": 1}]
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
