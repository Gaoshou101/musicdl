from contextlib import asynccontextmanager
import asyncio
import inspect
import math
from typing import Any, Callable
import time

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from .config import AppSettings
from .wecom.service import WeComService
from .wecom.state import RedisStateStore, StateUnavailable
from .plugins import PluginClient, PluginSource, PluginStore
from .plugins.broker import HttpsActionBroker
from .media.transport import SecureMediaTransport
from .sources import SourceEntry, SourceRegistry, search_sources
from .worker.workers import MessageWorker, JobWorker
from .wecom.client import WeComClient
from .ai.client import OpenAICompatibleClient
from .ai.service import advise_language, advise_ranking
from .admin import (AdminAuth, AdminStateStore, AuditLogStore, BotManager, EventLogStore,
                    HealthAggregator, RateLimiter, SourceManager)
from .admin.csrf import CSRFMiddleware
from .admin.portal import create_admin_router
from .media import classify_language

try:
    from redis.asyncio import Redis
except ImportError:  # pragma: no cover
    Redis = None


async def _maybe_close(value: Any) -> None:
    closer = getattr(value, "aclose", None) or getattr(value, "close", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result


class _AdminState:
    """Mutable administration state mounted once per application instance."""

    def __init__(self, settings: AppSettings, app: FastAPI) -> None:
        self.store = AdminStateStore(settings.admin.state_path)
        self.auth = AdminAuth(on_change=self.persist)
        self.sources = SourceManager(on_change=self.persist)
        self.bots = BotManager(on_change=self.persist)
        self.events = EventLogStore()
        self.audit = AuditLogStore()
        self.limiter = RateLimiter(limit=settings.admin.login_limit,
                                   window_seconds=settings.admin.login_window_seconds)
        self.health = HealthAggregator(_admin_probes(settings, app))
        self.load()
        app.include_router(create_admin_router(auth=self.auth, sources=self.sources, bots=self.bots,
                                               health=self.health, events=self.events,
                                               audit=self.audit, limiter=self.limiter))
        app.add_middleware(CSRFMiddleware, auth=self.auth)

    def load(self) -> None:
        """Adopt whatever the last run persisted; a first run has no file."""
        payload = self.store.load()
        if not payload:
            return
        self.auth.restore(payload.get("credentials"))
        self.sources.restore(payload.get("sources"))
        self.bots.restore(payload.get("bots"))

    def persist(self) -> None:
        self.store.save(credentials=self.auth.snapshot(), sources=self.sources.snapshot(),
                        bots=self.bots.snapshot())

    def publish_sources(self, registry: Any) -> None:
        """Expose the sources the runtime actually assembled.

        An id the operator already changed survives the restart and keeps
        precedence, so publishing never rewrites a stored choice.
        """
        for entry in registry.enabled() if registry is not None else ():
            try:
                self.sources.register({"id": entry.source_id, "enabled": entry.enabled,
                                       "priority": entry.priority})
            except ValueError:
                continue


def _admin_probes(settings: AppSettings, app: FastAPI) -> dict[str, Any]:
    """Dependency probes for the portal; an omitted key reports unavailable."""

    async def readyz() -> bool:
        tasks = getattr(app.state, "worker_tasks", None)
        if tasks is None:
            return True
        return app.state.worker_error is None and not any(task.done() for task in tasks)

    async def redis() -> bool:
        state = getattr(app.state, "wecom_state", None)
        if state is None:
            raise RuntimeError("state unavailable")
        return bool(await state.ping())

    async def telegram() -> bool:
        # The connector is not wired into the runtime yet, so an enabled
        # Telegram deployment is genuinely unhealthy while a disabled one has
        # nothing for this deployment to check.
        return not settings.telegram.enabled

    return {"readyz": readyz, "redis": redis, "telegram": telegram}


class _Runtime:
    def __init__(self, *, redis, state, service, wecom, plugin_client, transport, registry,
                 message_worker, job_worker):
        self.redis, self.state, self.service = redis, state, service
        self.wecom, self.plugin_client, self.registry = wecom, plugin_client, registry
        self.transport = transport
        self.message_worker, self.job_worker = message_worker, job_worker

    async def aclose(self) -> None:
        await _maybe_close(self.transport)
        await _maybe_close(self.plugin_client)
        await _maybe_close(self.redis)


def _build_runtime(settings: AppSettings, clock=None):
    worker_settings = settings.worker
    store = PluginStore(settings.plugin.app_data_root)
    stored = tuple(store.enabled())
    search_plugins = [p for p in stored if getattr(p, "enabled", True) and "search" in p.manifest.operations]
    ids = [p.manifest.plugin_id for p in search_plugins]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_source_id")
    if Redis is None:  # pragma: no cover
        raise RuntimeError("redis dependency is unavailable")
    redis = Redis.from_url(settings.redis.url.get_secret_value(), decode_responses=False,
                           socket_connect_timeout=settings.redis.connect_timeout,
                           socket_timeout=settings.redis.operation_timeout)
    state = RedisStateStore(redis)
    service = WeComService(settings.wecom, state, clock or time.time)
    wecom = WeComClient(settings.wecom.corp_id, settings.wecom.secret.get_secret_value(),
                        settings.wecom.agent_id, redis)
    plugin_client = PluginClient(str(settings.plugin.service_url), broker=HttpsActionBroker())
    transport = SecureMediaTransport()
    entries = []
    sources: dict[str, PluginSource] = {}
    for p in search_plugins:
        source = PluginSource(p, plugin_client, transport,
                              resolve_stream_timeout_ms=math.ceil(worker_settings.resolve_stream_timeout * 1000),
                              health_timeout_ms=math.ceil(worker_settings.health_timeout * 1000))
        entries.append(SourceEntry(p.manifest.plugin_id, p.manifest.version, source))
        if "resolve" in p.manifest.operations:
            sources[p.manifest.plugin_id] = source
    registry = SourceRegistry(entries)
    search_timeout = worker_settings.search_timeout
    ai_client = OpenAICompatibleClient(settings.ai)

    async def ranker(result, query):
        return await advise_ranking(result, query, settings.ai, client=ai_client)

    async def language_advisor(candidate):
        """Advisory classification on top of the deterministic verdict."""
        advice = await advise_language(candidate,
                                       classify_language(candidate.title, candidate.artist, candidate.album),
                                       settings.ai, client=ai_client)
        return advice.language

    async def refresh(query: str, failed_source_ids=frozenset()):
        excluded = set(failed_source_ids or ())
        return await search_sources(SourceRegistry(e for e in entries if e.source_id not in excluded),
                                     query, timeout=search_timeout)

    message_worker = MessageWorker(redis, registry, wecom, state=state, ai_ranker=ranker,
                                   search_timeout=search_timeout,
                                   selection_ttl=settings.wecom.selection_ttl)
    job_worker = JobWorker(redis, wecom, sources=sources, media_root=settings.media.root, state=state,
                           refresh=refresh, job_timeout=worker_settings.job_timeout,
                           language_advisor=language_advisor,
                           resolve_stream_timeout=worker_settings.resolve_stream_timeout,
                           refresh_timeout=worker_settings.search_timeout,
                           health_timeout=worker_settings.health_timeout,
                           pending_idle_ms=worker_settings.pending_idle_ms,
                           job_ttl=worker_settings.job_ttl,
                           retry_window_seconds=worker_settings.retry_window_seconds,
                           max_attempts=worker_settings.max_attempts,
                           selection_ttl=settings.wecom.selection_ttl)
    return _Runtime(redis=redis, state=state, service=service, wecom=wecom,
                    plugin_client=plugin_client, transport=transport, registry=registry,
                    message_worker=message_worker, job_worker=job_worker)


def create_app(settings: AppSettings | None = None, state_factory: Callable[[Any], Any] | None = None,
               clock=None, runtime_factory: Callable[[Any], Any] | None = None) -> FastAPI:
    settings = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = None
        redis = None
        runtime = None
        tasks: list[asyncio.Task] = []
        app.state.worker_error = None
        app.state.stopping = False
        if settings.wecom.enabled:
            if state_factory:
                state = state_factory(settings)
                if inspect.isawaitable(state):
                    state = await state
                app.state.wecom_state = state
                app.state.wecom_service = WeComService(settings.wecom, state, clock or time.time)
            else:
                factory = runtime_factory or (lambda current: _build_runtime(current, clock))
                runtime = factory(settings)
                if inspect.isawaitable(runtime):
                    runtime = await runtime
                app.state.runtime = runtime
                state = runtime.state
                app.state.wecom_state = state
                app.state.wecom_service = runtime.service
                app.state.service = runtime.service
                app.state.message_worker = runtime.message_worker
                app.state.job_worker = runtime.job_worker
                if admin is not None:
                    admin.publish_sources(getattr(runtime, "registry", None))
                async def observe(worker):
                    await worker.run_forever()

                def completed(task: asyncio.Task) -> None:
                    if app.state.stopping or task.cancelled():
                        return
                    try:
                        error = task.exception()
                    except asyncio.CancelledError:
                        return
                    app.state.worker_error = error or RuntimeError("worker exited")

                tasks = [asyncio.create_task(observe(runtime.message_worker)),
                         asyncio.create_task(observe(runtime.job_worker))]
                for task in tasks:
                    task.add_done_callback(completed)
                app.state.worker_tasks = tasks
        try:
            yield
        finally:
            app.state.stopping = True
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if runtime is not None:
                await _maybe_close(runtime)
            elif redis is not None:
                await _maybe_close(redis)

    app = FastAPI(title="musicdl", docs_url=None, redoc_url=None, lifespan=lifespan)
    admin = _AdminState(settings, app) if settings.admin.enabled else None
    app.state.admin = admin

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"service": "musicdl", "status": "ok", "config_version": settings.config.version}

    @app.get("/readyz")
    async def readyz() -> Response:
        if not settings.wecom.enabled:
            return PlainTextResponse("ready")
        try:
            if not await app.state.wecom_state.ping():
                return PlainTextResponse("not ready", status_code=503)
        except (AttributeError, StateUnavailable):
            return PlainTextResponse("not ready", status_code=503)
        if hasattr(app.state, "worker_tasks") and (
            app.state.worker_error is not None or any(task.done() for task in app.state.worker_tasks)
        ):
            return PlainTextResponse("not ready", status_code=503)
        return PlainTextResponse("ready")

    @app.get("/wecom/callback", response_class=PlainTextResponse)
    async def wecom_get(request: Request) -> PlainTextResponse:
        if not settings.wecom.enabled:
            return PlainTextResponse("disabled", status_code=503)
        try:
            value = await app.state.wecom_service.verify_get(request)
        except ValueError:
            return PlainTextResponse("bad request", status_code=400)
        return PlainTextResponse(value)

    @app.post("/wecom/callback")
    async def wecom_post(request: Request) -> Response:
        if not settings.wecom.enabled:
            return PlainTextResponse("disabled", status_code=503)
        try:
            await app.state.wecom_service.handle_post(request)
        except ValueError as exc:
            if str(exc) == "unsupported_encoding":
                return PlainTextResponse("unsupported media", status_code=415)
            if str(exc) == "body_too_large":
                return PlainTextResponse("payload too large", status_code=413)
            return PlainTextResponse("bad request", status_code=400)
        except PermissionError:
            return PlainTextResponse("forbidden", status_code=403)
        except StateUnavailable:
            return PlainTextResponse("not ready", status_code=503)
        return Response(status_code=200)

    return app


app = create_app()
