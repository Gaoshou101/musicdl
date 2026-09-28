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
import pytest
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

    def __init__(self, source_id: str = "primary", version: str = "1.0.0", quality: str | None = None) -> None:
        self.source_id, self.version, self.quality = source_id, version, quality
        self.queries: list[str] = []
        self.downloaded: list[str] = []
        # The tier the panel asks for arrives here as a keyword, and this is the
        # only place a test can see it: ``source_download`` omits the keyword
        # outright for a source that cannot take one, so recording the whole
        # kwargs dict tells "asked for flac" apart from "asked for nothing".
        self.download_kwargs: list[dict] = []

    async def search(self, query: str):
        self.queries.append(query)
        return [Candidate(source_id=self.source_id, source_version=self.version, item_id="1",
                          title="稻香", artist="周杰伦", album="魔杰座", format="mp3")]

    async def download(self, candidate: Candidate, **kwargs):
        self.downloaded.append(candidate.item_id)
        self.download_kwargs.append(kwargs)

        async def chunks():
            yield AUDIO

        async def close() -> None:
            return None

        return DownloadMetadata(chunks=chunks(), extension="mp3", media_type="audio/mpeg",
                                declared_size=len(AUDIO), quality=self.quality,
                                _close_once=_CloseOnce(close))


class Runtime:
    def __init__(self, registry, resolvers, *, language_advisor=None, lossless_capability=None) -> None:
        self.registry, self.resolvers = registry, resolvers
        # Deliberately leave these absent for the fixtures that do not need
        # them.  The portal must tolerate the lightweight runtime used by
        # existing tests, so each one is only set when a test pins it.
        if language_advisor is not None:
            self.language_advisor = language_advisor
        if lossless_capability is not None:
            self.lossless_capability = lossless_capability


def build(tmp_path, *, wired: bool = True, language_advisor=None, quality=None, worker=None):
    source = Source(quality=quality)
    auth, events = AdminAuth(), EventLogStore()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    service = (Runtime(SourceRegistry([SourceEntry("primary", "1.0.0", source)]), {"primary": source},
                       language_advisor=language_advisor)
               if wired else None)
    app.include_router(create_admin_router(auth=auth, events=events, runtime=lambda: service,
                                           media_root=tmp_path, worker=worker or WorkerSettings()))
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
    assert payload["relative_path"].startswith("华语/")
    assert payload["language"] == "华语"
    assert (tmp_path / payload["relative_path"]).read_bytes() == AUDIO
    assert played.status_code == 200 and played.content == AUDIO
    assert source.downloaded == ["1"]
    assert blocked.status_code == 403
    assert events.page()["items"][0]["source_id"] == "primary"


def test_download_report_separates_the_tier_asked_for_from_the_tier_delivered(tmp_path):
    """The panel report says which tier was requested and which one arrived."""
    async def scenario():
        app, source, _ = build(tmp_path, quality="320k")
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            return await client.post("/admin/download", json={"candidate": candidate},
                                     headers={"x-csrf-token": token})

    payload = run(scenario()).json()
    # The candidate declared no tiers, so ``lossless_first`` asks for FLAC
    # anyway; the source answered 320k.  The request is reported as the intent
    # it was, and the answer is the tier the bytes really are, never an echo.
    assert payload["requested_quality"] == "flac"
    assert payload["actual_quality"] == "320k"


def test_download_report_names_the_container_when_no_tier_is_stated(tmp_path):
    """A source that names no tier still produces a report as full as the file."""
    async def scenario():
        # ``best_available`` is the pinned 1.0.5 behaviour: with no tier asked
        # for, the container is the only tier the report can carry.
        app, source, _ = build(tmp_path, worker=WorkerSettings(quality_policy="best_available"))
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            return await client.post("/admin/download", json={"candidate": candidate},
                                     headers={"x-csrf-token": token})

    payload = run(scenario()).json()
    assert payload["requested_quality"] is None and payload["actual_quality"] == "mp3"


def test_download_uses_the_runtime_advisor_for_the_directory_and_response(tmp_path):
    async def advisor(candidate):
        return "日韩"

    async def scenario():
        app, _, _ = build(tmp_path, language_advisor=advisor)
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            return await client.post("/admin/download", json={"candidate": candidate},
                                     headers={"x-csrf-token": token})

    response = run(scenario())
    payload = response.json()
    assert response.status_code == 200
    assert payload["relative_path"].startswith("日韩/")
    assert payload["language"] == "日韩"


