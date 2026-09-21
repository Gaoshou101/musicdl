from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _Secret:
    def __init__(self, value: str):
        self.value = value

    def get_secret_value(self) -> str:
        return self.value


def _settings():
    return SimpleNamespace(
        redis=SimpleNamespace(url=_Secret("redis://runtime-test/0"), connect_timeout=1.5, operation_timeout=2.5),
        media=SimpleNamespace(root="/data/music"),
        wecom=SimpleNamespace(enabled=True, corp_id="corp-test", secret=_Secret("wecom-secret"), agent_id=7,
                              api_base="http://proxy.example:9080", selection_ttl=321),
        plugin=SimpleNamespace(service_url="http://plugin:8080", app_data_root="/data/app"),
        ai=SimpleNamespace(enabled=True, api_key=_Secret("ai-key"), model="test-model", timeout=3.0, max_candidates=10),
        worker=SimpleNamespace(search_timeout=8.0, resolve_stream_timeout=15.0, health_timeout=5.0,
                               job_timeout=30.0, budget_slack_seconds=2.0, pending_idle_ms=32000,
                               job_ttl=172800, retry_window_seconds=86400, max_attempts=3),
    )


class _Plugin:
    def __init__(self, plugin_id="searcher", version="1", operations=("search",), enabled=True):
        self.manifest = SimpleNamespace(plugin_id=plugin_id, version=version, operations=operations)
        self.enabled = enabled
        self.path = "/data/app/plugins/plugin.py"


class _Redis:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.closed = args, kwargs, False

    async def aclose(self):
        self.closed = True

    @classmethod
    def from_url(cls, url, **kwargs):
        instance = cls(url, **kwargs)
        return instance


class _State:
    def __init__(self, redis):
        self.redis = redis
        self.namespace = "{musicdl}"


class _WeComService:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class _Store:
    plugins = ()

    def __init__(self, root):
        self.root = root

    def enabled(self):
        return tuple(self.plugins)


class _PluginClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.closed = args, kwargs, False
        self.__class__.instances.append(self)

    async def aclose(self):
        self.closed = True


class _PluginSource:
    def __init__(self, stored, client, transport=None, **options):
        self.stored, self.client, self.transport = stored, client, transport
        self.options = options

    async def search(self, query):
        return ()


class _Transport:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.closed = args, kwargs, False
        self.__class__.instances.append(self)

    async def aclose(self):
        self.closed = True


class _Worker:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.__class__.instances.append(self)


@pytest.fixture
def runtime_fakes(monkeypatch):
    import musicdl.app as app_module

    _PluginClient.instances.clear()
    _Worker.instances.clear()
    _Transport.instances.clear()
    _Store.plugins = ()
    monkeypatch.setattr(app_module, "Redis", _Redis, raising=False)
    monkeypatch.setattr(app_module, "RedisStateStore", _State, raising=False)
    monkeypatch.setattr(app_module, "WeComService", _WeComService, raising=False)
    monkeypatch.setattr(app_module, "PluginStore", _Store, raising=False)
    monkeypatch.setattr(app_module, "PluginClient", _PluginClient, raising=False)
    monkeypatch.setattr(app_module, "PluginSource", _PluginSource, raising=False)
    monkeypatch.setattr(app_module, "SecureMediaTransport", _Transport, raising=False)
    monkeypatch.setattr(app_module, "MessageWorker", type("MessageWorker", (_Worker,), {}), raising=False)
    monkeypatch.setattr(app_module, "JobWorker", type("JobWorker", (_Worker,), {}), raising=False)
    monkeypatch.setattr(app_module, "WeComClient", type("WeComClient", (_Worker,), {}), raising=False)
    monkeypatch.setattr(app_module, "OpenAICompatibleClient", type("OpenAICompatibleClient", (_Worker,), {}), raising=False)
    return app_module


