from dataclasses import dataclass
from enum import Enum


class TelegramStatus(str, Enum):
    CODE_REQUIRED = "code_required"
    PASSWORD_REQUIRED = "password_required"
    READY = "ready"
    INVALID_SESSION = "invalid_session"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(frozen=True, repr=False)
class TelegramResult:
    status: TelegramStatus
    retry_after: int | None = None
    error: str | None = None

    def __repr__(self) -> str:
        values = [f"status={self.status.value!r}"]
        if self.retry_after is not None:
            values.append(f"retry_after={self.retry_after!r}")
        if self.error is not None:
            values.append(f"error={self.error!r}")
        return f"TelegramResult({', '.join(values)})"

    def to_dict(self) -> dict:
        return {"status": self.status.value, "retry_after": self.retry_after, "error": self.error}