@pytest.mark.parametrize("answer", ["Unknown", "zh", [], {}], ids=["unknown", "zh", "list", "dict"])
def test_download_falls_back_to_deterministic_language_for_bad_advice(tmp_path, answer):
    async def advisor(candidate):
        if answer == "Unknown":
            raise RuntimeError("advisor unavailable")
        if answer == "zh":
            raise TimeoutError("advisor timed out")
        return answer

    async def scenario():
        app, _, _ = build(tmp_path, language_advisor=advisor)
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            return await client.post("/admin/download", json={"candidate": candidate},
                                     headers={"x-csrf-token": token})

    response = run(scenario())
    payload = response.json()
    assert response.status_code == 200
    assert payload["relative_path"].startswith("华语/")
    assert payload["language"] == "华语"


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

    async def download(self, candidate: Candidate, **kwargs):
        self.downloaded.append(candidate.item_id)
        self.download_kwargs.append(kwargs)
        raise MediaError(self.code)


class FallbackRuntime(Runtime):
    """The runtime a real deployment assembles, refresh callback included."""

    def __init__(self, registry, resolvers, refreshed, *, language_advisor=None) -> None:
        super().__init__(registry, resolvers, language_advisor=language_advisor)
        self.refreshed, self.queries = refreshed, []

    async def refresh(self, query, failed_source_ids=frozenset()):
        self.queries.append(query)
        return self.refreshed


def copy_of(source: Source, *, duration: int = 210) -> Candidate:
    """One channel's listing of the recording every retry test is about."""
    return Candidate(source_id=source.source_id, source_version=source.version, item_id="1",
                     title="稻香", artist="周杰伦", album="魔杰座", duration=duration, format="mp3")


def fallback_app(tmp_path, sources, refreshed, *, language_advisor=None, worker=None):
    """Mount the panel over a runtime whose download can fall back."""
    registry = SourceRegistry([SourceEntry(s.source_id, s.version, s) for s in sources])
    service = FallbackRuntime(registry, {s.source_id: s for s in sources}, refreshed,
                              language_advisor=language_advisor)
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, events=EventLogStore(), runtime=lambda: service,
                                           media_root=tmp_path, worker=worker or WorkerSettings()))
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


def test_fallback_reuses_one_advised_language_for_every_attempt(tmp_path, monkeypatch):
    calls = []

    async def advisor(candidate):
        calls.append(candidate.item_id)
        return "日韩"

    broken, backup = BrokenSource(), Source("backup")
    # ``best_available`` keeps both attempts on the plain path, which is where
    # each one goes through the downloader; the ``lossless_first`` tier
    # forwarding has its own tests below.
    app, service = fallback_app(tmp_path, [broken, backup],
                                SearchResult((copy_of(backup),), (), "v"),
                                language_advisor=advisor,
                                worker=WorkerSettings(quality_policy="best_available"))

    # Both the fallback helper's first attempt and the portal's replacement
    # attempt must receive the same already-resolved value.
    from musicdl.admin import portal
    from musicdl.media import fallback as fallback_module

    seen = []
    portal_download = portal.download_candidate
    fallback_download = fallback_module.download_candidate

    async def record_portal_download(*args, **kwargs):
        seen.append(("replacement", kwargs.get("language")))
        return await portal_download(*args, **kwargs)

    async def record_fallback_download(*args, **kwargs):
        seen.append(("fallback", kwargs.get("language")))
        return await fallback_download(*args, **kwargs)

    monkeypatch.setattr(portal, "download_candidate", record_portal_download)
    monkeypatch.setattr(fallback_module, "download_candidate", record_fallback_download)

    response = fetch(app, {"candidate": PRIMARY, "query": "稻香 周杰伦"})

    payload = response.json()
    assert response.status_code == 200
    assert payload["relative_path"].startswith("日韩/")
    assert payload["language"] == "日韩"
    assert calls == ["1"]
    assert seen == [("fallback", "日韩"), ("replacement", "日韩")]


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

    async def download(self, candidate: Candidate, **kwargs):
        await asyncio.sleep(0.3)
        self.served = True
        return await super().download(candidate, **kwargs)


def app_for(tmp_path, service, *, worker=None):
    """Mount the panel over one assembled runtime."""
    auth, events = AdminAuth(), EventLogStore()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, events=events, runtime=lambda: service,
                                           media_root=tmp_path, worker=worker or WorkerSettings()))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app


