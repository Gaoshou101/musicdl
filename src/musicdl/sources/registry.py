from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from .models import Candidate
from .models import normalize_text


class MusicSource(Protocol):
    async def search(self, query: str) -> Iterable[Candidate]: ...


SourceCallable = Callable[[str], Awaitable[Iterable[Candidate]]]


@dataclass(frozen=True)
class SourceEntry:
    source_id: str
    version: str
    source: MusicSource | SourceCallable
    enabled: bool = True
    priority: int = 0


class SourceRegistry:
    def __init__(self, entries: Iterable[SourceEntry] = (), *, max_sources: int = 64):
        if not isinstance(max_sources, int) or isinstance(max_sources, bool) or not 1 <= max_sources <= 256:
            raise ValueError("invalid_max_sources")
        self._entries: dict[str, SourceEntry] = {}
        self._max_sources = max_sources
        for entry in entries:
            self.register(entry)

    def register(self, entry: SourceEntry) -> None:
        if not isinstance(entry.source_id, str) or not isinstance(entry.version, str):
            raise ValueError("invalid_source_id" if not isinstance(entry.source_id, str) else "invalid_source_version")
        source_id, version = normalize_text(entry.source_id), normalize_text(entry.version)
        adapter_ok = callable(entry.source) or callable(getattr(entry.source, "search", None))
        if source_id in self._entries:
            raise ValueError("duplicate_source_id")
        if not source_id or len(source_id) > 64:
            raise ValueError("invalid_source_id")
        if not version or len(version) > 64:
            raise ValueError("invalid_source_version")
        if not adapter_ok:
            raise ValueError("invalid_adapter")
        if not isinstance(entry.enabled, bool):
            raise ValueError("invalid_enabled")
        if not isinstance(entry.priority, int) or isinstance(entry.priority, bool):
            raise ValueError("invalid_priority")
        if len(self._entries) >= self._max_sources:
            raise ValueError("too_many_sources")
        self._entries[source_id] = SourceEntry(source_id, version, entry.source, entry.enabled, entry.priority)

    def enabled(self) -> tuple[SourceEntry, ...]:
        return tuple(sorted((e for e in self._entries.values() if e.enabled), key=lambda e: (e.priority, e.source_id)))

    def get(self, source_id: str) -> SourceEntry | None:
        return self._entries.get(source_id)