def test_build_runtime_wires_wecom_secret_and_redis_settings(runtime_fakes):
    runtime = runtime_fakes._build_runtime(_settings())

    client = runtime.wecom
    assert client.args[:3] == ("corp-test", "wecom-secret", 7)
    assert client.args[3] is runtime.redis
    # Where the outbound calls go is the deployment's choice, not a constant.
    assert client.kwargs["base_url"] == "http://proxy.example:9080"
    assert runtime.redis.args[0] == "redis://runtime-test/0"
    assert runtime.redis.kwargs["socket_connect_timeout"] == 1.5
    assert runtime.redis.kwargs["socket_timeout"] == 2.5
    assert _Transport.instances == [runtime.transport]


def test_build_runtime_registers_only_enabled_search_plugins_and_shares_client(runtime_fakes):
    search = _Plugin("searcher", operations=("search",))
    disabled = _Plugin("disabled", operations=("search",), enabled=False)
    downloader = _Plugin("downloader", operations=("download",))
    _Store.plugins = (search, disabled, downloader)

    runtime = runtime_fakes._build_runtime(_settings())

    entries = runtime.registry.enabled()
    assert [entry.source_id for entry in entries] == ["searcher"]
    assert entries[0].source.stored is search
    assert entries[0].source.client is runtime.plugin_client
    assert entries[0].source.transport is runtime.transport
    assert entries[0].source.options == {"resolve_stream_timeout_ms": 15000, "health_timeout_ms": 5000}
    assert runtime.message_worker.kwargs["ai_ranker"] is not None
    assert runtime.message_worker.kwargs["selection_ttl"] == 321
    assert runtime.job_worker.kwargs["sources"] == {}


def test_build_runtime_rejects_duplicate_enabled_plugin_ids(runtime_fakes):
    _Store.plugins = (_Plugin("same", "1"), _Plugin("same", "2"))

    with pytest.raises(ValueError, match="duplicate_source_id"):
        runtime_fakes._build_runtime(_settings())


def test_build_runtime_exposes_refresh_callback_for_search_sources(runtime_fakes, monkeypatch):
    first, second = _Plugin("first"), _Plugin("second")
    _Store.plugins = (first, second)
    calls = []

    async def search_spy(registry, query, **options):
        calls.append((registry, query, options))
        return SimpleNamespace(candidates=())

    monkeypatch.setattr(runtime_fakes, "search_sources", search_spy, raising=False)
    runtime = runtime_fakes._build_runtime(_settings())

    refresh = runtime.job_worker.kwargs["refresh"]
    result = __import__("asyncio").run(refresh("needle", frozenset({"first"})))
    assert result.candidates == ()
    assert len(calls) == 1
    registry, query, options = calls[0]
    assert query == "needle"
    assert [entry.source_id for entry in registry.enabled()] == ["second"]
    assert options["timeout"] == runtime.message_worker.kwargs["search_timeout"]
    assert runtime.job_worker.kwargs["sources"] == {}


def test_build_runtime_maps_only_search_and_resolve_plugins_into_download_sources(runtime_fakes):
    resolver = _Plugin("resolver", operations=("search", "resolve"))
    search_only = _Plugin("searcher", operations=("search",))
    download_only = _Plugin("downloader", operations=("download", "resolve"))
    _Store.plugins = (resolver, search_only, download_only)

    runtime = runtime_fakes._build_runtime(_settings())

    sources = runtime.job_worker.kwargs["sources"]
    assert list(sources) == ["resolver"]
    assert sources["resolver"].stored is resolver
    assert sources["resolver"] is runtime.registry.enabled()[0].source
    assert sources["resolver"].transport is runtime.transport


def _source_definition(source_id, **overrides):
    """One definition as the administration portal stores it."""
    entry = {"id": source_id, "enabled": True, "priority": 0, "timeout": 10.0, "name": None}
    entry.update(overrides)
    return entry


def test_a_stored_definition_outranks_the_volume_it_was_published_from(runtime_fakes):
    _Store.plugins = (_Plugin("first"), _Plugin("second"))

    runtime = runtime_fakes._build_runtime(_settings(), sources=(
        _source_definition("first", enabled=False), _source_definition("second", priority=7)))

    entries = runtime.registry.enabled()
    assert [entry.source_id for entry in entries] == ["second"]
    assert entries[0].priority == 7


