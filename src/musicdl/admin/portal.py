from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path, PurePosixPath
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from html import escape
from pydantic import ValidationError
from typing import Any, Callable

from musicdl.config import AppSettings
from musicdl.media import download_candidate, download_with_fallback
from musicdl.media.models import MediaError
from musicdl.plugins.install import install_source, preview_source
from musicdl.sources.models import Candidate, normalize_text
from musicdl.sources.search import search_sources
from .auth import AdminAuth, RateLimiter
from .config import ConfigManager
from .forms import form_fields
from .health import EventLogStore, HealthAggregator, SourceHealthStore
from .logs import LogBuffer
from .management import BotManager, SourceManager
from .pages import credentials_form, credentials_page, login_page


# The panel's own log lines, which the service-log window reads back. A failed
# download leaves no event that says why beyond its code, and the operator
# looking at this window is the person who just clicked the button.
_LOG = logging.getLogger("musicdl.admin")


def _positive(value: Any, default: float) -> float:
    """A usable positive budget, or the default the portal falls back to."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def _same_recording(wanted: Candidate, other: Candidate) -> bool:
    """Whether two channels offered the same recording.

    The two disagree about item ids and versions by construction, so only the
    parts a listener would hear take part. Duration orders two otherwise-equal
    candidates instead of deciding whether they are equal, because a channel
    that simply omits it still means the same song.
    """
    return (normalize_text(wanted.title) == normalize_text(other.title)
            and normalize_text(wanted.artist) == normalize_text(other.artist))


def _replacement(wanted: Candidate, candidates, resolvers) -> Candidate | None:
    """The best other channel's copy of a recording that just failed.

    ``candidates`` arrives in the refresh's own ranked order -- the registry
    already preferred one of them -- so the only preference applied here is
    closeness in duration: a channel that listed the track at 3:31 is a better
    guess for the 3:30 that just failed than one that listed some much longer
    version. A channel that cannot resolve media is skipped, because it would
    fail for a reason that has nothing to do with the one that just failed.
    """
    def distance(other: Candidate) -> int:
        if wanted.duration is None or other.duration is None:
            return 0
        return abs(other.duration - wanted.duration)

    matches = [item for item in candidates or ()
               if item.source_id != wanted.source_id and item.source_id in resolvers
               and _same_recording(wanted, item)]
    return min(matches, key=distance) if matches else None


def _shared_catalogue(registry, source_id: str) -> bool:
    """Whether one channel answers out of the shared catalogue search.

    Every installed lx source resolves against the same four catalogues and
    runs no search code of its own, so their answers are not independent
    findings.  The panel says so once instead of printing one identical result
    count per channel, which is what the count alone would claim.
    """
    entry = registry.get(source_id) if registry is not None else None
    return bool(getattr(getattr(entry, "source", None), "catalogue_shared", False))


def _media_target(root: str | Path, relative_path: str) -> Path:
    """Resolve one artifact below the media root, or refuse it.

    ``download_candidate`` reports a path it derived from the media root; serving
    that path back must not become a way to read any other file, so it is
    re-checked here rather than trusted.
    """
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
        raise HTTPException(404, "media not found")
    parts = PurePosixPath(relative_path)
    if parts.is_absolute() or any(part in {"", ".", ".."} for part in parts.parts):
        raise HTTPException(404, "media not found")
    root_path = Path(root).resolve(strict=False)
    try:
        resolved = root_path.joinpath(*parts.parts).resolve(strict=True)
        resolved.relative_to(root_path)
    except (OSError, ValueError):
        raise HTTPException(404, "media not found") from None
    if not resolved.is_file() or resolved.is_symlink():
        raise HTTPException(404, "media not found")
    return resolved


def create_admin_router(*, auth: AdminAuth | None = None, sources: SourceManager | None = None,
                        health: HealthAggregator | None = None, events: EventLogStore | None = None,
                        limiter: RateLimiter | None = None, bots: SourceManager | None = None,
                        audit: EventLogStore | None = None,
                        config: ConfigManager | None = None,
                        source_health: SourceHealthStore | None = None,
                        logs: LogBuffer | None = None,
                        plugins: Callable[[], Any] | None = None,
                        runtime: Callable[[], Any] | None = None,
                        media_root: str | Path | None = None,
                        worker: Any | None = None) -> APIRouter:
    auth, sources, health, events, limiter = auth or AdminAuth(), sources or SourceManager(), health or HealthAggregator({}), events or EventLogStore(), limiter or RateLimiter()
    bots, audit = bots or BotManager(), audit or EventLogStore()
    config = config or ConfigManager(AppSettings())
    source_health = source_health or SourceHealthStore()
    logs = logs or LogBuffer()
    router = APIRouter(prefix="/admin")
    search_timeout = _positive(getattr(worker, "search_timeout", None), 10.0)
    resolve_timeout = _positive(getattr(worker, "resolve_stream_timeout", None), 30.0)
    health_timeout = _positive(getattr(worker, "health_timeout", None), 10.0)
    credential_paths = {"/admin/", "/admin/change-credentials", "/admin/change-credentials-form"}

    def start_session(response: Response) -> str:
        """Issue a session and bind the CSRF token its forms will carry."""
        session = auth.issue_session()
        csrf = secrets.token_urlsafe(24)
        auth.bind_csrf(session, csrf)
        response.set_cookie("admin_session", session, httponly=True, secure=True, samesite="lax")
        response.set_cookie("csrf_token", csrf, httponly=False, secure=True, samesite="lax")
        return csrf

    def active_runtime():
        """The runtime the panel searches and downloads through.

        It is built at start-up and independent of WeCom, so a deployment with
        no WeCom account still answers here; a deployment whose runtime could
        not be assembled says so instead of failing halfway through a request.
        """
        service = runtime() if runtime is not None else None
        if service is None or getattr(service, "registry", None) is None:
            raise HTTPException(503, "search runtime is unavailable")
        return service

    def plugin_store():
        """The plugin storage this portal may install into; absent means no code."""
        store = open_store()
        if store is None:
            raise HTTPException(503, "plugin storage is unavailable")
        return store

    def open_store():
        """Open the plugin storage, or report its absence instead of raising.

        A read of ``/admin/sources`` must not fail because the volume that
        holds installed code is missing or unreadable: the portal still knows
        every source definition it owns, and the installed script is shown as
        absent.
        """
        if plugins is None:
            return None
        try:
            return plugins()
        except (OSError, ValueError):
            return None

    def installed(store) -> dict[str, dict]:
        """Map source id to the script the store currently serves for it."""
        index: dict[str, dict] = {}
        if store is None:
            return index
        try:
            stored = store.enabled()
        except (OSError, ValueError):
            return index
        for item in stored:
            manifest = item.manifest
            index.setdefault(manifest.plugin_id, plugin_descriptor(manifest))
        return index

    def plugin_descriptor(manifest) -> dict:
        """What the portal shows about one installed script.

        The egress policy is part of it: an operator who widened a source has to
        be able to read back exactly which widenings are in force.
        """
        return {"sha256": manifest.sha256, "version": manifest.version,
                "language": manifest.language, "egress": manifest.egress.model_dump(mode="json")}

    def with_plugin(index: dict[str, dict], item: dict) -> dict:
        return dict(item, plugin=index.get(item["id"]))

    def require(request: Request):
        session = request.cookies.get("admin_session")
        if not auth.session_user(session): raise HTTPException(401, "authentication required")
        if auth.session_must_change(session) and request.url.path not in credential_paths: raise HTTPException(403, "credential change required")
        return session

    def mutate(request: Request):
        session = require(request)
        if not auth.valid_csrf(session, request.headers.get("x-csrf-token")): raise HTTPException(403, "CSRF validation failed")
        return session

    @router.post("/login")
    async def login(request: Request, response: Response):
        if not limiter.allow(request.client.host if request.client else "unknown"):
            audit.append({"action": "login", "status": "rate_limited"})
            raise HTTPException(429, "too many login attempts")
        body = await request.json()
        result = auth.authenticate(str(body.get("username", "")), str(body.get("password", "")))
        if not result.ok:
            audit.append({"action": "login", "status": "failed"})
            raise HTTPException(401, "invalid credentials")
        csrf = start_session(response)
        audit.append({"action": "login", "status": "success"})
        return {"ok": True, "must_change": result.must_change, "csrf_token": csrf}

    @router.post("/login-form")
    async def login_form(request: Request):
        """The browser's own login: one page in, one redirect out.

        Same credential check, same rate limit and same audit record as
        ``/admin/login``; only the reply differs, because a browser has to be
        able to read why it was refused and follow the redirect itself.
        """
        form = await form_fields(request)
        if not limiter.allow(request.client.host if request.client else "unknown"):
            audit.append({"action": "login", "status": "rate_limited"})
            return HTMLResponse(login_page(error="登录尝试过于频繁，请稍后再试。"), status_code=429)
        result = auth.authenticate(str(form.get("username", "")), str(form.get("password", "")))
        if not result.ok:
            audit.append({"action": "login", "status": "failed"})
            return HTMLResponse(login_page(error="用户名或密码不正确。"), status_code=401)
        response = RedirectResponse("/admin/", status_code=303)
        start_session(response)
        audit.append({"action": "login", "status": "success"})
        return response

    @router.post("/change-credentials")
    async def change_credentials(request: Request, response: Response):
        session = mutate(request); body = await request.json()
        result = auth.authenticate(auth.session_user(session) or "", str(body.get("password", "")))
        if not result.ok: raise HTTPException(401, "invalid credentials")
        try:
            auth.change_credentials(result.user_id or "", str(body.get("username", "")), str(body.get("new_password", "")))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        replacement = auth.issue_session(); csrf = secrets.token_urlsafe(24); auth.bind_csrf(replacement, csrf)
        response.set_cookie("admin_session", replacement, httponly=True, secure=True, samesite="lax")
        response.set_cookie("csrf_token", csrf, secure=True, samesite="lax")
        audit.append({"action": "change_credentials", "status": "success"})
        return {"ok": True, "csrf_token": csrf}

    @router.post("/change-credentials-form")
    async def change_credentials_form(request: Request):
        """The same credential change, posted from the dashboard's form.

        A deployment that still carries the default password reaches this page
        first, so it has to succeed or fail in a browser rather than in JSON.
        """
        session = require(request)
        form = await form_fields(request)
        result = auth.authenticate(auth.session_user(session) or "", str(form.get("password", "")))
        if not result.ok:
            audit.append({"action": "change_credentials", "status": "failed"})
            return HTMLResponse(credentials_page(csrf=auth.session_csrf(session) or "",
                                                 error="当前密码不正确。"), status_code=401)
        try:
            auth.change_credentials(result.user_id or "", str(form.get("username", "")),
                                    str(form.get("new_password", "")))
        except ValueError:
            audit.append({"action": "change_credentials", "status": "failed"})
            return HTMLResponse(credentials_page(csrf=auth.session_csrf(session) or "",
                                                 error="凭据未被接受：用户名不能为空，新密码至少 8 位，且不能与默认密码相同。"),
                                status_code=422)
        response = RedirectResponse("/admin/", status_code=303)
        start_session(response)
        audit.append({"action": "change_credentials", "status": "success"})
        return response

    @router.get("/sources")
    async def list_sources(request: Request):
        require(request)
        index = installed(open_store())
        return {"items": [with_plugin(index, item) for item in sources.list()]}

    @router.get("/sources/health")
    async def read_source_health(request: Request):
        """What each channel did last, and how it went.

        The panel's own search and download are the two paths that exercise a
        source outside the message pipeline, so both report here: one search in
        the panel is enough to make every enabled channel answer with something
        other than ``unknown``, and a download adds what the search cannot say,
        which is whether the channel can actually serve the audio.

        This is a roll-up, not a log: the individual attempts stay in
        ``GET /admin/events``, and this answers the question an operator asks
        while looking at a source -- is this one working.
        """
        require(request)
        return source_health.snapshot(sources.list())

    @router.post("/sources/analyze")
    async def analyze_import(body: dict, request: Request):
        """Preview one import, and store nothing.

        The operator is about to hand the portal a script it has never seen, so
        the screen that does it has to be able to say, before anything is
        written, what the script declares about itself, which egress widenings
        it needs, and whether an install with these grants would be accepted.
        A POST because one source file is far larger than a query string, and
        because the same CSRF guard as every other portal change then covers it;
        the plugin store is not opened at all.  The source's own portal settings
        -- its name, priority and timeout -- are validated by the route that
        saves them, not by this preview.
        """
        mutate(request)
        try:
            return preview_source(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.post("/sources")
    async def create_source(body: dict, request: Request):
        mutate(request)
        store = plugin_store()
        try:
            stored = install_source(store, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(500, "plugin storage is unavailable") from None
        # One source id serves one script. Installing a second version retires
        # the first, because two enabled versions of one id is a registry the
        # runtime refuses to assemble at all.
        replaced = []
        try:
            for item in store.enabled():
                manifest = item.manifest
                if manifest.plugin_id == stored.manifest.plugin_id and manifest.sha256 != stored.manifest.sha256:
                    store.set_enabled(manifest.plugin_id, manifest.sha256, False)
                    replaced.append(manifest.sha256)
        except (OSError, ValueError):
            raise HTTPException(500, "plugin replacement failed") from None
        source_id = stored.manifest.plugin_id
        changes = {key: body[key] for key in ("name", "enabled", "priority", "timeout") if key in body}
        try:
            if source_id in {item["id"] for item in sources.list()}:
                row = sources.update(source_id, **changes)
            else:
                row = sources.register({"id": source_id, **changes}, persist=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit.append({"action": "create_source", "source_id": source_id, "status": "success",
                      "sha256": stored.manifest.sha256, "replaced": sorted(replaced)})
        return dict(row, plugin={"sha256": stored.manifest.sha256, "version": stored.manifest.version,
                                 "language": stored.manifest.language,
                                 "egress": stored.manifest.egress.model_dump(mode="json")})

    @router.patch("/sources/{source_id}")
    async def update_source(source_id: str, body: dict, request: Request):
        mutate(request)
        try:
            result = sources.update(source_id, enabled=body.get("enabled"), priority=body.get("priority"), timeout=body.get("timeout"), name=body.get("name")); audit.append({"action": "update_source", "source_id": source_id, "status": "success"}); return result
        except KeyError:
            raise HTTPException(404, "source not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.delete("/sources/{source_id}")
    async def delete_source(source_id: str, request: Request):
        mutate(request)
        if source_id not in {item["id"] for item in sources.list()}:
            raise HTTPException(404, "source not found")
        store = open_store()
        uninstalled: list[str] = []
        if store is not None:
            try:
                uninstalled = sorted(store.remove(source_id))
            except KeyError:
                pass
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            except OSError:
                raise HTTPException(500, "plugin storage is unavailable") from None
        removed = sources.remove(source_id)
        audit.append({"action": "delete_source", "source_id": source_id, "status": "success",
                      "versions": uninstalled})
        return {**removed, "uninstalled": uninstalled}

    @router.get("/search")
    async def search(request: Request, q: str = "", limit: int = 50):
        """The panel's own search: exactly the query the workers run.

        It answers from the registry the runtime assembled, so a search here
        exercises the same sources a WeCom message would reach, and it needs no
        Redis, no WeCom account and no running worker.
        """
        require(request)
        service = active_runtime()
        if not 1 <= limit <= 200:
            raise HTTPException(422, "invalid limit")
        try:
            result = await search_sources(service.registry, q, timeout=search_timeout)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # A search the panel ran is also the cheapest health probe every enabled
        # source can get, so each answer is recorded against its channel.
        for status in result.statuses:
            source_health.observe_search(status.source_id, status.status, count=status.count)
        page = result.candidates[:limit]
        return {"query": q.strip(), "version": result.version, "count": len(page),
                "total": len(result.candidates),
                "candidates": [candidate.public_representation for candidate in page],
                # Aligned with ``candidates``: which channels stand behind each
                # row, best first.  One catalogue search answers for every
                # installed lx source, so a row reached through twelve channels
                # is one row here, not twelve identical ones.
                "offers": [list(offered) for offered in result.offers[:len(page)]],
                "sources": [{"id": status.source_id, "status": status.status, "count": status.count,
                             "catalogue": _shared_catalogue(service.registry, status.source_id)}
                            for status in result.statuses]}

    @router.post("/download")
    async def download(body: dict, request: Request):
        """Download one candidate the search just listed, into the media root.

        The body carries the candidate itself rather than an id the server has
        to remember: the portal keeps no per-session state, and a candidate is
        already a complete, validated description of one recording.

        A channel can stop answering between the search that listed a track and
        the download that fetches it. When the runtime carries the worker's own
        refresh callback, one failed attempt is followed by a single retry on
        another channel's copy of the same recording, so a dead aggregator no
        longer fails every track the panel lists. The retry is bounded at one,
        and a runtime assembled without the callback keeps the older
        single-channel behaviour.
        """
        mutate(request)
        service = active_runtime()
        if media_root is None:
            raise HTTPException(503, "media root is unavailable")
        try:
            candidate = Candidate.model_validate(body.get("candidate"))
        except ValidationError:
            raise HTTPException(422, "invalid candidate") from None
        resolvers = getattr(service, "resolvers", None) or {}
        refresh = getattr(service, "refresh", None)
        request_id = secrets.token_hex(16)
        # The operator searched for something; the retry has to search for the
        # same thing rather than for whatever the candidate happens to be
        # titled on the channel that failed.
        handed = body.get("query")
        query = handed.strip() if isinstance(handed, str) and handed.strip() else candidate.title

        def recorded(event) -> None:
            """Keep the attempt in the log and in the channel's roll-up."""
            events.append(event)
            source_health.observe_event(event)

        def report(source_id: str, fallback_from: str | None, result) -> dict:
            """One successful download, and which channel actually served it."""
            return {"request_id": request_id, "source_id": source_id, "fallback_from": fallback_from,
                    "relative_path": str(result.relative_path).replace(os.sep, "/"),
                    "sha256": result.sha256, "size_bytes": result.size_bytes,
                    "media_type": result.media_type, "extension": result.extension,
                    "language": getattr(result.language, "value", result.language)}

        def failed(code: str) -> HTTPException:
            """The refusal to return, and one line for the service-log window.

            The event log already holds every attempt the media pipeline made;
            what it does not hold is why the operator's click ended the way it
            did, in their words rather than in error codes.
            """
            _LOG.warning("panel download failed: query=%s candidate=%s source=%s error=%s",
                         query, candidate.item_id, candidate.source_id, code)
            return HTTPException(504 if code == "media_timeout" else 502, code)

        if refresh is None or len(resolvers) < 2:
            source = resolvers.get(candidate.source_id)
            if source is None:
                raise HTTPException(404, "source cannot resolve media")
            try:
                async with asyncio.timeout(resolve_timeout):
                    result = await download_candidate(candidate, source, media_root,
                                                      request_id=request_id, record=recorded)
            except MediaError as exc:
                raise failed(exc.code) from None
            except TimeoutError:
                raise failed("media_timeout") from None
            return report(candidate.source_id, None, result)

        # Each stage below carries its own budget, which is what bounds the
        # call: one resolve, one refresh, one health probe, and at most one
        # more resolve when the refresh produced another channel's copy.
        attempt = await download_with_fallback(
            candidate, resolvers, media_root, request_id=request_id, query=query, refresh=refresh,
            resolve_stream_timeout=resolve_timeout, refresh_timeout=search_timeout,
            health_timeout=health_timeout, record=recorded)
        if attempt.download is not None:
            return report(candidate.source_id, None, attempt.download)
        code = attempt.download_error or "download_failed"
        replacement = _replacement(candidate, getattr(attempt.refreshed, "candidates", ()), resolvers)
        if replacement is not None:
            try:
                async with asyncio.timeout(resolve_timeout):
                    result = await download_candidate(replacement, resolvers[replacement.source_id],
                                                      media_root, request_id=request_id, record=recorded)
            except MediaError as exc:
                code = exc.code
            except TimeoutError:
                code = "media_timeout"
            else:
                return report(replacement.source_id, candidate.source_id, result)
        raise failed(code)

    @router.get("/media/{relative_path:path}")
    async def media(relative_path: str, request: Request):
        """Serve one downloaded artifact, so a browser can play what it fetched."""
        require(request)
        if media_root is None:
            raise HTTPException(503, "media root is unavailable")
        return FileResponse(_media_target(media_root, relative_path))

    @router.get("/bots")
    async def list_bots(request: Request):
        require(request); return {"items": bots.list()}

    @router.post("/bots")
    async def create_bot(body: dict, request: Request):
        mutate(request)
        try:
            created = bots.register(body, persist=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit.append({"action": "create_bot", "bot_id": created["id"], "status": "success"})
        return created

    @router.patch("/bots/{bot_id}")
    async def update_bot(bot_id: str, body: dict, request: Request):
        mutate(request)
        try: result = bots.update(bot_id, enabled=body.get("enabled"), priority=body.get("priority"), timeout=body.get("timeout"), username=body.get("username"), command_template=body.get("command_template"))
        except KeyError: raise HTTPException(404, "bot not found") from None
        except ValueError as exc: raise HTTPException(422, str(exc)) from None
        audit.append({"action": "update_bot", "bot_id": bot_id, "status": "success"}); return result

    @router.delete("/bots/{bot_id}")
    async def delete_bot(bot_id: str, request: Request):
        mutate(request)
        try:
            removed = bots.remove(bot_id)
        except KeyError:
            raise HTTPException(404, "bot not found") from None
        audit.append({"action": "delete_bot", "bot_id": bot_id, "status": "success"})
        return removed

    @router.get("/health")
    async def health_report(request: Request):
        require(request)
        return await health.check()

    @router.get("/config")
    async def read_config(request: Request):
        """What the panel owns, what the deployment owns, and what is in force.

        A secret is reported as set or unset and never as a value: the screen an
        operator uses to change a credential is not a screen that can read one
        back.
        """
        require(request)
        return config.describe()

    @router.patch("/config")
    async def write_config(body: dict, request: Request):
        """Store one batch of settings and adopt what this process can.

        A null clears an override and lets the deployment's own value stand
        again; a blank secret means "leave the stored one alone", so a form that
        cannot render an existing secret cannot erase it by accident.
        """
        mutate(request)
        values = body.get("values") if isinstance(body, dict) else None
        try:
            result = config.update(values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit.append({"action": "update_config", "status": "success",
                      "keys": sorted(values)})
        return result

    @router.get("/events")
    async def event_page(request: Request, offset: int = 0, limit: int = 50):
        require(request)
        if offset < 0 or limit < 1 or limit > 200:
            raise HTTPException(422, "invalid pagination")
        return events.page(offset=offset, limit=limit)

    @router.get("/audit")
    async def audit_page(request: Request, offset: int = 0, limit: int = 50):
        require(request)
        if offset < 0 or limit < 1 or limit > 200: raise HTTPException(422, "invalid pagination")
        return audit.page(offset=offset, limit=limit)

    @router.get("/logs")
    async def service_logs(request: Request, limit: int = 200, after: int = 0, level: str | None = None):
        """What this process logged while the panel was running.

        The two stores beside this one answer questions the portal asked; this
        one answers what the service printed, which is what an operator needs
        when something failed for a reason no event carries. It is a cursor
        rather than a page number, because a window that polls must not
        reprint what it already shows -- the client passes the last id it has
        and gets only what came after.
        """
        require(request)
        try:
            return logs.page(limit=limit, after=after, level=level)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        session = request.cookies.get("admin_session")
        if not auth.session_user(session):
            # The panel's front door is a page: a browser arriving at /admin/
            # has no other way in, while the API routes keep answering JSON.
            return HTMLResponse(login_page())
        require(request)
        warning = ""
        if auth.session_must_change(session):
            warning = ("<p>SECURITY WARNING: credential change required (强制修改)。</p>"
                       + credentials_form(csrf=auth.session_csrf(session) or ""))
        source_ids = ''.join(f"<li>{escape(item['id'])}</li>" for item in sources.list())
        bot_ids = ''.join(
            f"<li>{escape(item['id'])}{'' if item.get('username') is None else ' (' + escape(item['username']) + ')'}</li>"
            for item in bots.list())
        report = await health.check()
        health_html = " ".join(f"<span>{escape(str(k))}: {escape(str(v))}</span>" for k,v in report["checks"].items())
        event_page = events.page(offset=0, limit=1); audit_page = audit.page(offset=0, limit=1)
        recent = escape(str(event_page["items"][0])) if event_page["items"] else "none"
        audit_recent = escape(str(audit_page["items"][0])) if audit_page["items"] else "none"
        return HTMLResponse(render_dashboard(warning=warning, sources=source_ids, bots=bot_ids, health=health_html, event_total=event_page["total"], recent=recent, audit_total=audit_page["total"], audit_recent=audit_recent))

    return router


def render_dashboard(*, template_path: str | Path | None = None, **values: object) -> str:
    """Render the package template, with a safe built-in fallback."""
    try:
        path = Path(template_path) if template_path is not None else Path(__file__).parent.parent / "templates" / "admin_dashboard.html"
        template = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        template = "<html><body><h1>musicdl 管理后台</h1>{{warning}}<div>{{health}}</div><ul>{{sources}}</ul><ul>{{bots}}</ul><div>events={{event_total}} {{recent}}</div><div>audit={{audit_total}} {{audit_recent}}</div></body></html>"
    return template.replace("{{warning}}", str(values.get("warning", ""))).replace("{{sources}}", str(values.get("sources", ""))).replace("{{bots}}", str(values.get("bots", ""))).replace("{{health}}", str(values.get("health", ""))).replace("{{event_total}}", str(values.get("event_total", 0))).replace("{{recent}}", str(values.get("recent", "none"))).replace("{{audit_total}}", str(values.get("audit_total", 0))).replace("{{audit_recent}}", str(values.get("audit_recent", "none")))
