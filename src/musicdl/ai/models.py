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
    """A stable code, plus whatever the endpoint said about it.

    The advisor needs only the code, which is what it maps onto a fallback. The
    panel's test button needs the rest: an operator looking at a red line has to
    be able to tell "the endpoint rejected the key" from "the endpoint was slow"
    without a log window, and an HTTP status observed at the boundary is the one
    piece of evidence that says which.
    """

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code)


def emit_event(record: Callable[[AIEvent], None] | None, event: AIEvent) -> None:
    if record is None:
        return
    try:
        record(event)
    except Exception:
        pass
