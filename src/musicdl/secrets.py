from collections.abc import Mapping
from typing import Any

from pydantic import SecretBytes, SecretStr

_SENSITIVE = ("password", "secret", "token", "api_key", "api_hash", "aes_key", "private_key", "authorization", "session")
_MASK = "[REDACTED_SECRET]"


def redact_secrets(value: Any, *, _key: str | None = None) -> Any:
    if _key is not None and any(part in _key.lower() for part in _SENSITIVE):
        return _MASK
    if isinstance(value, (SecretStr, SecretBytes)):
        return _MASK
    if isinstance(value, Mapping):
        return {key: redact_secrets(item, _key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, set):
        return {redact_secrets(item) for item in value}
    return value
