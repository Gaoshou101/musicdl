import posixpath
from urllib.parse import urlsplit
import base64
import binascii
import math
import re

from pydantic import AnyHttpUrl, AnyUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TELEGRAM_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# One shared source of truth for the two worker allowances that are not operator tunables: the
# Redis round-trip slack added to every derived effect lease and the bound on one WeCom notice.
REDIS_OVERHEAD_SECONDS = 1.0
WECOM_NOTICE_TIMEOUT_SECONDS = 10.0


class ConfigVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)


class RedisSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: SecretStr = SecretStr("redis://localhost:6379/0")
    connect_timeout: float = Field(default=2.0, gt=0, le=30)
    operation_timeout: float = Field(default=2.0, gt=0, le=30)

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
    secret: SecretStr | None = None
    # Where every outbound API call goes. A deployment outside mainland China
    # points this at a reverse proxy in front of qyapi.weixin.qq.com, which is
    # also what puts the request behind an address the corporation trusts.
    api_base: AnyHttpUrl = AnyHttpUrl("https://qyapi.weixin.qq.com")
    encoding_aes_key: SecretStr | None = None
    allowed_users: list[str] = Field(default_factory=list)
    clock_skew: int = Field(default=300, ge=1, le=3600)
    dedup_ttl: int = Field(default=86400, ge=60, le=604800)
    selection_ttl: int = Field(default=600, ge=60, le=86400)

    @field_validator("api_base", mode="before")
    @classmethod
    def api_base_falls_back_to_the_official_endpoint(cls, value: object):
        """An empty box in the panel means the official endpoint, not a failure.

        The panel sends the empty string when an operator clears the field, and
        ``AnyHttpUrl`` would reject that; the deployment's documented default is
        what the operator just asked for.
        """
        if value is None:
            return "https://qyapi.weixin.qq.com"
        if isinstance(value, str):
            return value.strip() or "https://qyapi.weixin.qq.com"
        return value

    @model_validator(mode="after")
    def enabled_requires_credentials(self):
        if self.corp_id is not None:
            self.corp_id = self.corp_id.strip()
        if self.token is not None:
            self.token = SecretStr(self.token.get_secret_value().strip())
        if self.secret is not None:
            self.secret = SecretStr(self.secret.get_secret_value().strip())
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
            if (
                not self.corp_id
                or self.agent_id is None
                or not self.token
                or not self.token.get_secret_value()
                or not self.secret
                or not self.secret.get_secret_value()
                or not self.encoding_aes_key
            ):
                raise ValueError("enabled WeCom settings require credentials")
            if not 1 <= len(self.allowed_users) <= 100 or any(len(item) > 128 for item in self.allowed_users):
                raise ValueError("enabled WeCom settings require a non-empty allowlist")
        return self


class PluginSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_url: AnyUrl = AnyUrl("http://plugin:8080")
    app_data_root: str = "/data/app"

    @field_validator("service_url")
    @classmethod
    def service_url_must_use_http_scheme(cls, value: AnyUrl):
        if value.scheme not in {"http", "https"}:
            raise ValueError("plugin service URL must use http:// or https://")
        return value


class AdminSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    login_limit: int = Field(default=5, ge=1, le=1000)
    login_window_seconds: float = Field(default=60.0, gt=0, le=3600)
    state_path: str | None = None
    # Where the built console lives, when this deployment has one. It is a
    # property of the image rather than a setting an operator tunes, so it stays
    # out of the panel's own config page and is set by the image that bakes the
    # files in. It carries no path validator on purpose: the rest of these paths
    # describe mounts inside the container, while this one is also what a
    # developer points at a local `web/out`, and a value that resolves to
    # nothing is reported at start-up rather than refused.
    panel_root: str | None = None

    @field_validator("state_path")
    @classmethod
    def state_path_must_be_an_absolute_posix_file(cls, value: str | None):
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        normalized = posixpath.normpath(trimmed)
        if not normalized.startswith("/") or normalized == "/":
            raise ValueError("admin state path must be an absolute POSIX file path")
        return normalized

    @field_validator("login_limit", mode="before")
    @classmethod
    def login_limit_must_not_be_boolean(cls, value: object):
        if isinstance(value, bool):
            raise ValueError("admin login limit must be an integer")
        return value

    @field_validator("login_window_seconds")
    @classmethod
    def login_window_must_be_finite(cls, value: float):
        if not math.isfinite(value):
            raise ValueError("admin login window must be finite")
        return value


class AISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    base_url: AnyHttpUrl = AnyHttpUrl("https://api.openai.com/v1")
    api_key: SecretStr | None = None
    model: str | None = Field(default=None, max_length=200)
    # Some relays only answer a particular client: agentrouter.org rejects every
    # default Python, curl and browser User-Agent with 401 and admits
    # "claude-cli/1.0.0 (external, cli)". The value is a header, so it is kept to
    # a single line and bounded before it is ever sent.
    user_agent: str | None = Field(default=None, max_length=200)
    # A reasoning model behind a relay answered a real 20-candidate ranking in
    # 58.5 seconds, so the ceiling has to leave room above that.
    timeout: float = Field(default=10.0, gt=0, le=120)
    max_candidates: int = Field(default=20, ge=1, le=100)

    @field_validator("base_url")
    @classmethod
    def base_url_must_use_http_scheme(cls, value: AnyHttpUrl):
        if value.scheme not in {"http", "https"}:
            raise ValueError("AI base URL must use http:// or https://")
        return value

    @field_validator("timeout")
    @classmethod
    def timeout_must_be_finite(cls, value: float):
        if not math.isfinite(value):
            raise ValueError("AI timeout must be finite")
        return value

    @field_validator("user_agent", mode="before")
    @classmethod
    def user_agent_must_be_one_header_line(cls, value: object):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("AI user agent must be a string")
        text = value.strip()
        if not text:
            return None
        if any(character in text for character in "\r\n\x00"):
            raise ValueError("AI user agent must be a single header line")
        return text

    @field_validator("max_candidates", mode="before")
    @classmethod
    def max_candidates_must_not_be_boolean(cls, value: object):
        if isinstance(value, bool):
            raise ValueError("AI max candidates must be an integer")
        return value

    @model_validator(mode="after")
    def enabled_requires_credentials(self):
        if self.api_key is not None:
            self.api_key = SecretStr(self.api_key.get_secret_value().strip())
        if self.model is not None:
            self.model = self.model.strip() or None
        if self.enabled and (self.api_key is None or not self.api_key.get_secret_value() or not self.model):
            raise ValueError("enabled AI settings require credentials")
        return self


class WorkerSettings(BaseModel):
    """Worker budgets whose cross-field slack is validated before the app can start."""

    model_config = ConfigDict(extra="forbid")
    search_timeout: float = Field(default=8.0, gt=0, le=30)
    resolve_stream_timeout: float = Field(default=15.0, gt=0, le=30)
    health_timeout: float = Field(default=5.0, gt=0, le=30)
    job_timeout: float = Field(default=30.0, gt=0, le=30)
    budget_slack_seconds: float = Field(default=2.0, gt=0, le=30)
    pending_idle_ms: int = Field(default=32000, ge=1, le=604800000)
    job_ttl: int = Field(default=172800, ge=60, le=604800)
    retry_window_seconds: int = Field(default=86400, ge=1, le=604800)
    max_attempts: int = Field(default=3, ge=1, le=100)

    @field_validator("search_timeout", "resolve_stream_timeout", "health_timeout", "job_timeout",
                     "budget_slack_seconds", mode="before")
    @classmethod
    def durations_must_not_be_boolean(cls, value: object):
        if isinstance(value, bool):
            raise ValueError("worker durations must be numbers")
        return value

    @field_validator("pending_idle_ms", "job_ttl", "retry_window_seconds", "max_attempts", mode="before")
    @classmethod
    def budgets_must_not_be_boolean(cls, value: object):
        if isinstance(value, bool):
            raise ValueError("worker budgets must be integers")
        return value

    @field_validator("search_timeout", "resolve_stream_timeout", "health_timeout", "job_timeout",
                     "budget_slack_seconds")
    @classmethod
    def durations_must_be_finite(cls, value: float):
        if not math.isfinite(value):
            raise ValueError("worker durations must be finite")
        return value

    @model_validator(mode="after")
    def budgets_must_be_consistent(self):
        """Reject any budget set that cannot satisfy the worker's own delivery contract."""
        longest_handler = max(self.search_timeout, self.job_timeout)
        if self.pending_idle_ms <= 1000 * (longest_handler + REDIS_OVERHEAD_SECONDS):
            raise ValueError("pending idle must exceed the longest handler timeout plus Redis overhead")
        if self.health_timeout > self.job_timeout:
            raise ValueError("health timeout must not exceed the job timeout")
        download_budget = self.resolve_stream_timeout + self.search_timeout + self.health_timeout
        if download_budget >= self.job_timeout:
            raise ValueError("resolve, search, and health budgets must fit inside the job timeout")
        if self.job_timeout - download_budget < self.budget_slack_seconds:
            raise ValueError("job timeout must keep the configured budget slack")
        if self.retry_window_seconds <= self.max_attempts * math.ceil(self.pending_idle_ms / 1000):
            raise ValueError("retry window must outlast every configured attempt")
        if self.job_ttl <= self.retry_window_seconds + math.ceil(self.job_timeout):
            raise ValueError("job ttl must outlast the retry window plus the job timeout")
        return self


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
    admin: AdminSettings = AdminSettings()
    ai: AISettings = AISettings()
    worker: WorkerSettings = WorkerSettings()

    @model_validator(mode="after")
    def roots_must_differ(self):
        roots = {
            "media": posixpath.normpath(self.media.root.strip()),
            "Telegram session": posixpath.normpath(self.telegram.session_root.strip()),
            "plugin app-data": posixpath.normpath(self.plugin.app_data_root.strip()),
        }
        if any(not root.startswith("/") for root in roots.values()):
            raise ValueError("media, Telegram session, and plugin app-data roots must be absolute POSIX paths")
        for name, root in roots.items():
            for other_name, other in roots.items():
                if name != other_name and (
                    root == other or other == "/" or root.startswith(other + "/")
                ):
                    raise ValueError(f"{name} root and {other_name} root must be separate")
        self.media.root = roots["media"]
        self.telegram.session_root = roots["Telegram session"]
        self.plugin.app_data_root = roots["plugin app-data"]
        return self
