import posixpath
from urllib.parse import urlsplit
import base64
import binascii
import re

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TELEGRAM_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ConfigVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)


class RedisSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: SecretStr = SecretStr("redis://localhost:6379/0")

    @field_validator("url")
    @classmethod
    def url_must_use_redis_scheme(cls, value: SecretStr):
        if urlsplit(value.get_secret_value()).scheme not in {"redis", "rediss"}:
            raise ValueError("Redis URL must use redis:// or rediss://")
        return value


class MediaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = "/data/music"


class TelegramSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    api_id: int | None = Field(default=None, gt=0)
    api_hash: SecretStr | None = None
    profile: str = "default"
    session_root: str = "/data/telegram-sessions"
    proxy: str | None = None

    @model_validator(mode="after")
    def validate_credentials(self):
        self.profile = self.profile.strip()
        if not _TELEGRAM_PROFILE.fullmatch(self.profile):
            raise ValueError("Telegram profile must be a simple name")
        if self.proxy is not None:
            self.proxy = self.proxy.strip() or None
        if self.api_hash is not None:
            self.api_hash = SecretStr(self.api_hash.get_secret_value().strip())
        if self.enabled and (self.api_id is None or self.api_hash is None or not self.api_hash.get_secret_value()):
            raise ValueError("enabled Telegram settings require credentials")
        return self


class WeComSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    corp_id: str | None = None
    agent_id: int | None = Field(default=None, gt=0)
    token: SecretStr | None = None
    encoding_aes_key: SecretStr | None = None
    allowed_users: list[str] = Field(default_factory=list)
    clock_skew: int = Field(default=300, ge=1, le=3600)
    dedup_ttl: int = Field(default=86400, ge=60, le=604800)
    selection_ttl: int = Field(default=600, ge=60, le=86400)

    @model_validator(mode="after")
    def enabled_requires_credentials(self):
        if self.corp_id is not None:
            self.corp_id = self.corp_id.strip()
        if self.token is not None:
            self.token = SecretStr(self.token.get_secret_value().strip())
        if self.encoding_aes_key is not None:
            key = self.encoding_aes_key.get_secret_value()
            if len(key) != 43:
                raise ValueError("EncodingAESKey must be 43 characters")
            try:
                decoded = base64.b64decode(key + "=", validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("EncodingAESKey must be valid base64") from None
            if len(decoded) != 32:
                raise ValueError("EncodingAESKey must decode to 32 bytes")
        normalized = []
        for user in self.allowed_users:
            item = user.strip()
            if item and item not in normalized:
                normalized.append(item)
        self.allowed_users = normalized
        if self.enabled:
            if not self.corp_id or self.agent_id is None or not self.token or not self.token.get_secret_value() or not self.encoding_aes_key:
                raise ValueError("enabled WeCom settings require credentials")
            if not 1 <= len(self.allowed_users) <= 100 or any(len(item) > 128 for item in self.allowed_users):
                raise ValueError("enabled WeCom settings require a non-empty allowlist")
        return self


class PluginSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_url: AnyUrl = AnyUrl("http://plugin:8080")

    @field_validator("service_url")
    @classmethod
    def service_url_must_use_http_scheme(cls, value: AnyUrl):
        if value.scheme not in {"http", "https"}:
            raise ValueError("plugin service URL must use http:// or https://")
        return value


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSICDL_", env_nested_delimiter="__", extra="ignore"
    )
    config: ConfigVersion = ConfigVersion()
    redis: RedisSettings = RedisSettings()
    media: MediaSettings = MediaSettings()
    telegram: TelegramSettings = TelegramSettings()
    wecom: WeComSettings = WeComSettings()
    plugin: PluginSettings = PluginSettings()

    @model_validator(mode="after")
    def roots_must_differ(self):
        media = posixpath.normpath(self.media.root)
        session = posixpath.normpath(self.telegram.session_root)
        if not media.startswith("/") or not session.startswith("/"):
            raise ValueError("media and Telegram session roots must be absolute POSIX paths")
        if media == session or media.startswith(session + "/") or session.startswith(media + "/"):
            raise ValueError("media root and Telegram session root must be separate")
        return self
