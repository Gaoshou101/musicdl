import pytest
from pydantic import ValidationError

from musicdl.config import AppSettings


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


@pytest.mark.parametrize("timeout", ["0", "nan", "61"])
def test_ai_rejects_invalid_timeout(monkeypatch, timeout):
    monkeypatch.setenv("MUSICDL_AI__TIMEOUT", timeout)
    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize("max_candidates", ["true", "0", "101"])
def test_ai_rejects_invalid_max_candidates(monkeypatch, max_candidates):
    monkeypatch.setenv("MUSICDL_AI__MAX_CANDIDATES", max_candidates)
    with pytest.raises(ValidationError):
        AppSettings()
