import posixpath
from urllib.parse import urlsplit

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    session_root: str = "/data/telegram-sessions"


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
