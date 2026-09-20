import json
import ntpath
import posixpath
import base64
import hashlib
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

PROTOCOL = "musicdl.plugin/v1"
Operation = Literal["capabilities", "search", "resolve", "download", "health"]
ResolvedExtension = Literal["mp3", "flac", "m4a", "ogg"]
MAX_PAYLOAD_BYTES = 64 * 1024
# A custom source is one script file.  The widest analysed source needs 144 KiB,
# so the ceiling moves once for the whole family instead of per script.
MAX_SOURCE_BYTES = 256 * 1024
MAX_ACTION_BODY_BYTES = 1024 * 1024
MAX_ACTION_REQUEST_BODY_BYTES = 64 * 1024
# Every step re-sends the observations it has collected, so the invocation
# ceiling has to hold MAX_HTTP_ACTIONS base64-encoded response bodies.
MAX_INVOCATION_BYTES = 16 * 1024 * 1024
MAX_HTTP_ACTIONS = 8
MAX_HTTP_REQUEST_HEADERS = 16
MAX_HTTP_HEADER_NAME_BYTES = 64
MAX_HTTP_HEADER_VALUE_BYTES = 1024
MAX_ACTION_URL_BYTES = 4096
DEFAULT_EGRESS_PORT = 443
SUPPORTED_HTTP_METHODS = frozenset({"GET", "POST"})
# The request line and the framing headers are assembled by the main process, so
# a plugin may never supply one of them and smuggle a second request past the
# broker.
UNSAFE_REQUEST_HEADERS = frozenset({
    "connection", "content-length", "expect", "host", "keep-alive",
    "proxy-authorization", "proxy-connection", "te", "trailer",
    "transfer-encoding", "upgrade",
})
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
RESOLVED_MEDIA_MAX_BYTES = 500 * 1024 * 1024
RESOLVED_MEDIA_TYPES = {
    "mp3": frozenset({"audio/mpeg"}),
    # `audio/x-flac` is the vendor-tree alias of the registered `audio/flac`,
    # and it is what the CDN a resolved keyword actually points at answers with
    # (measured 2026-09-17 on car-er.kuwo.cn: HTTP 200, Content-Type
    # audio/x-flac).  Refusing the alias refused a real song, so both names are
    # accepted here; the extension still decides which container is expected
    # and `validate_media` still sniffs the bytes.
    "flac": frozenset({"audio/flac", "audio/x-flac"}),
    "m4a": frozenset({"audio/mp4", "audio/x-m4a"}),
    # Raw ADTS, which is what a `.aac` link carries when it is not an ISO base
    # media file: the transport publishes the container the bytes announce, so
    # both readings of the same answer are legal declarations.
    "aac": frozenset({"audio/aac", "audio/x-aac"}),
    "ogg": frozenset({"audio/ogg", "application/ogg"}),
}
PluginLanguage = Literal["python", "javascript"]


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-compatible") from exc


class PluginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal[PROTOCOL]
    request_id: UUID
    operation: Operation
    timeout_ms: int = Field(default=10_000, ge=1, le=30_000)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_must_be_bounded_and_json_safe(cls, value):
        if _json_size(value) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds 64 KiB")
        return value


class ArtifactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str | None = None
    extension: str | None = None

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_be_safe(cls, value: str):
        normalized = value.replace("\\", "/")
        if not value or normalized.startswith("/") or posixpath.isabs(normalized) or ntpath.splitdrive(value)[0]:
            raise ValueError("artifact path must be relative")
        parts = normalized.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("artifact path traversal is forbidden")
        return value


class PluginError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def details_must_be_json_safe_and_bounded(cls, value):
        if _json_size(value) > MAX_PAYLOAD_BYTES:
            raise ValueError("error details exceed 64 KiB")
        return value


class PluginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal[PROTOCOL]
    request_id: UUID
    operation: Operation
    ok: bool
    result: Any = None
    error: PluginError | None = None

    @field_validator("result")
    @classmethod
    def result_must_be_json_safe_and_bounded(cls, value):
        if value is not None and _json_size(value) > MAX_PAYLOAD_BYTES:
            raise ValueError("result exceeds 64 KiB")
        return value

    @model_validator(mode="after")
    def success_and_error_must_be_consistent(self):
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("successful response requires result and forbids error")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("failed response requires error and forbids result")
        return self


