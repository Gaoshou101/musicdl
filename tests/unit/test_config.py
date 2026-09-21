import math

import pytest
from pydantic import ValidationError

from musicdl.config import (AISettings, AppSettings, REDIS_OVERHEAD_SECONDS, WECOM_NOTICE_TIMEOUT_SECONDS,
                            WorkerSettings)


def test_settings_map_prefixed_nested_environment(monkeypatch):
    monkeypatch.setenv("MUSICDL_CONFIG__VERSION", "1")
    monkeypatch.setenv("MUSICDL_REDIS__URL", "redis://:secret@example.test/0")
    monkeypatch.setenv("MUSICDL_MEDIA__ROOT", "/data/music")
    monkeypatch.setenv("MUSICDL_TELEGRAM__SESSION_ROOT", "/data/sessions")
    monkeypatch.setenv("MUSICDL_PLUGIN__SERVICE_URL", "http://plugin:8080")

    settings = AppSettings()

    assert settings.config.version == 1
    assert settings.redis.url.get_secret_value() == "redis://:secret@example.test/0"
    assert settings.media.root == "/data/music"
    assert settings.telegram.session_root == "/data/sessions"
    assert str(settings.plugin.service_url) == "http://plugin:8080/"
    assert settings.plugin.app_data_root == "/data/app"


def test_plugin_app_data_root_maps_environment_and_is_not_relative(monkeypatch):
    monkeypatch.setenv("MUSICDL_PLUGIN__APP_DATA_ROOT", "/srv/musicdl/plugin-data")
    settings = AppSettings()
    assert settings.plugin.app_data_root == "/srv/musicdl/plugin-data"


def test_plugin_app_data_root_rejects_relative_path(monkeypatch):
    monkeypatch.setenv("MUSICDL_PLUGIN__APP_DATA_ROOT", "plugin-data")
    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize(
    ("media", "session", "plugin"),
    [
        ("/data/app", "/data/sessions", "/data/app"),
        ("/data/music", "/data/app", "/data/app/cache"),
        ("/data/music", "/data/sessions", "/data/music/plugin"),
    ],
)
def test_all_data_roots_must_be_pairwise_disjoint(monkeypatch, media, session, plugin):
    monkeypatch.setenv("MUSICDL_MEDIA__ROOT", media)
    monkeypatch.setenv("MUSICDL_TELEGRAM__SESSION_ROOT", session)
    monkeypatch.setenv("MUSICDL_PLUGIN__APP_DATA_ROOT", plugin)
    with pytest.raises(ValidationError):
        AppSettings()


def test_root_data_directory_overlaps_every_other_root(monkeypatch):
    monkeypatch.setenv("MUSICDL_MEDIA__ROOT", "/data/music")
    monkeypatch.setenv("MUSICDL_TELEGRAM__SESSION_ROOT", "/data/sessions")
    monkeypatch.setenv("MUSICDL_PLUGIN__APP_DATA_ROOT", "/")
    with pytest.raises(ValidationError):
        AppSettings()


def test_settings_rejects_unsupported_config_version(monkeypatch):
    monkeypatch.setenv("MUSICDL_CONFIG__VERSION", "2")
    with pytest.raises(ValidationError):
        AppSettings()


def test_media_and_telegram_session_roots_must_differ(monkeypatch):
    monkeypatch.setenv("MUSICDL_MEDIA__ROOT", "/same")
    monkeypatch.setenv("MUSICDL_TELEGRAM__SESSION_ROOT", "/same")
    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize(
    ("media", "session"),
    [("/data", "/data/sessions"), ("/data/media", "/data"), ("relative/media", "/data/sessions")],
)
def test_media_and_session_roots_must_be_absolute_and_disjoint(monkeypatch, media, session):
    monkeypatch.setenv("MUSICDL_MEDIA__ROOT", media)
    monkeypatch.setenv("MUSICDL_TELEGRAM__SESSION_ROOT", session)
    with pytest.raises(ValidationError):
        AppSettings()


