import pytest
from pydantic import SecretStr, ValidationError

from musicdl.config import AppSettings, WeComSettings


def test_wecom_disabled_is_safe_default():
    settings = WeComSettings()
    assert settings.enabled is False
    assert settings.allowed_users == []
    assert "token-secret" not in repr(settings)


def test_wecom_enabled_maps_environment_and_masks_secrets(monkeypatch):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", "ww-test")
    monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
    monkeypatch.setenv("MUSICDL_WECOM__TOKEN", "token-secret")
    monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", "a" * 43)
    monkeypatch.setenv("MUSICDL_WECOM__ALLOWED_USERS", '["alice", "bob"]')
    settings = AppSettings().wecom
    assert settings.enabled is True
    assert settings.agent_id == 7
    assert settings.allowed_users == ["alice", "bob"]
    assert isinstance(settings.token, SecretStr)
    assert "token-secret" not in repr(settings)
    assert "token-secret" not in str(settings.model_dump())


@pytest.mark.parametrize("field,value", [("agent_id", "0"), ("clock_skew", "-1"), ("selection_ttl", "0")])
def test_wecom_rejects_invalid_enabled_limits(monkeypatch, field, value):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", "ww-test")
    monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
    monkeypatch.setenv("MUSICDL_WECOM__TOKEN", "token")
    monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", "a" * 43)
    monkeypatch.setenv(f"MUSICDL_WECOM__{field.upper()}", value)
    with pytest.raises(ValidationError):
        AppSettings()


def test_enabled_requires_identity_and_nonempty_allowlist(monkeypatch):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    with pytest.raises(ValidationError):
        AppSettings()


def test_enabled_requires_exact_aes_key_shape(monkeypatch):
    for value in ("a" * 42, "!" * 43):
        monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
        monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", "ww-test")
        monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
        monkeypatch.setenv("MUSICDL_WECOM__TOKEN", "token")
        monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", value)
        monkeypatch.setenv("MUSICDL_WECOM__ALLOWED_USERS", '["alice"]')
        with pytest.raises(ValidationError):
            AppSettings()


def test_enabled_normalizes_and_deduplicates_allowlist(monkeypatch):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", " ww-test ")
    monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
    monkeypatch.setenv("MUSICDL_WECOM__TOKEN", " token ")
    monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", "a" * 43)
    monkeypatch.setenv("MUSICDL_WECOM__ALLOWED_USERS", '[" alice ", "alice"]')
    settings = AppSettings().wecom
    assert settings.corp_id == "ww-test"
    assert settings.allowed_users == ["alice"]
