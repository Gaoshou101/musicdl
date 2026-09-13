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
    def __init__(self, stored, client):
        self.stored, self.client = stored, client

    async def search(self, query):
        return ()


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
    _Store.plugins = ()
    monkeypatch.setattr(app_module, "Redis", _Redis, raising=False)
    monkeypatch.setattr(app_module, "RedisStateStore", _State, raising=False)
    monkeypatch.setattr(app_module, "WeComService", _WeComService, raising=False)
    monkeypatch.setattr(app_module, "PluginStore", _Store, raising=False)
    monkeypatch.setattr(app_module, "PluginClient", _PluginClient, raising=False)
    monkeypatch.setattr(app_module, "PluginSource", _PluginSource, raising=False)
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


def test_runtime_close_closes_plugin_client_before_redis(runtime_fakes):
    runtime = runtime_fakes._build_runtime(_settings())
    events = []
    runtime.plugin_client.aclose = lambda: events.append("plugin")
    runtime.redis.aclose = lambda: events.append("redis")

    asyncio.run(runtime.aclose())

    assert events == ["plugin", "redis"]
