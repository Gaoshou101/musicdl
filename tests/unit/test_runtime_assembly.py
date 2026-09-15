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
        wecom=SimpleNamespace(enabled=True, corp_id="corp-test", secret=_Secret("wecom-secret"), agent_id=7, selection_ttl=321),
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
