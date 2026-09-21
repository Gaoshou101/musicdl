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
from musicdl.admin.management import BotManager, SourceManager
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


def build(*, failing: bool = False, bots=None, source_health=None, entries=None):
    source = Source(failing=failing)
    auth, sources = AdminAuth(), SourceManager()
    auth.change_credentials("admin", "admin", "new-password")
    sources.register({"id": "primary", "name": "主音源", "priority": 3, "enabled": True})
    app = FastAPI()
    service = Runtime(SourceRegistry(entries if entries is not None else [SourceEntry("primary", "1.0.0", source)]))
    app.include_router(create_admin_router(auth=auth, sources=sources, events=EventLogStore(),
                                           bots=bots, source_health=source_health,
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


def test_a_telegram_bot_is_listed_as_a_configured_channel():
    """The Bot page defines a channel the plugin list knows nothing about.

    A Bot is searched and downloaded like any other channel, so a roll-up that
    reads only the plugin list showed the channel that was answering as one
    that had just been removed.
    """

    async def scenario():
        bots = BotManager()
        bots.register({"id": "music_v1bot", "username": "music_v1bot", "priority": 2})
        entries = [SourceEntry("primary", "1.0.0", Source()),
                   SourceEntry("music_v1bot", "music_v1bot", Source())]
        async with client_for(build(bots=bots, entries=entries)) as client:
            await signed_in(client)
            return (await client.get("/admin/sources/health")).json()

    bot = row(run(scenario()), "music_v1bot")
    assert bot["configured"] is True and bot["enabled"] is True
    assert bot["name"] == "music_v1bot" and bot["priority"] == 2
    assert bot["status"] == "unknown"


def test_a_bot_the_runtime_never_registered_is_not_a_channel():
    """A definition without a channel behind it stays off the roll-up.

    A deployment whose Telegram side is off keeps the shipped definition on
    the Bot page, and the runtime registers nothing for it.  Listing it here
    would be the same mistake in reverse: a channel that does not exist,
    reported as one that is merely idle.
    """

    async def scenario():
        bots = BotManager()
        bots.register({"id": "music_v1bot", "username": "music_v1bot"})
        async with client_for(build(bots=bots)) as client:
            await signed_in(client)
            return (await client.get("/admin/sources/health")).json()

    assert [item["id"] for item in run(scenario())["sources"]] == ["primary"]


def test_a_disabled_bot_is_history_rather_than_a_channel():
    async def scenario():
        bots = BotManager()
        bots.register({"id": "music_v1bot", "username": "music_v1bot"})
        bots.update("music_v1bot", enabled=False)
        store = SourceHealthStore()
        store.observe_search("music_v1bot", "ok", count=8)
        entries = [SourceEntry("primary", "1.0.0", Source()),
                   SourceEntry("music_v1bot", "music_v1bot", Source())]
        async with client_for(build(bots=bots, source_health=store, entries=entries)) as client:
            await signed_in(client)
            return (await client.get("/admin/sources/health")).json()

    bot = row(run(scenario()), "music_v1bot")
    assert bot["configured"] is False and bot["enabled"] is False
    # The listing it answered with is still worth reading.
    assert bot["status"] == "ok" and bot["last_count"] == 8


# -- the weight a search tie-break is given ------------------------------
#
# ``preference`` is what the panel hands to ``search_sources``: a small bounded
# number per channel, built from what this process actually observed. It never
# outweighs an operator's declared priority, so it only has to be right about
# which of two otherwise-equal channels has been answering.


def test_an_unobserved_channel_has_no_preference_either_way():
    assert SourceHealthStore().preference("primary") == 0


def test_a_channel_that_has_only_been_probed_has_no_preference():
    store = SourceHealthStore()
    store.observe_event(event("primary", "health", "success", healthy=True))
    assert store.preference("primary") == 0


def test_a_window_of_successes_prefers_a_channel_strongly():
    store = SourceHealthStore()
    store.observe_search("primary", "ok")
    assert store.preference("primary") == 10


def test_a_failed_search_costs_a_channel_more_than_its_rate_suggests():
    store = SourceHealthStore()
    store.observe_search("primary", "error")
    assert store.preference("primary") == -5

    store.observe_event(event("primary", "download", "failed", error_code="download_failed"))
    assert store.preference("primary") == -10


def test_a_successful_download_after_a_failed_search_reads_as_recovering():
    store = SourceHealthStore()
    store.observe_search("primary", "error")
    store.observe_event(event("primary", "download", "success"))

    # Half the window succeeded, the last download worked, the last search did not.
    assert store.preference("primary") == 5


def test_the_rate_is_the_rounded_share_of_the_window():
    store = SourceHealthStore()
    for status in ("success", "success", "success", "failed"):
        store.observe_event(event("primary", "refresh", status))

    assert store.preference("primary") == 8


def test_a_health_probe_never_enters_the_window():
    store = SourceHealthStore()
    store.observe_search("primary", "ok")
    store.observe_event(event("primary", "health", "failed", error_code="health_failed"))
    store.observe_event(event("primary", "health", "success", healthy=True))

    assert store.preference("primary") == 10


def test_the_preference_stays_bounded_and_never_raises():
    store = SourceHealthStore()
    for _ in range(5):
        store.observe_search("primary", "timeout")
        store.observe_event(event("primary", "download", "failed", error_code="media_timeout"))

    score = store.preference("primary")
    assert -20 <= score <= 20
    assert store.preference(None) == 0 and store.preference("") == 0