def test_plugin_service_url_must_use_http_or_https(monkeypatch):
    monkeypatch.setenv("MUSICDL_PLUGIN__SERVICE_URL", "ftp://plugin:21")
    with pytest.raises(ValidationError):
        AppSettings()


def test_redis_url_must_use_redis_scheme(monkeypatch):
    monkeypatch.setenv("MUSICDL_REDIS__URL", "http://redis:6379/0")
    with pytest.raises(ValidationError):
        AppSettings()


def test_settings_repr_and_dump_do_not_expose_secret(monkeypatch):
    monkeypatch.setenv("MUSICDL_REDIS__URL", "redis://:top-secret@example.test/0")
    settings = AppSettings()
    assert "top-secret" not in repr(settings)
    assert "top-secret" not in str(settings.model_dump())


def test_telegram_enabled_requires_credentials_and_normalizes_profile(monkeypatch):
    monkeypatch.setenv("MUSICDL_TELEGRAM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_TELEGRAM__API_ID", "123")
    monkeypatch.setenv("MUSICDL_TELEGRAM__API_HASH", "  secret-hash  ")
    monkeypatch.setenv("MUSICDL_TELEGRAM__PROFILE", "  alice  ")
    settings = AppSettings()
    assert settings.telegram.profile == "alice"
    assert "secret-hash" not in repr(settings.telegram)


def test_telegram_enabled_rejects_missing_credentials(monkeypatch):
    monkeypatch.setenv("MUSICDL_TELEGRAM__ENABLED", "true")
    with pytest.raises(ValidationError):
        AppSettings()

def test_telegram_proxy_configuration(monkeypatch):
    monkeypatch.setenv("MUSICDL_TELEGRAM__PROXY", " socks5://127.0.0.1:1080 ")
    settings = AppSettings()
    assert settings.telegram.proxy == "socks5://127.0.0.1:1080"


def test_ai_is_disabled_by_default():
    settings = AppSettings().ai
    assert settings.enabled is False
    assert str(settings.base_url) == "https://api.openai.com/v1"
    assert settings.timeout == 10.0
    assert settings.max_candidates == 20
    # Unset means the HTTP client's own agent, which is what most endpoints want.
    assert settings.user_agent is None


def test_enabled_ai_requires_and_normalizes_credentials(monkeypatch):
    monkeypatch.setenv("MUSICDL_AI__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_AI__BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("MUSICDL_AI__API_KEY", "  phase5-secret  ")
    monkeypatch.setenv("MUSICDL_AI__MODEL", "  compatible-model  ")
    settings = AppSettings().ai
    assert settings.api_key.get_secret_value() == "phase5-secret"
    assert settings.model == "compatible-model"
    assert "phase5-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [("api_key", None), ("api_key", "   "), ("model", None), ("model", "   ")],
)
def test_enabled_ai_rejects_each_missing_or_blank_credential(monkeypatch, field, value):
    monkeypatch.setenv("MUSICDL_AI__ENABLED", "true")
    other = "model" if field == "api_key" else "api_key"
    monkeypatch.setenv(f"MUSICDL_AI__{other.upper()}", "valid-credential")
    if value is not None:
        monkeypatch.setenv(f"MUSICDL_AI__{field.upper()}", value)
    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize("base_url", ["ftp://provider.example/v1"])
def test_ai_rejects_unsupported_base_url(monkeypatch, base_url):
    monkeypatch.setenv("MUSICDL_AI__BASE_URL", base_url)
    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize("timeout", ["0", "nan", "121"])
def test_ai_rejects_invalid_timeout(monkeypatch, timeout):
    monkeypatch.setenv("MUSICDL_AI__TIMEOUT", timeout)
    with pytest.raises(ValidationError):
        AppSettings()


def test_ai_timeout_admits_the_deadline_a_slow_relay_needs(monkeypatch):
    """A reasoning model behind a relay answered a real ranking in 58.5 seconds."""
    monkeypatch.setenv("MUSICDL_AI__TIMEOUT", "120")
    assert AppSettings().ai.timeout == 120.0