def app_with(tmp_path, source):
    service = Runtime(SourceRegistry([SourceEntry(source.source_id, "1.0.0", source)]),
                      {source.source_id: source})
    return app_for(tmp_path, service)


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


# -- the tier the panel asks for -------------------------------------------
#
# ``lossless_first`` is the deployment's default, but the panel's download used
# to leave the tier to the shim's own default.  In this deployment every
# candidate has ``qualities == ()`` -- the main process answers the search --
# so the panel downloaded 320k mp3 even under ``lossless_first``.  The panel
# now computes the tier exactly the way the worker does, from the same settings
# object, and forwards it to every channel it asks.


def test_lossless_first_asks_the_direct_channel_for_flac(tmp_path):
    """This deployment's real shape: no declared tiers still means "ask for FLAC"."""
    async def scenario():
        app, source, _ = build(tmp_path)
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            response = await client.post("/admin/download", json={"candidate": candidate},
                                         headers={"x-csrf-token": token})
        return response, source

    response, source = run(scenario())
    assert response.status_code == 200
    assert response.json()["requested_quality"] == "flac"
    assert source.download_kwargs == [{"quality": "flac"}]


def test_lossless_first_asks_the_fallback_and_the_replacement_for_flac(tmp_path):
    """The retry helper and the replacement each carry the tier they were asked for."""
    broken, backup = BrokenSource(), Source("backup")
    app, _ = fallback_app(tmp_path, [broken, backup], SearchResult((copy_of(backup),), (), "v"))

    response = fetch(app, {"candidate": PRIMARY, "query": "稻香 周杰伦"})

    payload = response.json()
    assert response.status_code == 200
    assert payload["source_id"] == "backup" and payload["fallback_from"] == "primary"
    assert payload["requested_quality"] == "flac"
    # The fallback helper's own first attempt, and the portal's replacement --
    # which recomputes the tier from its own candidate -- both asked for FLAC.
    assert broken.download_kwargs == [{"quality": "flac"}]
    assert backup.download_kwargs == [{"quality": "flac"}]


def test_best_available_asks_the_direct_channel_for_no_tier(tmp_path):
    """The pinned 1.0.5 behaviour: nothing asked for, nothing forwarded."""
    async def scenario():
        app, source, _ = build(tmp_path, worker=WorkerSettings(quality_policy="best_available"))
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            response = await client.post("/admin/download", json={"candidate": candidate},
                                         headers={"x-csrf-token": token})
        return response, source

    response, source = run(scenario())
    assert response.status_code == 200
    assert response.json()["requested_quality"] is None
    # ``source_download`` omits the keyword outright for a source that cannot
    # take one, so "no tier" is an empty forward, not ``quality=None``.
    assert source.download_kwargs == [{}]


def test_best_available_asks_the_fallback_and_the_replacement_for_no_tier(tmp_path):
    broken, backup = BrokenSource(), Source("backup")
    app, _ = fallback_app(tmp_path, [broken, backup], SearchResult((copy_of(backup),), (), "v"),
                          worker=WorkerSettings(quality_policy="best_available"))

    response = fetch(app, {"candidate": PRIMARY, "query": "稻香 周杰伦"})

    assert response.status_code == 200 and response.json()["requested_quality"] is None
    assert broken.download_kwargs == [{}] and backup.download_kwargs == [{}]


def test_a_flac_request_answered_with_mp3_is_reported_as_a_downgrade(tmp_path):
    """The report carries the tier asked for beside the tier the bytes turned out to be."""
    async def scenario():
        app, source, _ = build(tmp_path)
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            return await client.post("/admin/download", json={"candidate": candidate},
                                     headers={"x-csrf-token": token})

    payload = run(scenario()).json()
    assert payload["requested_quality"] == "flac"
    assert payload["actual_quality"] == "mp3"


