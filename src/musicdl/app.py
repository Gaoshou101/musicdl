from contextlib import asynccontextmanager
import asyncio
import inspect
import logging
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
from .sources.lx.analyzer import lx_shaped_file
from .sources.platform_search import LxSearchAdapter, PlatformSearch
from .worker.workers import MessageWorker, JobWorker
from .wecom.client import WeComClient
from .ai.client import OpenAICompatibleClient
from .ai.service import advise_language, advise_ranking
from .admin import (DEFAULT_BOTS, AdminAuth, AdminStateStore, AuditLogStore, BotManager,
                    ConfigManager, EventLogStore, HealthAggregator, LogBuffer, RateLimiter,
                    SourceHealthStore, SourceManager)
from .admin.csrf import CSRFMiddleware
from .admin.portal import create_admin_router
from .media import classify_language
from .telegram.connector import TelegramConnector, telethon_client_factory
from .telegram.models import TelegramStatus
from .telegram.source import TelegramBotSource

try:
    from redis.asyncio import Redis
except ImportError:  # pragma: no cover
    Redis = None


logger = logging.getLogger("musicdl.app")


async def _maybe_close(value: Any) -> None:
    closer = getattr(value, "aclose", None) or getattr(value, "close", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result


class _AdminState:
    """Mutable administration state mounted once per application instance."""

    def __init__(self, settings: AppSettings, app: FastAPI) -> None:
        self.settings = settings
        self.store = AdminStateStore(settings.admin.state_path)
        self.auth = AdminAuth(on_change=self.persist)
        self.sources = SourceManager(on_change=self.persist)
        self.bots = BotManager(on_change=self.persist)
        self.events = EventLogStore()
        self.audit = AuditLogStore()
        # One roll-up per process, fed by the panel's own search and download so
        # the channel health view has something to answer with from the first
        # search an operator runs.
        self.source_health = SourceHealthStore()
        # The panel's own layer of settings is read and applied before anything
        # captures a value from the settings object: the rate limiter below, and
        # the runtime the lifespan assembles on the next line of its own.
        self.config = ConfigManager(settings, on_change=self.persist, on_adopt=self.retune)
        self.limiter = RateLimiter(limit=settings.admin.login_limit,
                                   window_seconds=settings.admin.login_window_seconds)
        self.health = HealthAggregator(_admin_probes(settings, app))
        # The window onto what this process logs, created before the router is
        # mounted so it exists even when the stored configuration below fails
        # to load -- which is exactly when an operator needs to read it.
        self.logs = LogBuffer(capacity=500)
        self.load()
        app.include_router(create_admin_router(auth=self.auth, sources=self.sources, bots=self.bots,
                                               health=self.health, events=self.events,
                                               audit=self.audit, limiter=self.limiter,
                                               config=self.config,
                                               source_health=self.source_health,
                                               logs=self.logs,
                                               plugins=self.plugin_store,
                                               runtime=lambda: getattr(app.state, "runtime", None),
                                               reloader=lambda: getattr(app.state, "reload_runtime", None),
                                               media_root=settings.media.root,
                                               worker=settings.worker))
        app.add_middleware(CSRFMiddleware, auth=self.auth)

    def retune(self) -> None:
        """Adopt the settings the running process can change without a restart.

        Every other field is picked up by the next start, which is what the
        panel says on the field itself rather than leaving the operator to
        guess.
        """
        self.limiter.limit = self.settings.admin.login_limit
        self.limiter.window_seconds = self.settings.admin.login_window_seconds

    def plugin_store(self) -> PluginStore:
        """Storage handed to the portal on demand.

        Built per request rather than at start-up because constructing it
        creates the plugin directory, and a deployment that never installs a
        source should not need that volume to exist.
        """
        return PluginStore(self.settings.plugin.app_data_root)

    def load(self) -> None:
        """Adopt whatever the last run persisted; a first run has no file."""
        payload = self.store.load()
        if "bots" not in payload:
            # A deployment that has never stored a bot list starts from the
            # built-in definitions, so a fresh install can search through the
            # project's own bot without defining one. A stored list -- even the
            # empty one an operator leaves behind by deleting every entry --
            # wins, so removing the built-in bot is not undone by a restart.
            self.bots.restore(list(DEFAULT_BOTS))
        if not payload:
            return
        self.config.load(payload)
        self.auth.restore(payload.get("credentials"))
        self.sources.restore(payload.get("sources"))
        self.bots.restore(payload.get("bots"))

    def persist(self) -> None:
        self.store.save(credentials=self.auth.snapshot(), sources=self.sources.snapshot(),
                        bots=self.bots.snapshot(), settings=self.config.snapshot())

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
    """Dependency probes for the portal.

    An omitted key reports ``unavailable``.  A probe answering ``None`` reports
    ``not_required``: the dependency is real but this deployment has nothing
    for it to do, so its absence is not a fault to show the operator.
    """

    async def readyz() -> bool:
        tasks = getattr(app.state, "worker_tasks", None)
        if tasks is None:
            return True
        return app.state.worker_error is None and not any(task.done() for task in tasks)

    async def redis() -> bool | None:
        state = getattr(app.state, "wecom_state", None)
        if state is None:
            # Redis carries the WeCom sessions and the job queue.  With no
            # WeCom account the runtime never opens a client, so asking after
            # one would mark a working deployment as broken.
            return None
        return bool(await state.ping())

    async def plugin_runner() -> bool | None:
        # The runner is what actually fetches media, so its health is the one
        # an operator most wants to see; before a runtime exists there is
        # nothing to ask.
        client = getattr(getattr(app.state, "runtime", None), "plugin_client", None)
        if client is None:
            return None
        return await client.service_health()

    async def telegram() -> bool | None:
        # An enabled Telegram deployment is healthy only when a connector is
        # wired, at least one Bot definition is registered, and the account
        # session is authorized. A disabled deployment has nothing to check.
        configured = getattr(settings, "telegram", None)
        if not getattr(configured, "enabled", False):
            return None
        runtime = getattr(app.state, "runtime", None)
        connector = getattr(runtime, "telegram", None)
        if connector is None or not getattr(runtime, "telegram_sources", 0):
            return False
        result = await connector.restore(getattr(configured, "profile", "default"))
        return result.status is TelegramStatus.READY

    return {"readyz": readyz, "redis": redis, "plugin_runner": plugin_runner, "telegram": telegram}


class _Runtime:
    def __init__(self, *, redis, state, service, wecom, plugin_client, transport, registry,
                 message_worker, job_worker, telegram=None, plugin_registry=None, telegram_sources=0,
                 resolvers=None, refresh=None, preference=None):
        self.redis, self.state, self.service = redis, state, service
        self.wecom, self.plugin_client, self.registry = wecom, plugin_client, registry
        self.transport = transport
        self.message_worker, self.job_worker = message_worker, job_worker
        # Search and resolve are separate capabilities: an lx source resolves
        # media but never searches, so the two maps are not the same one.
        self.resolvers = dict(resolvers or {})
        # The panel's own download retries through the same refresh the
        # background worker uses, and orders equal candidates by what the
        # channel roll-up has observed. Both are optional: a runtime assembled
        # without them serves the panel exactly as it did before.
        self.refresh, self.preference = refresh, preference
        self.telegram, self.telegram_sources = telegram, telegram_sources
        # Only plugin sources are published to the portal. A Telegram bot is
        # configured as a bot, so a second copy under "sources" would be a
        # toggle an operator could flip without any effect.
        self.plugin_registry = plugin_registry if plugin_registry is not None else registry

    async def aclose(self) -> None:
        if self.telegram is not None:
            await self.telegram.disconnect()
        await _maybe_close(self.transport)
        await _maybe_close(self.plugin_client)
        await _maybe_close(self.redis)


def _telegram_sources(telegram_settings, bots, *, known_ids=frozenset(), factory=None):
    """Build the connector, one source per enabled Bot definition, its resolvers.

    The definitions are owned by the administration portal, so an enabled
    Telegram deployment with nothing defined registers nothing and the admin
    health probe reports it unhealthy instead of healthy-but-idle. The bot's
    username doubles as its source version, which is what the WeCom prompt shows
    as the version-identifying field and what separates two bots that return the
    same recording.

    Each definition becomes a whole source rather than a search-only adapter:
    the Bots this project is built around answer a search with a numbered list
    and only send the audio once a button is pressed, so the channel that
    listed a recording is the channel that has to fetch it.
    """
    api_hash = telegram_settings.api_hash
    secret = api_hash.get_secret_value() if hasattr(api_hash, "get_secret_value") else api_hash
    connector = TelegramConnector(
        telegram_settings.session_root, telegram_settings.api_id, secret,
        factory or telethon_client_factory(telegram_settings.api_id, secret,
                                           getattr(telegram_settings, "proxy", None)))
    flow = connector.bot_flow(telegram_settings.profile)
    known, definitions, resolvers = set(known_ids), [], {}
    for definition in bots:
        if not definition.get("enabled", True):
            continue
        source_id, username = definition["id"], definition["username"]
        if source_id in known:
            raise ValueError("duplicate_source_id")
        known.add(source_id)
        template = definition.get("command_template")
        adapter = TelegramBotSource(username, flow, source_id=source_id, source_version=username,
                                    command_template=template or "/search {query}",
                                    timeout=definition["timeout"])
        definitions.append(SourceEntry(source_id, username, adapter,
                                       priority=definition.get("priority", 0)))
        resolvers[source_id] = adapter
    return connector, definitions, resolvers


def _search_adapter(stored, source: PluginSource, platform_search: PlatformSearch):
    """Which half of an enabled plugin answers ``search``.

    An lx custom source answers ``musicUrl``, which is this project's
    ``resolve``.  Of the twelve sources this deployment was built around, none
    implements ``musicSearch`` (measured 2026-09-17 through the product's own
    path: 12/12 reported ``search_failed`` while the same twelve resolved 27-30
    of 48 platform cells), so the main process searches the platform catalogue
    on the source's behalf and labels every hit with this source's identity.
    The source still answers the part it is good at, and gains no egress: the
    search endpoints are main-process constants, not manifest fields.

    A plugin that does implement search -- anything the installer did not
    accept as an lx custom source -- keeps its own adapter.
    """
    manifest = getattr(stored, "manifest", None)
    if getattr(manifest, "language", None) != "javascript":
        return source
    if not lx_shaped_file(getattr(stored, "path", "")):
        return source
    return LxSearchAdapter(manifest.plugin_id, manifest.version, platform_search)


def _build_runtime(settings: AppSettings, clock=None, *, bots=(), sources=(),
                   telegram_client_factory=None, preference=None):
    """Build one runtime.

    ``bots`` and ``sources`` are the definitions the portal owns.  A stored
    source definition outranks the volume's own enable flag and priority,
    because the portal is the operator's control plane and a toggle that the
    runtime ignored would be a lie.  A definition without installed code
    registers nothing: an id nobody can search is not a source.
    """
    worker_settings = settings.worker
    store = PluginStore(settings.plugin.app_data_root)
    stored = tuple(store.enabled())
    search_plugins = [p for p in stored if getattr(p, "enabled", True) and "search" in p.manifest.operations]
    ids = [p.manifest.plugin_id for p in search_plugins]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_source_id")
    overrides = {item["id"]: item for item in sources
                 if isinstance(item, dict) and isinstance(item.get("id"), str)}
    # Only the WeCom workers need Redis and the WeCom client.  The registry
    # below answers the panel's own search and download without either, so a
    # deployment that has no WeCom account still gets a working runtime.
    redis = state = service = wecom = None
    if settings.wecom.enabled:
        if Redis is None:  # pragma: no cover
            raise RuntimeError("redis dependency is unavailable")
        redis = Redis.from_url(settings.redis.url.get_secret_value(), decode_responses=False,
                               socket_connect_timeout=settings.redis.connect_timeout,
                               socket_timeout=settings.redis.operation_timeout)
        state = RedisStateStore(redis)
        service = WeComService(settings.wecom, state, clock or time.time)
        wecom = WeComClient(settings.wecom.corp_id, settings.wecom.secret.get_secret_value(),
                            settings.wecom.agent_id, redis,
                            base_url=str(settings.wecom.api_base))
    plugin_client = PluginClient(str(settings.plugin.service_url), broker=HttpsActionBroker())
    transport = SecureMediaTransport()
    # Every installed lx source resolves against the same four catalogues, so
    # one main-process search client answers all of them: twelve sources cost
    # four upstream requests per query, not forty-eight.
    platform_search = PlatformSearch(timeout=worker_settings.search_timeout)
    entries = []
    resolvers: dict[str, PluginSource] = {}
    for p in search_plugins:
        definition = overrides.get(p.manifest.plugin_id, {})
        source = PluginSource(p, plugin_client, transport,
                              resolve_stream_timeout_ms=math.ceil(worker_settings.resolve_stream_timeout * 1000),
                              health_timeout_ms=math.ceil(worker_settings.health_timeout * 1000))
        entries.append(SourceEntry(p.manifest.plugin_id, p.manifest.version,
                                   _search_adapter(p, source, platform_search),
                                   enabled=definition.get("enabled", True),
                                   priority=definition.get("priority", 0)))
        if "resolve" in p.manifest.operations:
            resolvers[p.manifest.plugin_id] = source
    plugin_registry = SourceRegistry(entries)
    search_timeout = worker_settings.search_timeout
    ai_client = OpenAICompatibleClient(settings.ai)
    telegram = None
    telegram_sources = 0
    telegram_settings = getattr(settings, "telegram", None)
    if getattr(telegram_settings, "enabled", False):
        telegram, definitions, bot_resolvers = _telegram_sources(
            telegram_settings, bots, known_ids={entry.source_id for entry in entries},
            factory=telegram_client_factory)
        entries.extend(definitions)
        resolvers.update(bot_resolvers)
        telegram_sources = len(definitions)
    registry = SourceRegistry(entries)

    async def ranker(result, query):
        return await advise_ranking(result, query, settings.ai, client=ai_client)

    async def language_advisor(candidate):
        """Advisory classification on top of the deterministic verdict."""
        advice = await advise_language(candidate,
                                       classify_language(candidate.title, candidate.artist, candidate.album),
                                       settings.ai, client=ai_client)
        return advice.language

    async def refresh(query: str, failed_source_ids=frozenset()):
        """Re-search everything but the channel that just failed.

        The panel's download uses this too, so it is ordered by the same
        preference the panel's own search is: a retry should reach the channel
        that has been answering, not whichever one sorts first.
        """
        excluded = set(failed_source_ids or ())
        return await search_sources(SourceRegistry(e for e in entries if e.source_id not in excluded),
                                     query, timeout=search_timeout, preference=preference)

    message_worker = job_worker = None
    if settings.wecom.enabled:
        message_worker = MessageWorker(redis, registry, wecom, state=state, ai_ranker=ranker,
                                       search_timeout=search_timeout,
                                       selection_ttl=settings.wecom.selection_ttl)
        job_worker = JobWorker(redis, wecom, sources=resolvers, media_root=settings.media.root, state=state,
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
                    message_worker=message_worker, job_worker=job_worker,
                    telegram=telegram, plugin_registry=plugin_registry,
                    telegram_sources=telegram_sources, resolvers=resolvers,
                    refresh=refresh, preference=preference)


def create_app(settings: AppSettings | None = None, state_factory: Callable[[Any], Any] | None = None,
               clock=None, runtime_factory: Callable[[Any], Any] | None = None) -> FastAPI:
    settings = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.worker_error = None
        app.state.stopping = False
        app.state.runtime_error = None
        app.state.runtime_generation = 0
        # One reload at a time: a save that arrives while another is still
        # swapping runtimes waits for it instead of building on top of a
        # half-stopped one.
        reload_lock = asyncio.Lock()
        live: dict[str, Any] = {"runtime": None, "tasks": []}
        if admin is not None:
            # Installed before anything else the lifespan does, so a start-up
            # that goes wrong is itself readable from the panel afterwards.
            app.state.logs = admin.logs
            admin.logs.install()

        def worker_finished(task: asyncio.Task) -> None:
            if app.state.stopping or task.cancelled():
                return
            try:
                error = task.exception()
            except asyncio.CancelledError:
                return
            app.state.worker_error = error or RuntimeError("worker exited")

        async def assemble() -> Any:
            """Build one runtime from whatever the settings say right now.

            The portal owns the bot and source definitions, so the next runtime
            is assembled from the stored list rather than from the copy the
            running one closed over: a source installed a moment ago is part of
            the runtime built a moment later.
            """
            definitions = tuple(admin.bots.list()) if admin is not None else ()
            source_definitions = tuple(admin.sources.list()) if admin is not None else ()
            # The panel's own search and download are ordered by what it has
            # observed about each channel; the workers search through the same
            # registry, so they inherit the same ordering.
            preference = None if admin is None else admin.source_health.preference
            factory = runtime_factory or (
                lambda current: _build_runtime(current, clock, bots=definitions,
                                               sources=source_definitions,
                                               preference=preference))
            built = factory(settings)
            if inspect.isawaitable(built):
                built = await built
            return built

        async def observe(worker) -> None:
            await worker.run_forever()

        def start_workers(instance) -> list[asyncio.Task]:
            """Run one runtime's workers, or none when it has no message half."""
            if instance is None or instance.message_worker is None or instance.job_worker is None:
                return []
            app.state.message_worker = instance.message_worker
            app.state.job_worker = instance.job_worker
            started = [asyncio.create_task(observe(instance.message_worker)),
                       asyncio.create_task(observe(instance.job_worker))]
            for task in started:
                task.add_done_callback(worker_finished)
            return started

        async def stop_workers(tasks) -> None:
            """Cancel the running workers and wait for them to let go.

            A worker cancelled mid-message leaves it pending in its Redis
            stream, which is where the next runtime's ``XAUTOCLAIM`` finds it:
            a reload costs a retry, never a message.
            """
            if not tasks:
                return
            previous = app.state.stopping
            app.state.stopping = True
            try:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                app.state.stopping = previous

        def adopt(instance, tasks) -> None:
            """Point the application at the runtime that is now in force."""
            live["runtime"], live["tasks"] = instance, tasks
            app.state.runtime = instance
            if tasks:
                app.state.worker_tasks = tasks
            elif hasattr(app.state, "worker_tasks"):
                # A runtime with no message half must not leave the previous
                # one's finished tasks behind: /readyz reads this list.
                del app.state.worker_tasks
            if instance is None:
                return
            if instance.state is not None:
                app.state.wecom_state = instance.state
                app.state.wecom_service = instance.service
                app.state.service = instance.service
            if admin is not None:
                admin.publish_sources(getattr(instance, "plugin_registry", None)
                                      or getattr(instance, "registry", None))

        async def reload_runtime(reason: str = "manual") -> dict[str, Any]:
            """Adopt the settings in force without restarting the process.

            The next runtime is built before the running one is touched, so a
            build that fails leaves a working deployment alone and the operator
            reads the reason in the panel instead of meeting a dead service.
            """
            if state_factory is not None:
                return {"status": "skipped", "reason": "injected state boundary"}
            if admin is None and not settings.wecom.enabled:
                return {"status": "skipped", "reason": "no runtime is assembled"}
            async with reload_lock:
                previous, tasks = live["runtime"], live["tasks"]
                try:
                    built = await assemble()
                except Exception as error:  # noqa: BLE001 - reported, not raised
                    logger.warning("runtime reload failed: %s", error, exc_info=True)
                    return {"status": "failed", "error": str(error)}
                await stop_workers(tasks)
                started = start_workers(built)
                adopt(built, started)
                if previous is not None:
                    await _maybe_close(previous)
                app.state.runtime_error = None
                app.state.runtime_generation += 1
                registry = getattr(built, "registry", None)
                sources = len(registry.enabled()) if registry is not None else 0
                logger.info("runtime reloaded (%s): generation %d, %d sources",
                            reason, app.state.runtime_generation, sources)
                return {"status": "reloaded", "reason": reason,
                        "generation": app.state.runtime_generation, "sources": sources,
                        "workers": len(started)}

        # Set before the first assembly, so a start-up that fails to build a
        # runtime still leaves the panel able to ask for another one.
        app.state.reload_runtime = reload_runtime

        if settings.wecom.enabled and state_factory:
            boundary = state_factory(settings)
            if inspect.isawaitable(boundary):
                boundary = await boundary
            app.state.wecom_state = boundary
            app.state.wecom_service = WeComService(settings.wecom, boundary, clock or time.time)
        elif state_factory:
            # An injected state boundary means the caller owns the WeCom half;
            # assembling the source runtime on top of it is not what it asked
            # for, and nothing here needs the plugin volume then.
            pass
        elif settings.wecom.enabled or admin is not None:
            # The one registry answers both surfaces -- the WeCom workers and
            # the panel's own search box -- so it is assembled whenever either
            # is on.  A deployment that runs only the panel must not be left
            # with nothing to search, and one that runs neither is not asked
            # for plugin storage it will never read.
            try:
                built = await assemble()
            except Exception as error:
                # A WeCom deployment cannot serve its callback without workers;
                # the panel can still start and report what is missing.
                if settings.wecom.enabled:
                    raise
                app.state.runtime_error = error
            else:
                adopt(built, start_workers(built))
        try:
            yield
        finally:
            app.state.stopping = True
            await stop_workers(live["tasks"])
            live["tasks"] = []
            if live["runtime"] is not None:
                await _maybe_close(live["runtime"])
                live["runtime"] = None
            if admin is not None:
                admin.logs.uninstall()

    app = FastAPI(title="musicdl", docs_url=None, redoc_url=None, lifespan=lifespan)
    admin = _AdminState(settings, app) if settings.admin.enabled else None
    app.state.admin = admin
    # Read as plain state by /readyz, which must answer even when no lifespan
    # ever ran: a probe should never be the thing that raises.
    app.state.runtime_error = None

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"service": "musicdl", "status": "ok", "config_version": settings.config.version}

    @app.get("/readyz")
    async def readyz() -> Response:
        if not settings.wecom.enabled:
            # Without WeCom there are no workers to lose, but a panel whose
            # source runtime could not be assembled has nothing to search.
            if app.state.runtime_error is not None:
                return PlainTextResponse("not ready", status_code=503)
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
        service = getattr(app.state, "wecom_service", None)
        if service is None:
            # Enabling the boundary is a restart-scoped change: the panel writes
            # the setting at once, while the service that answers is built at
            # start-up. Say so instead of raising at the WeCom console.
            return PlainTextResponse("restart required", status_code=503)
        try:
            value = await service.verify_get(request)
        except ValueError:
            return PlainTextResponse("bad request", status_code=400)
        return PlainTextResponse(value)

    @app.post("/wecom/callback")
    async def wecom_post(request: Request) -> Response:
        if not settings.wecom.enabled:
            return PlainTextResponse("disabled", status_code=503)
        service = getattr(app.state, "wecom_service", None)
        if service is None:
            return PlainTextResponse("restart required", status_code=503)
        try:
            await service.handle_post(request)
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
