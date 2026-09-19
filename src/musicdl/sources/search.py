from __future__ import annotations

import asyncio
import hashlib
import json
import math
import inspect
from collections.abc import Callable, Mapping, Sequence
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


def search_result_version(candidates: Sequence[Candidate]) -> str:
    public = [candidate.public_representation for candidate in candidates]
    return hashlib.sha256(json.dumps(public, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _invoke(entry: SourceEntry, query: str, timeout: float, max_results: int) -> tuple[SourceStatus, list[Candidate]]:
    try:
        fn = entry.source.search if hasattr(entry.source, "search") else entry.source
        pending = fn(query)
        if not inspect.isawaitable(pending):
            return SourceStatus(entry.source_id, entry.version, "invalid"), []
        raw = await asyncio.wait_for(pending, timeout=timeout)
        if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence) or len(raw) > max_results:
            return SourceStatus(entry.source_id, entry.version, "invalid"), []
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


async def search_sources(registry: SourceRegistry, query: str, *, timeout: float = 10.0,
                         max_results_per_source: int = 100,
                         preference: Mapping[str, int] | Callable[[str], int] | None = None) -> SearchResult:
    """Search every enabled source, and answer with one candidate per recording.

    Two channels offering the same recording are the normal case, so one of
    them has to be chosen.  The declared priority decides first; when two
    channels were never told apart, ``preference`` breaks the tie with what the
    deployment actually observed -- the administration portal passes its own
    per-channel health as a ``{source_id: weight}`` mapping, and this module
    stays ignorant of where that came from.  A preference that raises, names a
    channel nothing observed, or is absent entirely is simply no preference,
    and then the stable ``(source, item)`` order decides as before.
    """
    query = normalize_text(query)
    if not query or len(query) > 500:
        raise ValueError("invalid_query")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid_timeout")
    if not isinstance(max_results_per_source, int) or isinstance(max_results_per_source, bool) or not 1 <= max_results_per_source <= 1000:
        raise ValueError("invalid_max_results_per_source")
    entries = registry.enabled()
    outcomes = await asyncio.gather(*(_invoke(e, query, timeout, max_results_per_source) for e in entries))
    by_identity: dict[tuple, tuple[Candidate, SourceEntry]] = {}
    for entry, (status, candidates) in zip(entries, outcomes):
        for candidate in candidates:
            key = candidate.canonical_version_key
            incumbent = by_identity.get(key)
            # Prefer registry priority, completeness/size, the channel the
            # panel last saw working, then stable identity. Priority is part of
            # the quality key, so a channel an operator ranked higher still
            # outranks one that merely answered a search recently.
            rank = quality_key(candidate, entry.priority)
            incumbent_rank = quality_key(incumbent[0], incumbent[1].priority) if incumbent else None
            prefer = _preference_weight(preference, candidate.source_id)
            incumbent_prefer = _preference_weight(preference, incumbent[0].source_id) if incumbent else None
            stable = (candidate.source_id.casefold(), candidate.item_id.casefold())
            incumbent_stable = (incumbent[0].source_id.casefold(), incumbent[0].item_id.casefold()) if incumbent else None
            if incumbent is None or rank > incumbent_rank or (
                    rank == incumbent_rank
                    and (prefer > incumbent_prefer or (prefer == incumbent_prefer and stable < incumbent_stable))):
                by_identity[key] = (candidate, entry)
    q = query.casefold()
    retained = [(v[0], v[1]) for v in by_identity.values()]
    def ordering(pair):
        c, entry = pair
        title, combined = c.title.casefold(), f"{c.title} {c.artist}".casefold()
        relevance = 0 if title == q or combined == q else 1 if q in combined else 2
        quality = quality_key(c, entry.priority)
        return (relevance, -quality[0], -quality[1], -quality[2], -quality[3], entry.priority, title, c.artist.casefold(), c.source_id.casefold(), c.item_id.casefold())
    ordered = tuple(c for c, _ in sorted(retained, key=ordering))
    return SearchResult(ordered, tuple(s for s, _ in outcomes), search_result_version(ordered))


def quality_key(candidate: Candidate, priority: int) -> tuple:
    """Higher values are better; unknown quality fields rank lowest."""
    lossless = (candidate.format or "").casefold() in {"flac", "alac", "ape", "wav", "aiff"}
    completeness = sum(value is not None for value in (candidate.album, candidate.duration, candidate.bitrate, candidate.format, candidate.size))
    return (int(lossless), candidate.bitrate if candidate.bitrate is not None else -1, completeness, candidate.size if candidate.size is not None else -1, -priority)


def _preference_weight(preference: Mapping[str, int] | Callable[[str], int] | None, source_id: str) -> int:
    """How strongly an observed channel argues for keeping its copy of a track.

    A missing preference, an unobserved channel and a preference that raises
    all mean the same thing here: nothing was observed, so there is nothing to
    say.  A search must not fail because the bookkeeping behind one of its
    tie-breaks did.
    """
    if preference is None:
        return 0
    try:
        value = preference(source_id) if callable(preference) else preference[source_id]
    except Exception:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