def test_a_definition_without_installed_code_registers_nothing(runtime_fakes):
    _Store.plugins = (_Plugin("installed"),)

    runtime = runtime_fakes._build_runtime(_settings(), sources=(
        _source_definition("installed"), _source_definition("removed")))

    assert [entry.source_id for entry in runtime.registry.enabled()] == ["installed"]


def test_a_disabled_definition_keeps_its_script_out_of_the_download_sources(runtime_fakes):
    _Store.plugins = (_Plugin("resolver", operations=("search", "resolve")),)

    runtime = runtime_fakes._build_runtime(_settings(), sources=(_source_definition("resolver", enabled=False),))

    assert runtime.registry.enabled() == ()


def test_build_runtime_shares_one_transport_and_client_across_every_source(runtime_fakes):
    first = _Plugin("first", operations=("search", "resolve"))
    second = _Plugin("second", operations=("search", "resolve"))
    _Store.plugins = (first, second)

    runtime = runtime_fakes._build_runtime(_settings())

    assert _PluginClient.instances == [runtime.plugin_client]
    assert _Transport.instances == [runtime.transport]
    for entry in runtime.registry.enabled():
        assert entry.source.client is runtime.plugin_client
        assert entry.source.transport is runtime.transport


def test_build_runtime_forwards_the_configured_worker_budgets(runtime_fakes):
    settings = _settings()
    settings.worker = SimpleNamespace(search_timeout=6.5, resolve_stream_timeout=12.0, health_timeout=4.0,
                                      job_timeout=26.0, budget_slack_seconds=2.0, pending_idle_ms=28000,
                                      job_ttl=100000, retry_window_seconds=50000, max_attempts=2)
    _Store.plugins = (_Plugin("resolver", operations=("search", "resolve")),)

    runtime = runtime_fakes._build_runtime(settings)

    assert runtime.message_worker.kwargs["search_timeout"] == 6.5
    assert runtime.job_worker.kwargs["media_root"] == "/data/music"
    assert runtime.job_worker.kwargs["sources"] != {}
    assert runtime.job_worker.kwargs["resolve_stream_timeout"] == 12.0
    assert runtime.job_worker.kwargs["refresh_timeout"] == 6.5
    assert runtime.job_worker.kwargs["health_timeout"] == 4.0
    assert runtime.job_worker.kwargs["job_timeout"] == 26.0
    assert runtime.job_worker.kwargs["pending_idle_ms"] == 28000
    assert runtime.job_worker.kwargs["job_ttl"] == 100000
    assert runtime.job_worker.kwargs["retry_window_seconds"] == 50000
    assert runtime.job_worker.kwargs["max_attempts"] == 2
    assert runtime.job_worker.kwargs["selection_ttl"] == 321
    assert runtime.job_worker.kwargs["refresh"] is not None
    assert runtime.job_worker.kwargs["state"] is runtime.state


def test_runtime_close_closes_transport_then_plugin_client_then_redis(runtime_fakes):
    runtime = runtime_fakes._build_runtime(_settings())
    events = []
    runtime.transport.aclose = lambda: events.append("transport")
    runtime.plugin_client.aclose = lambda: events.append("plugin")
    runtime.redis.aclose = lambda: events.append("redis")

    asyncio.run(runtime.aclose())

    assert events == ["transport", "plugin", "redis"]


def _telegram_settings(tmp_path, **overrides):
    values = dict(enabled=True, api_id=7, api_hash=_Secret("api-hash"), profile="default",
                  session_root=str(tmp_path / "sessions"), proxy=None)
    values.update(overrides)
    return SimpleNamespace(**values)


def _bot(bot_id="tg", username="MusicBot", **overrides):
    """One definition as the administration portal stores it."""
    entry = {"id": bot_id, "enabled": True, "priority": 0, "timeout": 10.0,
             "username": username, "command_template": None}
    entry.update(overrides)
    return entry


def test_a_disabled_telegram_deployment_wires_nothing(runtime_fakes, tmp_path):
    settings = _settings()
    settings.telegram = _telegram_settings(tmp_path, enabled=False)
    _Store.plugins = (_Plugin("searcher"),)

    runtime = runtime_fakes._build_runtime(settings, bots=(_bot(),))

    assert runtime.telegram is None and runtime.telegram_sources == 0
    assert [entry.source_id for entry in runtime.registry.enabled()] == ["searcher"]


