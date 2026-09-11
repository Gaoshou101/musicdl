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
