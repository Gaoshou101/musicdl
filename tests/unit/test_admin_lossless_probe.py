"""The panel's manual lossless check for one channel.

A channel's lossless support cannot be read off a candidate: in this deployment
every candidate declares no tiers at all, so the search cannot tell a FLAC
channel from an MP3 one.  What a channel really serves is only knowable by
asking it, which is what the probe does, and this is the route that runs one
check on demand.  It is session- and CSRF-guarded like every other portal
mutation, it refuses a channel the runtime does not hold, and it answers with
exactly the verdict the probe reached -- never a verdict of its own.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.health import EventLogStore, SourceHealthStore
from musicdl.admin.management import SourceManager
from musicdl.admin.portal import create_admin_router
from musicdl.config import WorkerSettings
from musicdl.sources import SourceEntry, SourceRegistry
from musicdl.sources.lossless_probe import LosslessProbe
from musicdl.sources.models import Candidate

LOGIN = {"username": "operator", "password": "new-password"}


class Source:
    """A registered channel; the check never searches it, the registry holds it."""

    def __init__(self, source_id: str = "primary", version: str = "1.0.0") -> None:
        self.source_id, self.version = source_id, version

    async def search(self, query: str):
        return [Candidate(source_id=self.source_id, source_version=self.version, item_id="1",
                          title="稻香", artist="周杰伦")]


class Runtime:
    """The runtime the panel reaches the registry and the probe through."""

    def __init__(self, registry, *, probe=None) -> None:
        self.registry = registry
        # A deployment without a health roll-up assembles a runtime with no
        # probe at all; the route has to say so rather than invent a verdict.
        if probe is not None:
            self.lossless_probe = probe


class Resolver:
    """One fixed answer for the probe's single resolve."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, source_id: str, quality: str = "flac"):
        self.calls.append((source_id, quality))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def build(tmp_path, *, enabled: bool = True, registered: bool = True, wired: bool = True,
          probe=None, source_health=None, csrf: bool = True):
    source = Source()
    auth, sources = AdminAuth(), SourceManager()
    auth.change_credentials("admin", "operator", "new-password")
    if registered:
        sources.register({"id": "primary", "name": "主音源", "priority": 3, "enabled": enabled})
    entries = [SourceEntry("primary", "1.0.0", source, enabled=enabled)] if registered else []
    service = Runtime(SourceRegistry(entries), probe=probe) if wired else None
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, sources=sources, events=EventLogStore(),
                                           source_health=source_health, runtime=lambda: service,
                                           worker=WorkerSettings()))
    if csrf:
        app.add_middleware(CSRFMiddleware, auth=auth)
    return app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def signed_in(client) -> str:
    response = await client.post("/admin/login", json=LOGIN)
    return response.json()["csrf_token"]


def run(coro):
    return asyncio.run(coro)


def row(report, source_id: str = "primary"):
    return next(item for item in report["sources"] if item["id"] == source_id)


def test_the_manual_check_requires_a_session(tmp_path):
    async def scenario():
        app = build(tmp_path, csrf=False)
        async with client_for(app) as client:
            return await client.post("/admin/sources/primary/lossless-check")

    response = run(scenario())
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


def test_the_manual_check_requires_the_csrf_token(tmp_path):
    async def scenario():
        app = build(tmp_path)
        async with client_for(app) as client:
            await signed_in(client)
            return await client.post("/admin/sources/primary/lossless-check")

    response = run(scenario())
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


def test_the_manual_check_records_the_verdict_and_refreshes_the_row(tmp_path):
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="flac", quality="flac"))
    probe = LosslessProbe(resolver, store)

    async def scenario():
        app = build(tmp_path, probe=probe, source_health=store)
        async with client_for(app) as client:
            token = await signed_in(client)
            before = (await client.get("/admin/sources/health")).json()
            checked = await client.post("/admin/sources/primary/lossless-check",
                                        headers={"x-csrf-token": token})
            after = (await client.get("/admin/sources/health")).json()
            return before, checked, after

    before, checked, after = run(scenario())
    assert checked.status_code == 200
    body = checked.json()
    assert body["status"] == "lossless"
    assert body["evidence"] == {"extension": "flac", "quality": "flac"}
    assert isinstance(body["checked_at"], float)
    # The check asked the channel itself, once, for the one tier a capability
    # check is about.
    assert resolver.calls == [("primary", "flac")]
    assert row(before)["lossless_status"] == "unknown"
    primary = row(after)
    assert primary["lossless_status"] == "lossless"
    assert primary["lossless"] is True
    assert primary["lossless_checked_at"] == body["checked_at"]
    assert primary["lossless_evidence"] == {"extension": "flac", "quality": "flac"}