def test_enabled_telegram_registers_one_source_per_enabled_definition(runtime_fakes, tmp_path):
    settings = _settings()
    settings.telegram = _telegram_settings(tmp_path)
    _Store.plugins = (_Plugin("searcher"),)
    definitions = (_bot("public", "MusicBot", priority=3),
                   _bot("custom", "MyBot", command_template="/get {query}", timeout=4.0),
                   _bot("off", "OffBot", enabled=False))

    runtime = runtime_fakes._build_runtime(settings, bots=definitions)

    entries = {entry.source_id: entry for entry in runtime.registry.enabled()}
    assert sorted(entries) == ["custom", "public", "searcher"]
    assert runtime.telegram_sources == 2
    assert runtime.telegram.root == (tmp_path / "sessions").resolve()

    public = entries["public"]
    assert (public.version, public.priority) == ("MusicBot", 3)
    assert public.source.command_template == "/search {query}"
    assert (public.source.bot_username, public.source.source_id) == ("MusicBot", "public")
    assert public.source.timeout == 10.0

    custom = entries["custom"]
    assert custom.source.command_template == "/get {query}" and custom.source.timeout == 4.0
    # One connector serves every definition, so the conversations with each Bot
    # go through the same flow object, and every definition is also a resolver:
    # the channel that listed a recording is the channel asked for the file,
    # which is the whole reason a Bot is a source here.
    assert public.source.flow is custom.source.flow
    assert sorted(runtime.resolvers) == ["custom", "public"]
    assert runtime.resolvers["public"] is public.source
    assert runtime.resolvers["custom"] is custom.source


def test_the_portal_does_not_get_a_second_toggle_for_a_telegram_bot(runtime_fakes, tmp_path):
    """A published bot would be a source entry an operator could flip for nothing."""
    settings = _settings()
    settings.telegram = _telegram_settings(tmp_path)
    _Store.plugins = (_Plugin("searcher"),)

    runtime = runtime_fakes._build_runtime(settings, bots=(_bot("tg"),))

    assert [entry.source_id for entry in runtime.plugin_registry.enabled()] == ["searcher"]
    assert [entry.source_id for entry in runtime.registry.enabled()] == ["searcher", "tg"]


def test_enabled_telegram_without_a_definition_registers_nothing(runtime_fakes, tmp_path):
    settings = _settings()
    settings.telegram = _telegram_settings(tmp_path)

    runtime = runtime_fakes._build_runtime(settings)

    assert runtime.telegram is not None and runtime.telegram_sources == 0


def test_a_bot_id_that_collides_with_a_plugin_id_is_rejected(runtime_fakes, tmp_path):
    settings = _settings()
    settings.telegram = _telegram_settings(tmp_path)
    _Store.plugins = (_Plugin("same"),)

    with pytest.raises(ValueError, match="duplicate_source_id"):
        runtime_fakes._build_runtime(settings, bots=(_bot("same"),))


def test_runtime_close_disconnects_the_telegram_connector_first(runtime_fakes, tmp_path):
    settings = _settings()
    settings.telegram = _telegram_settings(tmp_path)
    runtime = runtime_fakes._build_runtime(settings, bots=(_bot(),))
    events = []

    async def disconnect():
        events.append("telegram")

    runtime.telegram.disconnect = disconnect
    runtime.transport.aclose = lambda: events.append("transport")
    runtime.plugin_client.aclose = lambda: events.append("plugin")
    runtime.redis.aclose = lambda: events.append("redis")

    asyncio.run(runtime.aclose())

    assert events == ["telegram", "transport", "plugin", "redis"]


class _RestoringConnector:
    def __init__(self, status):
        self.status, self.profiles = status, []

    async def restore(self, profile):
        from musicdl.telegram.models import TelegramResult
        self.profiles.append(profile)
        return TelegramResult(self.status)


def _probe_state(runtime):
    return SimpleNamespace(state=SimpleNamespace(runtime=runtime))


async def _check(probes, name):
    """``_admin_probes`` hands back a mapping of probe name to coroutine factory."""
    return await probes[name]()


async def _check_telegram(probes):
    return await _check(probes, "telegram")


