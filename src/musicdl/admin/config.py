"""The configuration the panel owns, layered over the deployment's environment.

Compose gives this process a handful of variables and nothing else: an operator
who wants to set the WeCom credentials, the AI endpoint or a worker budget has
to edit ``.env`` on the host and recreate the container. The portal exists to
remove exactly that step, so it keeps a second layer of settings beside the
credentials it already stores and applies them to the settings object before
the runtime is assembled.

Three rules keep that layer safe:

* Only the fields declared in :data:`GROUPS` can be written. An unknown path is
  refused instead of stored and silently ignored.
* A write is validated by rebuilding the whole settings object, so a change that
  would break a cross-field invariant is refused when it is made rather than
  preventing the deployment from starting.
* A secret is never read back. The panel reports whether one is set, and a
  blank value keeps whatever is already stored.

Every field carries the scope it takes effect in: ``hot`` when the running
process adopts the new value immediately -- either by retuning what it owns
directly or by rebuilding the runtime the setting feeds -- and ``restart`` for
the rare field the next start has to pick up. Settings this process cannot
change at all -- the published ports, the volumes, the resource limits -- are
listed separately as container knobs, so the panel can say who owns them
rather than offering a control that would lie.
"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr, ValidationError

from ..config import AppSettings


@dataclass(frozen=True)
class ConfigField:
    """One setting the panel may write, and how the panel should render it."""

    key: str
    label: str
    kind: str
    help: str
    scope: str = "hot"

    @property
    def env(self) -> str:
        """The environment variable that sets the same thing from outside."""
        return "MUSICDL_" + self.key.replace(".", "__").upper()

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


@dataclass(frozen=True)
class ConfigGroup:
    id: str
    label: str
    help: str
    fields: tuple[ConfigField, ...]


GROUPS: tuple[ConfigGroup, ...] = (
    ConfigGroup(
        id="wecom",
        label="企业微信",
        help="回调边界与白名单。关闭时后台照样可用，只是没有机器人入口。",
        fields=(
            ConfigField("wecom.enabled", "启用企业微信边界", "bool",
                        "打开后才会注册 /wecom/callback 并启动两个 worker。"),
            ConfigField("wecom.corp_id", "企业 ID", "text", "企微后台的 CorpID。"),
            ConfigField("wecom.agent_id", "应用 AgentId", "int", "企微应用的 AgentId。"),
            ConfigField("wecom.token", "回调 Token", "secret", "回调签名 Token，留空表示不修改。"),
            ConfigField("wecom.secret", "应用 Secret", "secret", "应用 Secret，留空表示不修改。"),
            ConfigField("wecom.api_base", "消息代理地址", "text",
                        "出网 API 地址，留空即用官方 https://qyapi.weixin.qq.com。"
                        "海外部署可指向国内的反向代理（例如 ddsderek/wxchat 的 http://主机:9080），"
                        "gettoken 与发消息就从可信 IP 发出；只需写协议、主机和端口，路径会被拼在后面。"
                        "注意：公网明文 HTTP 会让应用 Secret 暴露在链路上，能上就上 HTTPS。"),
            ConfigField("wecom.encoding_aes_key", "EncodingAESKey", "secret",
                        "43 位 EncodingAESKey，解码后必须是 32 字节。"),
            ConfigField("wecom.allowed_users", "白名单", "list",
                        "允许使用的企微账号，逗号分隔；启用时不能为空。"),
            ConfigField("wecom.clock_skew", "回调时间容差（秒）", "int", "默认 300 秒。"),
            ConfigField("wecom.dedup_ttl", "回调去重 TTL（秒）", "int", "默认 86400 秒。"),
            ConfigField("wecom.selection_ttl", "候选选择 TTL（秒）", "int", "默认 600 秒。"),
        ),
    ),
    ConfigGroup(
        id="redis",
        label="Redis",
        help="企业微信会话与任务队列的外部依赖；只使用面板搜索时用不到它。",
        fields=(
            ConfigField("redis.url", "Redis 地址", "secret",
                        "redis:// 或 rediss:// 地址。改错会让重启后的服务无法就绪，请谨慎。"),
            ConfigField("redis.connect_timeout", "连接超时（秒）", "float", "默认 2 秒。"),
            ConfigField("redis.operation_timeout", "操作超时（秒）", "float", "默认 2 秒。"),
        ),
    ),
    ConfigGroup(
        id="telegram",
        label="Telegram 连接器",
        help="用 Telegram 账号去公共或自定义音乐 Bot 取文件；Bot 定义在 Bot 管理页维护。",
        fields=(
            ConfigField("telegram.enabled", "启用 Telegram 连接器", "bool",
                        "打开后才会注册 Bot 定义对应的音源。"),
            ConfigField("telegram.api_id", "API ID", "int", "Telegram 应用的 api_id。"),
            ConfigField("telegram.api_hash", "API Hash", "secret", "Telegram 应用的 api_hash，留空表示不修改。"),
            ConfigField("telegram.profile", "Session 配置名", "text", "默认 default，只能是简单名称。"),
            ConfigField("telegram.proxy", "代理", "text", "可选，例如 socks5://127.0.0.1:1080。"),
        ),
    ),
    ConfigGroup(
        id="ai",
        label="AI 辅助",
        help="只影响排序与语言分类的建议值；关闭或失败时自动回到确定性逻辑。",
        fields=(
            ConfigField("ai.enabled", "启用 AI 辅助", "bool", "默认关闭。"),
            ConfigField("ai.base_url", "接口地址", "text",
                        "OpenAI 兼容接口的 base URL，例如 https://api.openai.com/v1。"),
            ConfigField("ai.api_key", "API Key", "secret", "留空表示不修改。"),
            ConfigField("ai.model", "模型名", "text", "启用时必须填写。"),
            ConfigField("ai.user_agent", "User-Agent", "text",
                        "留空使用 HTTP 客户端默认值。部分中转站只放行特定客户端的 UA，"
                        "例如 claude-cli/1.0.0 (external, cli)。"),
            ConfigField("ai.timeout", "超时（秒）", "float", "默认 10 秒，最大 120 秒。"),
            ConfigField("ai.max_candidates", "参与排序的候选数", "int", "默认 20，最大 100。"),
        ),
    ),
    ConfigGroup(
        id="worker",
        label="任务预算",
        help="搜索、下载与重试的时间与次数上限；它们之间的不等式在保存时校验。",
        fields=(
            ConfigField("worker.search_timeout", "搜索超时（秒）", "float", "默认 8 秒。"),
            ConfigField("worker.resolve_stream_timeout", "解析下载地址超时（秒）", "float", "默认 15 秒。"),
            ConfigField("worker.health_timeout", "音源健康检查超时（秒）", "float", "默认 5 秒。"),
            ConfigField("worker.job_timeout", "任务总超时（秒）", "float", "默认 30 秒。"),
            ConfigField("worker.budget_slack_seconds", "预算余量（秒）", "float", "默认 2 秒。"),
            ConfigField("worker.pending_idle_ms", "待处理判定空闲（毫秒）", "int", "默认 32000 毫秒。"),
            ConfigField("worker.job_ttl", "任务 TTL（秒）", "int", "默认 172800 秒。"),
            ConfigField("worker.retry_window_seconds", "重试窗口（秒）", "int", "默认 86400 秒。"),
            ConfigField("worker.max_attempts", "最大尝试次数", "int", "默认 3 次。"),
        ),
    ),
    ConfigGroup(
        id="admin",
        label="后台登录",
        help="登录失败预算；改动立即对新的尝试生效，不需要重启。",
        fields=(
            ConfigField("admin.login_limit", "窗口内失败次数上限", "int",
                        "默认 5 次，超出后该来源被限速。"),
            ConfigField("admin.login_window_seconds", "限速窗口（秒）", "float",
                        "默认 60 秒。"),
        ),
    ),
)

EDITABLE: dict[str, ConfigField] = {item.key: item for group in GROUPS for item in group.fields}

# The settings groups the panel owns -- every group that has at least one
# editable field. A rebuild validates these and leaves the rest alone: the
# media root or the state path is the deployment's business, and a value the
# deployment assigned outside the validator must not make the panel unable to
# save anything at all.
OWNED_GROUPS: tuple[str, ...] = ("wecom", "redis", "telegram", "ai", "worker", "admin")

# The groups the assembled runtime reads, and therefore the ones a write has to
# rebuild it for. ``admin`` is deliberately absent: the login limiter belongs to
# the process itself, which adopts a new budget in place, and bouncing the
# workers to change it would be a lie about what the change costs.
RUNTIME_GROUPS: tuple[str, ...] = ("wecom", "redis", "telegram", "ai", "worker")

# What only the deployment can change. The panel reports these so an operator
# reads one page instead of two places, and names the exact variable to set.
CONTAINER_KNOBS: tuple[dict[str, Any], ...] = (
    {"key": "admin.cookie_secure", "label": "仅通过 HTTPS 发送登录 Cookie", "env": "MUSICDL_ADMIN__COOKIE_SECURE",
     "help": "默认 true；直接通过 HTTP 访问的可信局域网部署设为 false，再重建主服务容器。HTTPS 反向代理部署保持 true。"},
    {"key": "ports.app", "label": "主服务回环端口", "env": "MUSICDL_PORT",
     "help": "compose.yaml 发布 127.0.0.1:${MUSICDL_PORT:-8000}:8000；改完执行 docker compose up -d musicdl。"},
    {"key": "paths.panel", "label": "管理面板静态文件", "env": "MUSICDL_ADMIN__PANEL_ROOT",
     "help": "面板由主服务自己托管，没有独立端口：镜像把控制台烘在 /app/panel，浏览器访问 /。"},
    {"key": "paths.media", "label": "媒体库挂载", "env": "MUSICDL_MEDIA__ROOT",
     "help": "Compose 固定为 /data/music，卷 musicdl-media；换成别的路径需要同时改挂载。"},
    {"key": "paths.app_data", "label": "应用数据挂载", "env": "MUSICDL_PLUGIN__APP_DATA_ROOT",
     "help": "Compose 固定为 /data/app，卷 musicdl-app-data；后台状态与已安装音源都在这里。"},
    {"key": "paths.telegram", "label": "Telegram session 挂载", "env": "MUSICDL_TELEGRAM__SESSION_ROOT",
     "help": "Compose 固定为 /data/telegram-sessions，卷 musicdl-telegram。"},
    {"key": "paths.state", "label": "后台状态文件", "env": "MUSICDL_ADMIN__STATE_PATH",
     "help": "Compose 固定为 /data/app/admin-state.json，凭据与这份配置都存在这里。"},
    {"key": "runner.url", "label": "插件运行器地址", "env": "MUSICDL_PLUGIN__SERVICE_URL",
     "help": "Compose 固定为 http://plugin-runner:8080，走内部 plugin-control 网络。"},
    {"key": "limits.app", "label": "主服务资源上限", "env": None,
     "help": "控制台与后台接口跑在同一个进程里：只读根文件系统、cap_drop ALL、no-new-privileges；生产覆盖文件另设 CPU 与内存上限。"},
    {"key": "runner.isolation", "label": "插件运行器隔离", "env": None,
     "help": "只接入内部 plugin-control 网络，不接收 Redis 地址、密钥与 Docker socket。"},
)


def env_of(key: str) -> str:
    """The variable name that sets one dotted path from outside the process."""
    return "MUSICDL_" + key.replace(".", "__").upper()


def unwrap(value: Any) -> Any:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def coerce(item: ConfigField, value: Any) -> Any:
    """Turn one JSON value into the Python shape the settings model expects."""
    if item.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{item.key} must be true or false")
        return value
    if item.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{item.key} must be an integer")
        return value
    if item.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{item.key} must be a number")
        return float(value)
    if item.kind == "list":
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
            raise ValueError(f"{item.key} must be a list of strings")
        return [entry.strip() for entry in value if entry.strip()]
    if not isinstance(value, str):
        raise ValueError(f"{item.key} must be a string")
    text = value.strip()
    if item.secret and not text:
        raise ValueError(f"{item.key} cannot be blank")
    return text


def explain(error: ValidationError) -> str:
    """One readable line for a rejected field."""
    first = error.errors()[0] if error.errors() else None
    if first is None:  # pragma: no cover - pydantic always reports something
        return "configuration is invalid"
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid value"))
    return f"{location}: {message}" if location else message


class ConfigManager:
    """The panel's own layer of settings, and the only thing that writes it."""

    def __init__(self, settings: AppSettings, *, on_change=None, on_adopt=None):
        self.settings = settings
        # The environment layer, captured before any override is applied, so a
        # cleared setting falls back to the value the deployment started with.
        self._base = settings.model_dump()
        # A clean baseline for every field the panel may not write. The rebuild
        # validates the panel's own paths on top of it, because a value the
        # deployment assigned outside the validator -- a test's temporary state
        # path, for instance -- must not stop the panel from saving anything.
        self._defaults = AppSettings().model_dump()
        self._overrides: dict[str, Any] = {}
        self._rejected: dict[str, str] = {}
        self._on_change = on_change
        self._on_adopt = on_adopt

    # -- persistence ----------------------------------------------------
    def load(self, payload: Any) -> None:
        """Adopt the stored layer, dropping anything this build no longer knows."""
        values = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            return
        cleaned = {}
        rejected = {}
        for key, value in values.items():
            item = EDITABLE.get(key)
            if item is None:
                rejected[str(key)] = "unknown setting"
                continue
            try:
                cleaned[key] = coerce(item, value)
            except ValueError as error:
                rejected[key] = str(error)
        self._overrides = cleaned
        self._rejected = rejected
        self._apply(recover=True)

    def snapshot(self) -> dict:
        return deepcopy(self._overrides)

    # -- what a change costs --------------------------------------------

    @staticmethod
    def affects_runtime(key: str) -> bool:
        """Whether the assembled runtime reads this setting at all."""
        return key.split(".", 1)[0] in RUNTIME_GROUPS

    def effective(self, keys) -> dict[str, Any]:
        """The values in force right now, secrets unwrapped for comparison.

        The portal diffs this before and after a write, so a save that repeats
        what was already stored -- or clears an override the deployment's own
        value already matched -- does not bounce the workers for nothing.
        """
        return {key: unwrap(self._value(key)) for key in keys if key in EDITABLE}

    # -- reading --------------------------------------------------------
    def describe(self) -> dict:
        return {
            "groups": [
                {"id": group.id, "label": group.label, "help": group.help,
                 "fields": [self._field_view(item) for item in group.fields]}
                for group in GROUPS
            ],
            "container": [dict(knob) for knob in CONTAINER_KNOBS],
            "rejected": dict(self._rejected),
        }

    def _field_view(self, item: ConfigField) -> dict:
        value = self._value(item.key)
        view = {"key": item.key, "env": item.env, "label": item.label, "kind": item.kind,
                "scope": item.scope, "help": item.help, "secret": item.secret,
                "source": self._source(item.key)}
        if item.secret:
            # The value itself never leaves the process; only whether one is set.
            view.update(value=None, set=bool(unwrap(value)))
        else:
            view.update(value=self._render(value), set=value is not None)
        return view

    def _render(self, value: Any) -> Any:
        """A JSON-native value for the panel; a secret is rendered elsewhere."""
        if isinstance(value, (list, tuple)):
            return [self._render(item) for item in value]
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, SecretStr):  # pragma: no cover - secrets mask earlier
            return None
        return str(value)

    def _value(self, key: str) -> Any:
        node: Any = self.settings
        for part in key.split("."):
            node = getattr(node, part)
        return node

    def _source(self, key: str) -> str:
        if key in self._overrides:
            return "panel"
        return "env" if env_of(key) in os.environ else "default"

    # -- writing --------------------------------------------------------
    def update(self, values: Any) -> dict:
        """Validate one batch, persist it, and adopt whatever it can apply now."""
        if not isinstance(values, dict) or not values:
            raise ValueError("no configuration was provided")
        unknown = sorted(key for key in values if key not in EDITABLE)
        if unknown:
            raise ValueError(f"unknown setting: {unknown[0]}")
        candidate = deepcopy(self._overrides)
        for key, value in values.items():
            item = EDITABLE[key]
            if value is None:
                candidate.pop(key, None)
                continue
            if item.secret and value == "":
                # A blank secret is "leave it alone", not "clear it": clearing
                # is an explicit null, which no text field can send by accident.
                continue
            candidate[key] = coerce(item, value)
        try:
            settings = self._materialize(candidate)
        except ValidationError as error:
            raise ValueError(explain(error)) from None
        previous = deepcopy(self._overrides)
        self._overrides = candidate
        self._adopt(settings)
        try:
            if self._on_change is not None:
                self._on_change()
        except BaseException:
            self._overrides = previous
            self._apply()
            raise
        self._rejected = {key: reason for key, reason in self._rejected.items() if key in candidate}
        return self.describe()

    def _materialize(self, overrides: dict[str, Any]) -> AppSettings:
        """Rebuild the fields the panel owns, so every invariant still holds.

        Only the editable paths are carried over from the live settings; every
        other field is validated against its own default, and never adopted.
        """
        data = deepcopy(self._defaults)
        for key in EDITABLE:
            parts = key.split(".")
            node, source = data, self._base
            for part in parts[:-1]:
                node, source = node[part], source[part]
            node[parts[-1]] = deepcopy(source[parts[-1]])
        for key, value in overrides.items():
            parts = key.split(".")
            node = data
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = value
        return AppSettings.model_validate(data)

    def _adopt(self, settings: AppSettings) -> None:
        """Copy the effective value of every editable field onto the live object.

        Only those paths move: a group the panel owns carries fields it does not
        own -- the Telegram session root, the state path -- and those keep the
        value the deployment gave them.
        """
        for key in EDITABLE:
            parts = key.split(".")
            target, source = self.settings, settings
            for part in parts[:-1]:
                target, source = getattr(target, part), getattr(source, part)
            setattr(target, parts[-1], getattr(source, parts[-1]))
        if self._on_adopt is not None:
            self._on_adopt()

    def _apply(self, *, recover: bool = False) -> None:
        """Put the stored layer in force, dropping what it can no longer carry."""
        if not self._overrides:
            if recover:
                self._rejected = {}
            return
        try:
            settings = self._materialize(self._overrides)
        except ValidationError:
            if not recover:
                raise
            accepted: dict[str, Any] = {}
            rejected = dict(self._rejected)
            for key, value in self._overrides.items():
                attempt = dict(accepted)
                attempt[key] = value
                try:
                    self._materialize(attempt)
                except ValidationError as error:
                    rejected[key] = explain(error)
                    continue
                accepted = attempt
            self._overrides = accepted
            self._rejected = rejected
            if not accepted:
                return
            settings = self._materialize(accepted)
        self._adopt(settings)