def test_ai_user_agent_is_trimmed_and_a_blank_clears_it(monkeypatch):
    monkeypatch.setenv("MUSICDL_AI__USER_AGENT", "  claude-cli/1.0.0 (external, cli)  ")
    assert AppSettings().ai.user_agent == "claude-cli/1.0.0 (external, cli)"
    monkeypatch.setenv("MUSICDL_AI__USER_AGENT", "   ")
    assert AppSettings().ai.user_agent is None


@pytest.mark.parametrize("value", ["x" * 201, "claude-cli/1.0.0\r\nX-Injected: 1", "claude\ncli"])
def test_ai_rejects_a_user_agent_that_is_not_one_bounded_header(value):
    with pytest.raises(ValidationError):
        AISettings(user_agent=value)


@pytest.mark.parametrize("max_candidates", ["true", "0", "101"])
def test_ai_rejects_invalid_max_candidates(monkeypatch, max_candidates):
    monkeypatch.setenv("MUSICDL_AI__MAX_CANDIDATES", max_candidates)
    with pytest.raises(ValidationError):
        AppSettings()


def test_ai_rejects_boolean_max_candidates_directly():
    with pytest.raises(ValidationError):
        AISettings(max_candidates=True)


def test_ai_accepts_numeric_max_candidates_environment_string(monkeypatch):
    monkeypatch.setenv("MUSICDL_AI__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_AI__API_KEY", "phase5-secret")
    monkeypatch.setenv("MUSICDL_AI__MODEL", "phase5-model")
    monkeypatch.setenv("MUSICDL_AI__MAX_CANDIDATES", "12")

    settings = AppSettings().ai

    assert settings.max_candidates == 12


def test_redis_timeouts_default_and_environment_override(monkeypatch):
    settings = AppSettings()
    assert settings.redis.connect_timeout == 2.0
    assert settings.redis.operation_timeout == 2.0
    monkeypatch.setenv("MUSICDL_REDIS__CONNECT_TIMEOUT", "4.5")
    monkeypatch.setenv("MUSICDL_REDIS__OPERATION_TIMEOUT", "3")
    settings = AppSettings()
    assert settings.redis.connect_timeout == 4.5
    assert settings.redis.operation_timeout == 3.0


@pytest.mark.parametrize("name", ["CONNECT_TIMEOUT", "OPERATION_TIMEOUT"])
def test_redis_timeouts_reject_invalid_values(monkeypatch, name):
    monkeypatch.setenv(f"MUSICDL_REDIS__{name}", "31")
    with pytest.raises(ValidationError):
        AppSettings()


def test_worker_budget_defaults_leave_material_slack():
    worker = AppSettings().worker

    assert (worker.search_timeout, worker.resolve_stream_timeout, worker.health_timeout) == (8.0, 15.0, 5.0)
    assert (worker.job_timeout, worker.budget_slack_seconds) == (30.0, 2.0)
    assert (worker.pending_idle_ms, worker.job_ttl, worker.retry_window_seconds) == (32000, 172800, 86400)
    assert worker.max_attempts == 3
    assert worker.job_timeout - (worker.resolve_stream_timeout + worker.search_timeout + worker.health_timeout) == worker.budget_slack_seconds
    assert worker.pending_idle_ms > 1000 * (max(worker.search_timeout, worker.job_timeout) + REDIS_OVERHEAD_SECONDS)
    assert worker.retry_window_seconds > worker.max_attempts * math.ceil(worker.pending_idle_ms / 1000)
    assert worker.job_ttl > worker.retry_window_seconds + math.ceil(worker.job_timeout)


def test_worker_overhead_constants_are_shared_with_the_worker_module():
    from musicdl.worker import workers

    assert (REDIS_OVERHEAD_SECONDS, WECOM_NOTICE_TIMEOUT_SECONDS) == (1.0, 10.0)
    assert workers.REDIS_OVERHEAD_SECONDS == REDIS_OVERHEAD_SECONDS
    assert workers.WECOM_NOTICE_TIMEOUT_SECONDS == WECOM_NOTICE_TIMEOUT_SECONDS


