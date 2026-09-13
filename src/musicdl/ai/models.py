from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from musicdl.media.models import Language
from musicdl.sources.search import SearchResult


@dataclass(frozen=True)
class AIEvent:
    operation: Literal["rank", "language"]
    status: Literal["applied", "fallback"]
    error_code: str | None = None


@dataclass(frozen=True)
class AIRankResult:
    search: SearchResult
    applied: bool
    error_code: str | None = None


@dataclass(frozen=True)
class AILanguageResult:
    language: Language
    applied: bool
    error_code: str | None = None


class AICompletionClient(Protocol):
    async def complete_json(self, messages: Sequence[dict[str, str]]) -> dict[str, Any]: ...


class AIError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def emit_event(record: Callable[[AIEvent], None] | None, event: AIEvent) -> None:
    if record is None:
        return
    try:
        record(event)
    except Exception:
        pass
