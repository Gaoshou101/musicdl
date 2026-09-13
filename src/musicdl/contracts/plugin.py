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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL = "musicdl.plugin/v1"
Operation = Literal["capabilities", "search", "resolve", "download", "health"]
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 128 * 1024
MAX_ACTION_BODY_BYTES = 1024 * 1024
MAX_INVOCATION_BYTES = 6 * 1024 * 1024
MAX_HTTP_ACTIONS = 4
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


def _unique(values: tuple[Any, ...], label: str) -> tuple[Any, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique")
    return values


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plugin_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    version: str = Field(min_length=1, max_length=64)
    language: PluginLanguage
    operations: tuple[Operation, ...]
    allowed_hosts: tuple[str, ...] = ()
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
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("IP literals are not allowed")
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


class HttpAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action_id: str = Field(min_length=1, max_length=128)
    method: Literal["GET"]
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("HTTP action must be an HTTPS URL without credentials or fragment")
        if parsed.port not in (None, 443):
            raise ValueError("HTTPS action must use port 443")
        return value


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
            raise ValueError("source exceeds 128 KiB")
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
            raise ValueError("invocation exceeds 6 MiB")
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