@pytest.mark.parametrize("overrides", [
    {"job_timeout": 28.0},
    {"job_timeout": 29.0},
    {"job_timeout": 30.0, "budget_slack_seconds": 2.0001},
    {"resolve_stream_timeout": 16.0},
    {"pending_idle_ms": 31000},
    {"pending_idle_ms": 30999},
    {"health_timeout": 30.0, "job_timeout": 29.0},
    {"retry_window_seconds": 96},
    {"retry_window_seconds": 95},
    {"job_ttl": 86430},
    {"job_ttl": 86429},
])
def test_worker_budget_inequalities_are_enforced(overrides):
    with pytest.raises(ValidationError):
        WorkerSettings(**overrides)


@pytest.mark.parametrize("overrides", [
    {"job_timeout": 30.0},
    {"job_timeout": 29.0, "budget_slack_seconds": 1.0},
    {"resolve_stream_timeout": 14.0, "search_timeout": 8.0},
    {"pending_idle_ms": 31001},
    {"retry_window_seconds": 97},
    {"job_ttl": 86431},
    {"max_attempts": 1, "retry_window_seconds": 33},
])
def test_worker_budget_boundaries_are_accepted(overrides):
    assert isinstance(WorkerSettings(**overrides), WorkerSettings)


@pytest.mark.parametrize("field", ["pending_idle_ms", "job_ttl", "retry_window_seconds", "max_attempts"])
def test_worker_integer_budgets_reject_booleans(field):
    with pytest.raises(ValidationError):
        WorkerSettings(**{field: True})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0, 31.0, True])
@pytest.mark.parametrize("field", ["search_timeout", "resolve_stream_timeout", "health_timeout", "job_timeout",
                                   "budget_slack_seconds"])
def test_worker_duration_budgets_are_strictly_validated(field, value):
    with pytest.raises(ValidationError):
        WorkerSettings(**{field: value})


def test_worker_settings_map_nested_environment(monkeypatch):
    monkeypatch.setenv("MUSICDL_WORKER__SEARCH_TIMEOUT", "7")
    monkeypatch.setenv("MUSICDL_WORKER__PENDING_IDLE_MS", "33000")

    worker = AppSettings().worker

    assert worker.search_timeout == 7.0
    assert worker.pending_idle_ms == 33000


def test_worker_settings_reject_an_unsound_pending_idle_from_environment(monkeypatch):
    monkeypatch.setenv("MUSICDL_WORKER__PENDING_IDLE_MS", "31000")
    with pytest.raises(ValidationError):
        AppSettings()


def test_worker_settings_defaults_are_accepted_by_the_real_job_worker():
    from musicdl.worker.workers import JobWorker

    settings = AppSettings()
    worker = settings.worker

    real = JobWorker(None, None, {}, settings.media.root, refresh=lambda *args: None,
                     job_timeout=worker.job_timeout, pending_idle_ms=worker.pending_idle_ms,
                     job_ttl=worker.job_ttl, max_attempts=worker.max_attempts,
                     retry_window_seconds=worker.retry_window_seconds,
                     resolve_stream_timeout=worker.resolve_stream_timeout,
                     refresh_timeout=worker.search_timeout, health_timeout=worker.health_timeout,
                     selection_ttl=settings.wecom.selection_ttl)

    assert (real.job_timeout, real.pending_idle_ms) == (30.0, 32000)
    assert (real.resolve_stream_timeout, real.refresh_timeout, real.health_timeout) == (15.0, 8.0, 5.0)
    assert (real.redis_overhead_seconds, real.wecom_notice_timeout) == (REDIS_OVERHEAD_SECONDS,
                                                                       WECOM_NOTICE_TIMEOUT_SECONDS)
    assert real.pending_idle_ms > 1000 * (max(real.job_timeout, real.refresh_timeout) + real.redis_overhead_seconds)