def test_a_probe_that_answers_lossy_is_passed_through(tmp_path):
    """A judged "no" is reported as the probe judged it, not softened."""
    store = SourceHealthStore()
    probe = LosslessProbe(Resolver(SimpleNamespace(extension="mp3", quality="320k")), store)

    async def scenario():
        app = build(tmp_path, probe=probe, source_health=store)
        async with client_for(app) as client:
            token = await signed_in(client)
            checked = await client.post("/admin/sources/primary/lossless-check",
                                        headers={"x-csrf-token": token})
            health = (await client.get("/admin/sources/health")).json()
            return checked, health

    checked, health = run(scenario())
    assert checked.status_code == 200
    assert checked.json()["status"] == "lossy"
    primary = row(health)
    assert primary["lossless_status"] == "lossy" and primary["lossless"] is False


def test_a_probe_that_cannot_answer_stays_unknown(tmp_path):
    """An unknown is the absence of an answer, never an invented "no"."""
    store = SourceHealthStore()
    probe = LosslessProbe(Resolver(RuntimeError("upstream is down")), store)

    async def scenario():
        app = build(tmp_path, probe=probe, source_health=store)
        async with client_for(app) as client:
            token = await signed_in(client)
            checked = await client.post("/admin/sources/primary/lossless-check",
                                        headers={"x-csrf-token": token})
            health = (await client.get("/admin/sources/health")).json()
            return checked, health

    checked, health = run(scenario())
    assert checked.status_code == 200
    assert checked.json()["status"] == "unknown"
    primary = row(health)
    # The row must not claim the channel is lossy; it simply does not know.
    assert primary["lossless_status"] == "unknown" and primary["lossless"] is None


def test_the_manual_check_is_not_found_for_a_source_the_runtime_lacks(tmp_path):
    async def scenario():
        app = build(tmp_path, registered=False, probe=LosslessProbe(Resolver(None), SourceHealthStore()))
        async with client_for(app) as client:
            token = await signed_in(client)
            return await client.post("/admin/sources/primary/lossless-check",
                                     headers={"x-csrf-token": token})

    response = run(scenario())
    assert response.status_code == 404
    assert response.json()["detail"] == "source not found"


def test_the_manual_check_is_not_found_for_a_disabled_source(tmp_path):
    async def scenario():
        app = build(tmp_path, enabled=False, probe=LosslessProbe(Resolver(None), SourceHealthStore()))
        async with client_for(app) as client:
            token = await signed_in(client)
            return await client.post("/admin/sources/primary/lossless-check",
                                     headers={"x-csrf-token": token})

    response = run(scenario())
    assert response.status_code == 404
    assert response.json()["detail"] == "source not found"


def test_the_manual_check_says_so_when_no_probe_is_wired(tmp_path):
    async def scenario():
        app = build(tmp_path, probe=None)
        async with client_for(app) as client:
            token = await signed_in(client)
            return await client.post("/admin/sources/primary/lossless-check",
                                     headers={"x-csrf-token": token})

    response = run(scenario())
    assert response.status_code == 503
    assert response.json()["detail"] == "lossless probe is unavailable"


def test_the_manual_check_says_so_when_the_runtime_is_missing(tmp_path):
    async def scenario():
        app = build(tmp_path, wired=False)
        async with client_for(app) as client:
            token = await signed_in(client)
            return await client.post("/admin/sources/primary/lossless-check",
                                     headers={"x-csrf-token": token})

    response = run(scenario())
    assert response.status_code == 503
    assert response.json()["detail"] == "search runtime is unavailable"