"""Per-channel health, and the two panel paths that feed it.

Dependency health answers "is Redis up"; this answers "does this channel still
work", which is the question an operator has while looking at one source. The
panel's own search and download are what exercise a source outside the message
pipeline, so both are what the roll-up is built from.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.health import EventLogStore, SourceHealthStore
from musicdl.admin.management import SourceManager
from musicdl.admin.portal import create_admin_router
from musicdl.config import WorkerSettings
from musicdl.media.models import DownloadEvent
from musicdl.sources import SourceEntry, SourceRegistry
from musicdl.sources.models import Candidate

LOGIN = {"username": "admin", "password": "new-password"}


def event(source_id: str, stage: str, status: str, *, error_code=None, healthy=None) -> DownloadEvent:
    return DownloadEvent("req", "item", source_id, "1.0.0", stage, status,
                         error_code=error_code, healthy=healthy)


def row(report: dict, source_id: str) -> dict:
    return next(item for item in report["sources"] if item["id"] == source_id)


# -- the roll-up itself --------------------------------------------------


def test_an_unexercised_channel_is_listed_and_says_unknown():
    report = SourceHealthStore().snapshot([{"id": "primary", "name": "主音源", "enabled": True, "priority": 10}])
    primary = report["sources"][0]
    assert primary["status"] == "unknown"
    assert primary["attempts"] == 0 and primary["success_rate"] is None
    assert primary["name"] == "主音源" and primary["configured"] is True


def test_a_successful_search_makes_a_channel_ok():
    store = SourceHealthStore()
    store.observe_search("primary", "ok", count=3)
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert primary["status"] == "ok"
    assert (primary["searches"], primary["attempts"], primary["successes"]) == (1, 1, 1)
    assert primary["success_rate"] == 1.0
    assert primary["last_search"] == "ok" and primary["last_count"] == 3


@pytest.mark.parametrize("status", ["timeout", "error", "invalid"])
def test_every_unsuccessful_search_status_is_a_failure(status):
    store = SourceHealthStore()
    store.observe_search("primary", status)
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert primary["status"] == "failing"
    assert primary["failures"] == 1 and primary["successes"] == 0
    assert primary["last_error"] == f"search_{status}" and primary["last_error_stage"] == "search"


def test_recovering_after_a_failure_reads_as_degraded():
    store = SourceHealthStore()
    store.observe_search("primary", "error")
    store.observe_search("primary", "ok")
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert primary["status"] == "degraded"
    assert (primary["attempts"], primary["successes"], primary["failures"]) == (2, 1, 1)
    assert primary["success_rate"] == 0.5
    # The last attempt succeeded, so the surviving error is history, not state.
    assert primary["last_error"] == "search_error"


def test_the_window_bounds_what_the_rate_is_computed_from():
    store = SourceHealthStore(window=2)
    for status in ("ok", "error", "ok"):
        store.observe_search("primary", status)
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    # The first outcome has aged out of the window: it is still counted in
    # ``searches`` but not in the rate, which is what keeps the rate a picture
    # of now rather than of the process's whole life.
    assert primary["attempts"] == 2 and primary["searches"] == 3
    assert (primary["successes"], primary["failures"], primary["success_rate"]) == (1, 1, 0.5)
    assert primary["status"] == "degraded"


def test_the_number_of_tracked_channels_is_capped():
    store = SourceHealthStore(limit=2)
    for name in ("a", "b", "c"):
        store.observe_search(name, "ok")
    report = store.snapshot()
    assert [item["id"] for item in report["sources"]] == ["a", "b"]


def test_a_download_and_a_health_probe_are_recorded_separately():
    store = SourceHealthStore()
    store.observe_event(event("primary", "download", "success"))
    store.observe_event(event("primary", "health", "success", healthy=True))
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert primary["status"] == "ok" and primary["downloads"] == 1
    assert primary["last_download"] == "success"
    assert primary["last_health"] is True and primary["last_health_status"] == "success"
    # A probe is not an attempt: it says the channel is up, not that it served.
    assert primary["attempts"] == 1


def test_a_failed_download_names_the_channel_that_failed():
    store = SourceHealthStore()
    store.observe_event(event("primary", "download", "failed", error_code="media_timeout"))
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert primary["status"] == "failing"
    assert primary["last_error"] == "media_timeout" and primary["last_error_stage"] == "download"


def test_a_cleanup_failure_is_not_charged_to_the_channel():
    store = SourceHealthStore()
    store.observe_event(event("primary", "cleanup", "failed", error_code="cleanup_failed"))
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert primary["status"] == "unknown" and primary["attempts"] == 0 and primary["last_error"] is None


def test_a_refresh_is_counted_the_way_a_download_is():
    """The fallback pipeline refreshes the catalogue after a failed download.

    A refresh that fails is the same channel failing to answer a search, so it
    counts; a refresh that succeeds after a failed download is the channel
    recovering, which is what makes the verdict ``degraded`` rather than ``ok``.
    """
    store = SourceHealthStore()
    store.observe_event(event("primary", "download", "failed", error_code="download_failed"))
    store.observe_event(event("primary", "refresh", "success"))
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert (primary["downloads"], primary["refreshes"]) == (1, 1)
    assert primary["status"] == "degraded"
    assert primary["last_refresh"] == "success"
    assert primary["last_error"] == "download_failed" and primary["last_error_stage"] == "download"


def test_a_failed_refresh_is_the_channel_failing():
    store = SourceHealthStore()
    store.observe_event(event("primary", "refresh", "failed", error_code="refresh_failed"))
    primary = row(store.snapshot([{"id": "primary", "enabled": True, "priority": 1}]), "primary")
    assert primary["status"] == "failing" and primary["refreshes"] == 1
    assert primary["last_error"] == "refresh_failed" and primary["last_error_stage"] == "refresh"


def test_a_channel_with_traffic_but_no_configuration_is_still_reported():
    store = SourceHealthStore()
    store.observe_search("retired", "error")
    report = store.snapshot([{"id": "primary", "enabled": True, "priority": 1}])
    retired = row(report, "retired")
    assert retired["configured"] is False and retired["enabled"] is False
    assert retired["status"] == "failing" and retired["last_error"] == "search_error"
    assert report["sources"][0]["id"] == "primary"


def test_a_bad_window_or_limit_is_refused():
    for kwargs in ({"window": 0}, {"window": 201}, {"window": True}, {"limit": 0}):
        with pytest.raises(ValueError):
            SourceHealthStore(**kwargs)


# -- the route -----------------------------------------------------------


class Source:
    source_id, version = "primary", "1.0.0"

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing

    async def search(self, query: str):
        if self.failing:
            raise RuntimeError("upstream is down")
        return [Candidate(source_id=self.source_id, source_version=self.version, item_id="1",
                          title="稻香", artist="周杰伦")]


class Runtime:
    def __init__(self, registry) -> None:
        self.registry = registry


def build(*, failing: bool = False):
    source = Source(failing=failing)
    auth, sources = AdminAuth(), SourceManager()
    auth.change_credentials("admin", "admin", "new-password")
    sources.register({"id": "primary", "name": "主音源", "priority": 3, "enabled": True})
    app = FastAPI()
    service = Runtime(SourceRegistry([SourceEntry("primary", "1.0.0", source)]))
    app.include_router(create_admin_router(auth=auth, sources=sources, events=EventLogStore(),
                                           runtime=lambda: service, worker=WorkerSettings()))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def signed_in(client) -> None:
    assert (await client.post("/admin/login", json=LOGIN)).status_code == 200


def run(coro):
    return asyncio.run(coro)


def test_source_health_requires_a_session():
    async def scenario():
        async with client_for(build()) as client:
            response = await client.get("/admin/sources/health")
            return response.status_code, response.json()["detail"]

    assert run(scenario()) == (401, "authentication required")


def test_a_panel_search_answers_with_the_configured_channels():
    async def scenario():
        async with client_for(build()) as client:
            await signed_in(client)
            before = (await client.get("/admin/sources/health")).json()
            search = await client.get("/admin/search", params={"q": "稻香"})
            after = (await client.get("/admin/sources/health")).json()
            return before, search, after

    before, search, after = run(scenario())
    assert search.status_code == 200
    assert row(before, "primary")["status"] == "unknown"
    primary = row(after, "primary")
    assert primary["status"] == "ok" and primary["last_search"] == "ok"
    assert primary["name"] == "主音源" and primary["priority"] == 3
    assert after["window"] == SourceHealthStore.WINDOW


def test_a_channel_that_fails_its_search_is_reported_as_failing():
    async def scenario():
        async with client_for(build(failing=True)) as client:
            await signed_in(client)
            await client.get("/admin/search", params={"q": "稻香"})
            return (await client.get("/admin/sources/health")).json()["sources"][0]

    primary = run(scenario())
    assert primary["status"] == "failing"
    assert primary["last_error"] == "search_error" and primary["last_error_stage"] == "search"