def test_the_telegram_probe_reports_every_state_honestly(runtime_fakes, tmp_path):
    from musicdl.telegram.models import TelegramStatus
    probes = runtime_fakes._admin_probes
    enabled = _settings()
    enabled.telegram = _telegram_settings(tmp_path)
    disabled = _settings()
    disabled.telegram = _telegram_settings(tmp_path, enabled=False)

    # A disabled deployment has nothing to check.
    offline = _probe_state(SimpleNamespace(telegram=None))
    assert asyncio.run(_check_telegram(probes(disabled, offline))) is None
    # Enabled but never wired, or wired with nothing defined: unhealthy, not fine.
    assert asyncio.run(_check_telegram(probes(enabled, offline))) is False
    defined = _probe_state(SimpleNamespace(telegram=_RestoringConnector(TelegramStatus.READY),
                                           telegram_sources=0))
    assert asyncio.run(_check_telegram(probes(enabled, defined))) is False

    for status, expected in ((TelegramStatus.READY, True),
                             (TelegramStatus.INVALID_SESSION, False),
                             (TelegramStatus.RATE_LIMITED, False),
                             (TelegramStatus.CODE_REQUIRED, False)):
        connector = _RestoringConnector(status)
        state = _probe_state(SimpleNamespace(telegram=connector, telegram_sources=1))
        assert asyncio.run(_check_telegram(probes(enabled, state))) is expected
        assert connector.profiles == ["default"]


def test_the_redis_probe_reports_not_required_where_no_wecom_state_is_held(runtime_fakes):
    probes = runtime_fakes._admin_probes

    class State:
        def __init__(self, alive):
            self.alive = alive

        async def ping(self):
            return self.alive

    def app(wecom_state):
        return SimpleNamespace(state=SimpleNamespace(runtime=None, wecom_state=wecom_state))

    # WeCom off: the runtime never opens a Redis client, so there is nothing to
    # report but the fact that this dependency is not part of the deployment.
    assert asyncio.run(_check(probes(_settings(), app(None)), "redis")) is None
    assert asyncio.run(_check(probes(_settings(), app(State(True))), "redis")) is True
    assert asyncio.run(_check(probes(_settings(), app(State(False))), "redis")) is False


def test_the_plugin_runner_probe_asks_the_client_the_runtime_holds(runtime_fakes):
    probes = runtime_fakes._admin_probes
    asked = []

    class Client:
        async def service_health(self):
            asked.append(True)
            return False

    def app(runtime):
        return SimpleNamespace(state=SimpleNamespace(runtime=runtime))

    assert asyncio.run(_check(probes(_settings(), app(None)), "plugin_runner")) is None
    assert asked == []
    unwired = SimpleNamespace(plugin_client=None)
    assert asyncio.run(_check(probes(_settings(), app(unwired)), "plugin_runner")) is None
    assert asked == []
    assert asyncio.run(_check(probes(_settings(), app(SimpleNamespace(plugin_client=Client()))),
                              "plugin_runner")) is False
    assert asked == [True]

def test_build_runtime_publishes_the_refresh_and_preference_the_panel_downloads_with(runtime_fakes, monkeypatch):
    """The panel needs the worker's refresh to retry on another channel.

    The same preference orders the retry, so a fallback lands on a channel the
    deployment has actually seen answer, and a runtime built without one keeps
    working with no opinion about any channel.
    """
    _Store.plugins = (_Plugin("first"), _Plugin("second"))
    calls = []

    async def search_spy(registry, query, **options):
        calls.append((query, options))
        return SimpleNamespace(candidates=())

    monkeypatch.setattr(runtime_fakes, "search_sources", search_spy, raising=False)
    preference = {"second": 10}.get
    runtime = runtime_fakes._build_runtime(_settings(), preference=preference)

    assert runtime.preference is preference
    assert runtime.refresh is runtime.job_worker.kwargs["refresh"]
    asyncio.run(runtime.refresh("needle", frozenset()))
    assert calls == [("needle", {"timeout": 8.0, "preference": preference})]

    default = runtime_fakes._build_runtime(_settings())
    assert default.preference is None and default.refresh is not None
