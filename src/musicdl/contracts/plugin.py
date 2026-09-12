import json
import ntpath
import posixpath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL = "musicdl.plugin/v1"
Operation = Literal["capabilities", "search", "resolve", "download", "health"]
MAX_PAYLOAD_BYTES = 64 * 1024


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