class ResolvedMedia(BaseModel):
    """One playable media URL a plugin resolved for a confirmed candidate.

    The shape is checked here and the destination is checked against the
    source's egress policy by the media transport, so a plugin may name a plain
    ``http`` URL or a non-default port and the transport still refuses it unless
    the operator granted it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_id: StrictStr = Field(min_length=1, max_length=256)
    url: StrictStr = Field(min_length=1, max_length=MAX_ACTION_URL_BYTES)
    extension: ResolvedExtension
    media_type: StrictStr
    declared_size: StrictInt | None = Field(default=None, ge=0, le=RESOLVED_MEDIA_MAX_BYTES)

    @field_validator("url", mode="before")
    @classmethod
    def url_is_absolute_without_unsafe_parts(cls, value: str) -> str:
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
            raise ValueError("URL contains controls or whitespace")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("malformed media URL") from exc
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("URL must be an absolute http or https URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("URL must not carry credentials or a fragment")
        if port is not None and not 0 < port <= 65535:
            raise ValueError("media URL port is out of range")
        return value

    @model_validator(mode="after")
    def media_type_matches_extension(self):
        if self.media_type.casefold() not in RESOLVED_MEDIA_TYPES[self.extension]:
            raise ValueError("media_type does not match extension")
        return self


def _unique(values: tuple[Any, ...], label: str) -> tuple[Any, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    return values


def literal_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host string names, or ``None`` when it is a DNS name."""
    try:
        return ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return None


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plugin_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    version: str = Field(min_length=1, max_length=64)
    language: PluginLanguage
    operations: tuple[Operation, ...]
    allowed_hosts: tuple[str, ...] = ()
    # Every field below is an explicit, per-source operator decision.  The
    # defaults describe the strict contract, so a manifest that forgets to opt
    # in keeps the strict one.
    # Explicitly stated ports.  The default port of an allowed scheme always
    # comes with that scheme, so an HTTPS source reaches 443 without listing it.
    allowed_ports: tuple[int, ...] = (DEFAULT_EGRESS_PORT,)
    allow_insecure_http: bool = False
    allow_ip_hosts: bool = False
    allow_any_host: bool = False
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("version")
    @classmethod
    def printable_version(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        if not value or not all(char.isprintable() for char in value):
            raise ValueError("version must be printable")
        return value

    @field_validator("operations")
    @classmethod
    def unique_operations(cls, value: tuple[Operation, ...]) -> tuple[Operation, ...]:
        if not value:
            raise ValueError("at least one operation is required")
        return _unique(value, "operations")

    @field_validator("allowed_hosts")
    @classmethod
    def valid_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, "allowed_hosts")
        for host in value:
            if not host or host != host.lower() or host.endswith(".") or "*" in host:
                raise ValueError("allowed host must be lowercase DNS")
            if any(char in host for char in "/:@?#"):
                raise ValueError("allowed host must not contain URL syntax")
            if literal_address(host) is not None:
                # An address is still one exact target.  Whether a manifest may
                # name one is a policy question, answered below.
                continue
            try:
                ascii_host = host.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise ValueError("invalid DNS host") from exc
            if ascii_host != host or len(host) > 253:
                raise ValueError("allowed host must be an exact lowercase DNS name")
            labels = host.split(".")
            if any(not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
                raise ValueError("invalid DNS host")
        return value

    @field_validator("allowed_ports")
    @classmethod
    def valid_ports(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        _unique(value, "allowed_ports")
        for port in value:
            if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
                raise ValueError("allowed port must be an integer between 1 and 65535")
        return value

    @model_validator(mode="after")
    def egress_policy_is_consistent(self):
        if not self.allowed_ports:
            raise ValueError("at least one egress port is required")
        if not self.allow_ip_hosts and any(literal_address(host) is not None for host in self.allowed_hosts):
            raise ValueError("IP literals are not allowed")
        return self

    @property
    def egress(self) -> "EgressPolicy":
        """The policy the broker and the media transport enforce for this source."""
        return EgressPolicy(allowed_hosts=self.allowed_hosts, allowed_ports=self.allowed_ports,
                            allow_insecure_http=self.allow_insecure_http,
                            allow_ip_hosts=self.allow_ip_hosts, allow_any_host=self.allow_any_host)


class EgressPolicy(BaseModel):
    """What one source may reach, independently of how the manifest was built."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed_hosts: tuple[str, ...] = ()
    # Explicitly stated ports; see PluginManifest.allowed_ports.
    allowed_ports: tuple[int, ...] = (DEFAULT_EGRESS_PORT,)
    allow_insecure_http: bool = False
    allow_ip_hosts: bool = False
    allow_any_host: bool = False

    def permits(self, host: str) -> bool:
        """Whether the policy reaches one normalized host."""
        if self.allow_any_host:
            return True
        return host in self.allowed_hosts


class HttpAction(BaseModel):
    """One request a plugin asks the main process to perform.

    The shape is deliberately permissive. A source may describe an ``http`` URL,
    an explicit port, extra headers, or a POST body, and the broker still refuses
    every one of them unless the manifest's egress policy allows it. Which layer
    owns which refusal is the point: this model only rejects what could not be a
    well-formed request at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    action_id: str = Field(min_length=1, max_length=128)
    method: Literal["GET", "POST"] = "GET"
    url: str = Field(min_length=1, max_length=MAX_ACTION_URL_BYTES)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""

    @field_validator("url")
    @classmethod
    def wellformed_url(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
            raise ValueError("HTTP action must not contain controls or whitespace")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("malformed URL") from exc
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("HTTP action must be an absolute http or https URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("HTTP action must not carry credentials or a fragment")
        if port is not None and not 0 < port <= 65535:
            raise ValueError("HTTP action port is out of range")
        return value

    @field_validator("headers")
    @classmethod
    def headers_must_be_safe(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_HTTP_REQUEST_HEADERS:
            raise ValueError("too many request headers")
        seen: set[str] = set()
        for name, item in value.items():
            if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
                raise ValueError("invalid request header name")
            if len(name.encode("utf-8")) > MAX_HTTP_HEADER_NAME_BYTES:
                raise ValueError("request header name is too long")
            lowered = name.lower()
            if lowered in UNSAFE_REQUEST_HEADERS:
                raise ValueError("request header is owned by the transport")
            if lowered in seen:
                raise ValueError("request header names must be unique")
            seen.add(lowered)
            if not isinstance(item, str):
                raise ValueError("request header value must be text")
            if any(ord(char) < 32 or ord(char) == 127 for char in item):
                raise ValueError("unsafe request header value")
            try:
                encoded = item.encode("ascii")
            except UnicodeEncodeError as exc:
                # The main process writes the request line itself, so a value it
                # cannot encode as bytes it can also not reproduce faithfully.
                raise ValueError("request header value must be ASCII") from exc
            if len(encoded) > MAX_HTTP_HEADER_VALUE_BYTES:
                raise ValueError("request header value is too long")
        return value

    @field_validator("body")
    @classmethod
    def bounded_base64_request_body(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("request body must be base64") from exc
        if len(decoded) > MAX_ACTION_REQUEST_BODY_BYTES:
            raise ValueError("request body exceeds 64 KiB")
        return value

    @model_validator(mode="after")
    def body_belongs_to_post(self):
        if self.method != "POST" and self.body:
            raise ValueError("only POST carries a request body")
        return self


class HttpObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action_id: str = Field(min_length=1, max_length=128)
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""

    @field_validator("body")
    @classmethod
    def bounded_base64_body(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("body must be base64") from exc
        if len(decoded) > MAX_ACTION_BODY_BYTES:
            raise ValueError("response body exceeds 1 MiB")
        return value


class PluginInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest: PluginManifest
    source: str
    request: PluginRequest
    actions: tuple[HttpAction, ...] = ()
    observations: tuple[HttpObservation, ...] = ()

    @model_validator(mode="after")
    def validate_invocation(self):
        source_bytes = self.source.encode("utf-8")
        if b"\x00" in source_bytes:
            raise ValueError("source must not contain NUL")
        if len(source_bytes) > MAX_SOURCE_BYTES:
            raise ValueError(f"source exceeds {MAX_SOURCE_BYTES // 1024} KiB")
        if hashlib.sha256(source_bytes).hexdigest() != self.manifest.sha256:
            raise ValueError("source digest does not match manifest")
        if self.request.operation not in self.manifest.operations:
            raise ValueError("operation is not declared by manifest")
        if len(self.actions) > MAX_HTTP_ACTIONS or len(self.observations) > MAX_HTTP_ACTIONS:
            raise ValueError("too many HTTP actions")
        action_ids = [action.action_id for action in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("action IDs must be unique")
        if any(observation.action_id not in action_ids for observation in self.observations):
            raise ValueError("observation action ID does not match an action")
        if len(self.model_dump_json().encode()) > MAX_INVOCATION_BYTES:
            raise ValueError(f"invocation exceeds {MAX_INVOCATION_BYTES // (1024 * 1024)} MiB")
        return self


class PluginStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    response: PluginResponse | None = None
    action: HttpAction | None = None

    @model_validator(mode="after")
    def exactly_one_payload(self):
        if (self.response is None) == (self.action is None):
            raise ValueError("step must contain exactly one response or action")
        return self
