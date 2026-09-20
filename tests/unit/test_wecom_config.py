import pytest
from pydantic import SecretStr, ValidationError

from musicdl.config import AppSettings, WeComSettings


def test_wecom_disabled_is_safe_default():
    settings = WeComSettings()
    assert settings.enabled is False
    assert settings.secret is None
    assert settings.allowed_users == []
    assert "token-secret" not in repr(settings)


def test_wecom_enabled_maps_environment_and_masks_secrets(monkeypatch):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", "ww-test")
    monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
    monkeypatch.setenv("MUSICDL_WECOM__TOKEN", "token-secret")
    monkeypatch.setenv("MUSICDL_WECOM__SECRET", "  outbound-secret  ")
    monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", "a" * 43)
    monkeypatch.setenv("MUSICDL_WECOM__ALLOWED_USERS", '["alice", "bob"]')
    settings = AppSettings().wecom
    assert settings.enabled is True
    assert settings.agent_id == 7
    assert settings.allowed_users == ["alice", "bob"]
    assert isinstance(settings.token, SecretStr)
    assert settings.secret.get_secret_value() == "outbound-secret"
    assert "token-secret" not in repr(settings)
    assert "outbound-secret" not in repr(settings)
    assert "token-secret" not in str(settings.model_dump())
    assert "outbound-secret" not in str(settings.model_dump())


@pytest.mark.parametrize("field,value", [("agent_id", "0"), ("clock_skew", "-1"), ("selection_ttl", "0")])
def test_wecom_rejects_invalid_enabled_limits(monkeypatch, field, value):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", "ww-test")
    monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
    monkeypatch.setenv("MUSICDL_WECOM__TOKEN", "token")
    monkeypatch.setenv("MUSICDL_WECOM__SECRET", "outbound-secret")
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
        monkeypatch.setenv("MUSICDL_WECOM__SECRET", "outbound-secret")
        monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", value)
        monkeypatch.setenv("MUSICDL_WECOM__ALLOWED_USERS", '["alice"]')
        with pytest.raises(ValidationError):
            AppSettings()


def test_enabled_normalizes_and_deduplicates_allowlist(monkeypatch):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", " ww-test ")
    monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
    monkeypatch.setenv("MUSICDL_WECOM__TOKEN", " token ")
    monkeypatch.setenv("MUSICDL_WECOM__SECRET", " outbound-secret ")
    monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", "a" * 43)
    monkeypatch.setenv("MUSICDL_WECOM__ALLOWED_USERS", '[" alice ", "alice"]')
    settings = AppSettings().wecom
    assert settings.corp_id == "ww-test"
    assert settings.allowed_users == ["alice"]
    assert settings.secret.get_secret_value() == "outbound-secret"


@pytest.mark.parametrize("secret", [None, "   "])
def test_enabled_rejects_missing_or_blank_outbound_secret(monkeypatch, secret):
    monkeypatch.setenv("MUSICDL_WECOM__ENABLED", "true")
    monkeypatch.setenv("MUSICDL_WECOM__CORP_ID", "ww-test")
    monkeypatch.setenv("MUSICDL_WECOM__AGENT_ID", "7")
    monkeypatch.setenv("MUSICDL_WECOM__TOKEN", "token")
    monkeypatch.setenv("MUSICDL_WECOM__ENCODING_AES_KEY", "a" * 43)
    monkeypatch.setenv("MUSICDL_WECOM__ALLOWED_USERS", '["alice"]')
    if secret is not None:
        monkeypatch.setenv("MUSICDL_WECOM__SECRET", secret)
    with pytest.raises(ValidationError):
        AppSettings()


def _official_endpoint() -> str:
    return "https://qyapi.weixin.qq.com"


def test_outbound_api_base_defaults_to_the_official_endpoint():
    assert str(WeComSettings().api_base).rstrip("/") == _official_endpoint()


def test_outbound_api_base_accepts_a_mainland_reverse_proxy():
    settings = WeComSettings(api_base="http://115.159.107.211:9080")
    assert str(settings.api_base).rstrip("/") == "http://115.159.107.211:9080"


def test_outbound_api_base_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MUSICDL_WECOM__API_BASE", "  http://proxy.example:9080/  ")
    assert str(AppSettings().wecom.api_base).rstrip("/") == "http://proxy.example:9080"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_outbound_api_base_means_the_official_endpoint(blank):
    """Clearing the panel field is how an operator asks for the official API."""
    assert str(WeComSettings(api_base=blank).api_base).rstrip("/") == _official_endpoint()


@pytest.mark.parametrize("value", ["115.159.107.211:9080", "qyapi.weixin.qq.com",
                                   "ftp://proxy.example:9080", "not a url"])
def test_outbound_api_base_must_be_an_http_url(value):
    with pytest.raises(ValidationError):
        WeComSettings(api_base=value)
