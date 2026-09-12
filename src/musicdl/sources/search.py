from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from .models import Candidate, normalize_text
from .registry import SourceEntry, SourceRegistry


@dataclass(frozen=True)
class SourceStatus:
    source_id: str
    source_version: str
    status: str
    count: int = 0

    def as_log_fields(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "source_version": self.source_version, "status": self.status, "count": self.count}


@dataclass(frozen=True)
class SearchResult:
    candidates: tuple[Candidate, ...]
    statuses: tuple[SourceStatus, ...]
    version: str


async def _invoke(entry: SourceEntry, query: str, timeout: float) -> tuple[SourceStatus, list[Candidate]]:
    try:
        fn = entry.source.search if hasattr(entry.source, "search") else entry.source
        raw = await asyncio.wait_for(fn(query), timeout=timeout)
        candidates: list[Candidate] = []
        for value in raw:
            candidate = value if isinstance(value, Candidate) else Candidate.model_validate(value)
            if candidate.source_id != entry.source_id or candidate.source_version != entry.version:
                return SourceStatus(entry.source_id, entry.version, "invalid"), []
            candidates.append(candidate)
        return SourceStatus(entry.source_id, entry.version, "ok", len(candidates)), candidates
    except asyncio.TimeoutError:
        return SourceStatus(entry.source_id, entry.version, "timeout"), []
    except Exception:
        return SourceStatus(entry.source_id, entry.version, "error"), []


async def search_sources(registry: SourceRegistry, query: str, *, timeout: float = 10.0) -> SearchResult:
    query = normalize_text(query)
    if not query or len(query) > 500:
        raise ValueError("invalid_query")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_timeout")
    entries = registry.enabled()
    outcomes = await asyncio.gather(*(_invoke(e, query, timeout) for e in entries))
    by_identity: dict[tuple, tuple[Candidate, SourceEntry]] = {}
    for entry, (status, candidates) in zip(entries, outcomes):
        for candidate in candidates:
            key = candidate.canonical_version_key
            incumbent = by_identity.get(key)
            # Prefer registry priority, completeness/size, then stable identity.
            rank = (-entry.priority, sum(v is not None for v in (candidate.album, candidate.duration, candidate.bitrate, candidate.format, candidate.size)), candidate.size or -1, entry.source_id, candidate.item_id)
            if incumbent is None or rank > (-incumbent[1].priority, sum(v is not None for v in (incumbent[0].album, incumbent[0].duration, incumbent[0].bitrate, incumbent[0].format, incumbent[0].size)), incumbent[0].size or -1, incumbent[1].source_id, incumbent[0].item_id):
                by_identity[key] = (candidate, entry)
    q = query.casefold()
    retained = [(v[0], v[1]) for v in by_identity.values()]
    def ordering(pair):
        c, entry = pair
        title, combined = c.title.casefold(), f"{c.title} {c.artist}".casefold()
        relevance = 0 if title == q or combined == q else 1 if q in combined else 2
        return (relevance, title, c.artist.casefold(), (c.format or "").casefold(), c.bitrate or -1, entry.priority, c.source_id, c.item_id)
    ordered = tuple(c for c, _ in sorted(retained, key=ordering))
    public = [c.public_representation for c in ordered]
    digest = hashlib.sha256(json.dumps(public, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return SearchResult(ordered, tuple(s for s, _ in outcomes), digest)