def test_the_panel_search_passes_the_measured_capability(tmp_path, monkeypatch):
    """The panel's own search reads the same capability the probe recorded."""
    from musicdl.admin import portal

    def capability(source_id: str) -> bool:
        return source_id == "primary"

    captured: dict = {}
    real = portal.search_sources

    async def capture(registry, query, **kwargs):
        captured.update(kwargs)
        return await real(registry, query, **kwargs)

    monkeypatch.setattr(portal, "search_sources", capture)
    source = Source()
    measured = Runtime(SourceRegistry([SourceEntry("primary", "1.0.0", source)]),
                       {"primary": source}, lossless_capability=capability)

    async def scenario(app):
        async with client_for(app) as client:
            await signed_in(client)
            return await client.get("/admin/search", params={"q": "稻香"})

    assert run(scenario(app_for(tmp_path, measured))).status_code == 200
    assert captured["lossless_capability"] is capability
    # The capability is only one half of the ordering bias; the policy it is
    # applied under has to come from the worker too, or a best_available
    # deployment would still run its panel search lossless-first.
    assert captured["quality_policy"] == "lossless_first"
    assert captured["quality_preference"] is None

    # A runtime assembled without a roll-up passes nothing rather than
    # inventing a verdict, and the search keeps the order it always had.
    captured.clear()
    plain = Runtime(SourceRegistry([SourceEntry("primary", "1.0.0", source)]), {"primary": source})
    assert run(scenario(app_for(tmp_path, plain))).status_code == 200
    assert captured["lossless_capability"] is None
    assert captured["quality_policy"] == "lossless_first"
    assert captured["quality_preference"] is None


def test_the_panel_search_forwards_the_workers_quality_settings(tmp_path, monkeypatch):
    """The panel's search runs under the worker's policy and preference."""
    from musicdl.admin import portal

    captured: dict = {}
    real = portal.search_sources

    async def capture(registry, query, **kwargs):
        captured.update(kwargs)
        return await real(registry, query, **kwargs)

    monkeypatch.setattr(portal, "search_sources", capture)
    source = Source()
    service = Runtime(SourceRegistry([SourceEntry("primary", "1.0.0", source)]), {"primary": source})
    worker = WorkerSettings(quality_policy="best_available", quality_preference="flac")

    async def scenario():
        async with client_for(app_for(tmp_path, service, worker=worker)) as client:
            await signed_in(client)
            return await client.get("/admin/search", params={"q": "稻香"})

    assert run(scenario()).status_code == 200
    assert captured["quality_policy"] == "best_available"
    assert captured["quality_preference"] == "flac"
    assert captured["lossless_capability"] is None


def test_best_available_keeps_the_channel_order_v105_left_it(tmp_path, monkeypatch):
    """Under ``best_available`` the panel's search must not bias by capability.

    Two channels list the same recording and were never ranked apart, so the
    stable order picks ``alpha``.  ``beta`` was measured lossless-capable: under
    ``lossless_first`` that promotes it, and under ``best_available`` the bias
    must not apply, leaving the row exactly where v1.0.5 would have put it.
    """
    from musicdl.admin import portal

    def capability(source_id: str) -> bool:
        return source_id == "beta"

    forwarded: list[dict] = []
    real = portal.search_sources

    async def capture(registry, query, **kwargs):
        forwarded.append(kwargs)
        return await real(registry, query, **kwargs)

    monkeypatch.setattr(portal, "search_sources", capture)

    def panel(worker):
        alpha, beta = Source("alpha"), Source("beta")
        registry = SourceRegistry([SourceEntry("alpha", "1.0.0", alpha),
                                   SourceEntry("beta", "1.0.0", beta)])
        service = Runtime(registry, {"alpha": alpha, "beta": beta}, lossless_capability=capability)
        auth, events = AdminAuth(), EventLogStore()
        auth.change_credentials("admin", "operator", "new-password")
        app = FastAPI()
        app.include_router(create_admin_router(auth=auth, events=events, runtime=lambda: service,
                                               media_root=tmp_path, worker=worker))
        app.add_middleware(CSRFMiddleware, auth=auth)
        return app

    async def listed(app):
        async with client_for(app) as client:
            await signed_in(client)
            return (await client.get("/admin/search", params={"q": "稻香 周杰伦"})).json()

    v105 = run(listed(panel(WorkerSettings(quality_policy="best_available"))))
    assert forwarded[-1]["quality_policy"] == "best_available"
    assert forwarded[-1]["lossless_capability"] is capability
    assert v105["count"] == 1
    assert v105["candidates"][0]["source_id"] == "alpha"
    assert v105["offers"] == [["alpha", "beta"]]

    promoted = run(listed(panel(WorkerSettings())))
    assert forwarded[-1]["quality_policy"] == "lossless_first"
    assert promoted["count"] == 1
    assert promoted["candidates"][0]["source_id"] == "beta"
    assert promoted["offers"] == [["beta", "alpha"]]
