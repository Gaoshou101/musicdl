from __future__ import annotations

import asyncio
import hashlib
import json
import math
import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import Candidate, normalize_text
from .quality import is_lossless, quality_rank
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
    # One entry per candidate, aligned by index: the channels that offered
    # that recording, best first.  A candidate names the one channel a
    # download starts with; this names the rest of what a retry may reach, so
    # a panel can say which channels stand behind a row instead of showing the
    # same row once per installed source.  Defaulted and last, so every
    # existing positional construction keeps meaning what it did.
    offers: tuple[tuple[str, ...], ...] = ()
    # Resolver-ready rows for the channels behind each collapsed candidate.
    # Defaulted and last so existing positional construction remains valid.
    channels: tuple[tuple[Candidate, ...], ...] = ()


def search_result_version(candidates: Sequence[Candidate]) -> str:
    public = [candidate.public_representation for candidate in candidates]
    return hashlib.sha256(json.dumps(public, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# The separators people type between a song and its artist, and the space a
# catalogue answers with.  Folding them is what makes "晴天-周杰伦" the same
# request as "晴天 周杰伦": the hyphenated spelling used to miss the
# exact-match tier entirely, and the quality sort then buried the recording
# somebody had asked for by name under louder versions of the same title.
_SEPARATORS = re.compile(r"[\s\-–—―－_·/]+")


def _search_key(value: str) -> str:
    return _SEPARATORS.sub(" ", value.casefold()).strip()


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
                         preference: Mapping[str, int] | Callable[[str], int] | None = None,
                         quality_policy: str = "lossless_first",
                         quality_preference: str | None = None,
                         lossless_capability: Mapping[str, bool | None] | Callable[[str], bool | None] | None = None
                         ) -> SearchResult:
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
    # One consistent health snapshot per search instead of repeated lookups for
    # every duplicate candidate, retained row and alternative-channel sort.
    preference = {entry.source_id: _preference_weight(preference, entry.source_id) for entry in entries}
    lossless_bias = ({entry.source_id: _lossless_bias(lossless_capability, entry.source_id) for entry in entries}
                     if quality_policy == "lossless_first" else {})
    ranks: dict[tuple[int, int], tuple[int, ...]] = {}

    def rank_of(candidate, priority):
        key = (id(candidate), priority)
        if key in ranks:
            return ranks[key]
        quality = quality_key(candidate, priority, policy=quality_policy, preference=quality_preference)
        if quality_policy != "lossless_first":
            # Compatibility mode keeps the historical quality key byte for byte.
            ranks[key] = quality
            return quality
        # A measured capability leads the quality key and is all-zero when
        # nothing was measured, so an unknown channel leaves the order exactly
        # as v1.0.5 left it.
        ranks[key] = (lossless_bias.get(candidate.source_id, 0),) + quality
        return ranks[key]
    by_identity: dict[tuple, tuple[Candidate, SourceEntry]] = {}
    # Every channel that offered a recording, grouped by the identity they all
    # agree on.  A group is kept whole while the winner is picked from it,
    # because the channels that lost the pick are exactly what a download falls
    # back to when the winner cannot serve the bytes.
    offered: dict[tuple, list[tuple[Candidate, SourceEntry]]] = {}
    for entry, (status, candidates) in zip(entries, outcomes):
        for candidate in candidates:
            key = candidate.canonical_version_key
            offered.setdefault(key, []).append((candidate, entry))
            incumbent = by_identity.get(key)
            # Prefer registry priority, completeness/size, the channel the
            # panel last saw working, then stable identity. Priority is part of
            # the quality key, so a channel an operator ranked higher still
            # outranks one that merely answered a search recently.
            rank = rank_of(candidate, entry.priority)
            incumbent_rank = rank_of(incumbent[0], incumbent[1].priority) if incumbent else None
            prefer = _preference_weight(preference, candidate.source_id)
            incumbent_prefer = _preference_weight(preference, incumbent[0].source_id) if incumbent else None
            stable = (candidate.source_id.casefold(), candidate.item_id.casefold())
            incumbent_stable = (incumbent[0].source_id.casefold(), incumbent[0].item_id.casefold()) if incumbent else None
            if incumbent is None or rank > incumbent_rank or (
                    rank == incumbent_rank
                    and (prefer > incumbent_prefer or (prefer == incumbent_prefer and stable < incumbent_stable))):
                by_identity[key] = (candidate, entry)
    q = _search_key(query)
    retained = [(v[0], v[1]) for v in by_identity.values()]

    def relevance_of(candidate: Candidate) -> int:
        """How close one candidate is to what was typed, separators aside.

        A song is asked for as "title artist" or as "artist title", and the
        catalogue answers with one spelling of it.  Both spellings are checked,
        because a request that names the artist exactly is an exact request
        whichever way round it was written.
        """
        as_written = _search_key(f"{candidate.title} {candidate.artist}")
        swapped = _search_key(f"{candidate.artist} {candidate.title}")
        if q in (_search_key(candidate.title), as_written, swapped):
            return 0
        return 1 if q in as_written or q in swapped else 2

    def ordering(row):
        relevance, c, entry = row
        quality = rank_of(c, entry.priority)
        return (relevance, *(-value for value in quality),
                c.title.casefold(), c.artist.casefold(), c.source_id.casefold(), c.item_id.casefold())
    ranked = ((relevance_of(c), c, entry) for c, entry in retained)
    ordered = tuple(c for _, c, _ in sorted(ranked, key=ordering))
    offers = tuple(_offers_of(offered[candidate.canonical_version_key], preference, rank_of)
                   for candidate in ordered)
    channels = tuple(_channel_rows_of(offered[candidate.canonical_version_key], preference, rank_of)
                     for candidate in ordered)
    return SearchResult(ordered, tuple(s for s, _ in outcomes), search_result_version(ordered), offers, channels)


def quality_key(candidate: Candidate, priority: int, *, policy: str = "lossless_first",
                preference: str | None = None) -> tuple[int, int, int, int, int, int]:
    """Higher values are better; unknown quality fields rank lowest."""
    # Keep the extra tier axis neutral in compatibility mode, preserving the
    # historical format/bitrate/completeness/size/priority order on the same metadata.
    preferred = preference.strip().casefold() if preference else None
    legacy = policy == "best_available" and not preferred
    declared = () if legacy else candidate.qualities
    supported_preference = preferred in candidate.qualities if preferred else False
    lossless = is_lossless(candidate.format) or any(is_lossless(value) for value in declared)
    tier_rank = 0 if legacy else max((quality_rank(value) for value in declared),
                                     default=quality_rank(candidate.format))
    completeness = sum(value is not None for value in (candidate.album, candidate.duration, candidate.bitrate, candidate.format, candidate.size))
    return (2 if supported_preference else int(lossless), tier_rank,
            candidate.bitrate if candidate.bitrate is not None else -1,
            completeness, candidate.size if candidate.size is not None else -1, -priority)


def _offers_of(group: Sequence[tuple[Candidate, SourceEntry]],
               preference: Mapping[str, int] | Callable[[str], int] | None,
               rank_of=quality_key) -> tuple[str, ...]:
    """The channels that offered one recording, best first, once each.

    The order is the order a download reaches them in: the same quality key the
    pick uses, then the panel's own observation, then the stable channel name.
    It answers the question a single ``source_id`` cannot -- with twelve
    channels all listing the same catalogue entry, which of them stand behind
    the row, and which one is tried first.
    """
    def order(pair: tuple[Candidate, SourceEntry]) -> tuple:
        candidate, entry = pair
        quality = rank_of(candidate, entry.priority)
        return (*(-value for value in quality),
                -_preference_weight(preference, candidate.source_id),
                candidate.source_id.casefold(), candidate.item_id.casefold())
    names: list[str] = []
    seen: set[str] = set()
    for candidate, _ in sorted(group, key=order):
        if candidate.source_id not in seen:
            names.append(candidate.source_id)
            seen.add(candidate.source_id)
    return tuple(names)


def _channel_rows_of(group: Sequence[tuple[Candidate, SourceEntry]],
                     preference: Mapping[str, int] | Callable[[str], int] | None,
                     rank_of=quality_key) -> tuple[Candidate, ...]:
    """Return one resolver-ready row per offering channel in offer order."""
    def order(pair: tuple[Candidate, SourceEntry]) -> tuple:
        candidate, entry = pair
        quality = rank_of(candidate, entry.priority)
        return (*(-value for value in quality),
                -_preference_weight(preference, candidate.source_id),
                candidate.source_id.casefold(), candidate.item_id.casefold())
    rows: list[Candidate] = []
    seen: set[str] = set()
    for candidate, _ in sorted(group, key=order):
        if candidate.source_id not in seen:
            rows.append(candidate)
            seen.add(candidate.source_id)
    return tuple(rows)


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
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value)


def _lossless_bias(lookup: Mapping[str, bool | None] | Callable[[str], bool | None] | None,
                   source_id: str) -> int:
    """Whether a channel was measured able to serve lossless, as an ordering bias.

    Only a recorded ``True`` moves a channel up.  ``False`` and an absent
    record mean the same thing here -- no usable measurement -- so they leave
    the order untouched, and a lookup that raises, names nothing, or answers
    with something that is not a bool is read the same way: a tie-break input
    must never be the reason a search fails.
    """
    if lookup is None:
        return 0
    try:
        value = lookup(source_id) if callable(lookup) else lookup[source_id]
    except Exception:
        return 0
    return 1 if value is True else 0
