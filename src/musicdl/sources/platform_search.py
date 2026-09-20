"""Main-process search for the platforms the supplied lx sources resolve.

The custom sources this project was built for answer ``musicUrl`` and declare no
``musicSearch`` action, so the only search entry the product has --
``PluginSource.search`` -- can never produce a candidate from them.  Measured
2026-09-17 through the product's own path: 12/12 reported ``search_failed``
while the same twelve resolved 27-30 of 48 (platform, source) cells, so search
is the one half they cannot supply and the main process owns it here.

Three properties are deliberate.

The endpoints are main-process constants rather than manifest fields, so a
source can neither widen nor rename them: it never sees this request, and the
``available sources`` list a manifest carries is not what this reads.

Every request goes through the same ``HttpsActionBroker`` a plugin HTTP action
uses, with a policy built here from :data:`SEARCH_HOSTS` -- HTTPS, one exact
host per platform, port 443, DNS pinned to a global address, redirects refused,
body capped.  A source that asks for ``allow_any_host`` for its own downloads
does not move this boundary by one host.

One upstream search per ``(platform, query)`` is shared by every installed lx
source, because they all resolve against the same catalogue.  Twelve sources
cost four requests per query rather than forty-eight, and a catalogue measured
to answer an empty envelope by accident may be asked again inside the same
per-query budget.

What comes back is one candidate per hit, tagged with the identity of one
installed lx source, the ``lx:<platform>:<songId>`` item id its shim decodes
back on ``resolve``, and the platform the row came from.  The source keeps the
half it is good at.

The platform tag is what makes a shared catalogue search legible downstream.
Every installed lx source resolves against the same four catalogues, so all
twelve answer with the same rows; without the tag, twelve copies of one kw row
are indistinguishable from twelve findings, and nothing downstream can say
which platform a listed recording is actually on.  It is provenance, not a
second search: the source still runs no code here, and ``resolve`` still goes
to the channel it was asked of.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from musicdl.contracts.plugin import HttpAction
from musicdl.plugins.broker import ActionDenied, HttpsActionBroker

from .models import Candidate, normalize_text

# The platforms the supplied sources declare, in the order the portal lists
# them.  Every one of the twelve builds its handler table from the same five
# names (``wy``, ``tx``, ``kw``, ``kg``, ``mg``); ``mg`` is absent because its
# search endpoint is unreachable from this deployment (measured 2026-09-17:
# ``redirect_denied``), so offering it would only produce candidates nothing
# can resolve.
PLATFORMS: tuple[str, ...] = ("kw", "wy", "tx", "kg")

# One exact host per platform, all HTTPS on 443.  The broker adds its own
# browser-shaped User-Agent and an Accept header; a Referer is what the three
# that require one are checked against.
SEARCH_HOSTS: tuple[str, ...] = ("search.kuwo.cn", "music.163.com", "c.y.qq.com",
                                 "songsearch.kugou.com")

# How many rows to ask each platform for.  Four platforms of 20 stay under the
# 100-candidate ceiling one source entry may return.
DEFAULT_LIMIT = 20
MAX_LIMIT = 25
# The item id this module builds and the lx shim decodes.
ITEM_PREFIX = "lx:"
MAX_CANDIDATES = 100
MAX_TITLE_CHARS = 500
# A search answers with excerpts and karaoke cuts as well as songs.  Measured
# 2026-09-17: kuwo's first row for 夜曲 is a 22-second ringtone excerpt and its
# fourth is an official 230-second track, so the floor is what separates a song
# from a sample.
MIN_DURATION_SECONDS = 30
MAX_DURATION_SECONDS = 86400

# An id has to survive being pasted into ``lx:<platform>:<id>`` and handed back
# to a source, so only characters that cannot change the meaning of the id are
# kept.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX_ID = re.compile(r"^[A-Fa-f0-9]{32}$")


class PlatformSearchError(Exception):
    """No platform answered, so no candidate could be produced."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class PlatformHit:
    """One song as one platform describes it, before it belongs to a source."""

    platform: str
    song_id: str
    title: str
    artist: str
    album: str | None = None
    duration: int | None = None

    @property
    def item_id(self) -> str:
        return f"{ITEM_PREFIX}{self.platform}:{self.song_id}"


def _text(value: Any, limit: int = MAX_TITLE_CHARS) -> str:
    """One display field, with the entities a search API leaves in it resolved."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    # Kuwo answers ``Jay&nbsp;Chou`` and ``夜曲&nbsp;(mp3.2)``; the entity is
    # part of the wire format, not part of the title.
    collapsed = normalize_text(html.unescape(value))
    return collapsed[:limit].strip()


def _seconds(value: Any, *, per: int = 1) -> int | None:
    """A duration in whole seconds, or ``None`` when the platform did not say."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value / per) or None


def _joined(value: Any, *, key: str) -> str:
    """A list of collaborators as one field, the way the roster is displayed."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ""
    names = [_text(item.get(key)) for item in value if isinstance(item, Mapping)]
    return "/".join(name for name in names if name)


def _duration_or_drop(value: Any, *, per: int = 1) -> int | None | bool:
    """Return the duration, or ``False`` when the row is an excerpt, not a song.

    The distinction matters: a platform that omits the field leaves a usable
    row with an unknown duration, while a platform that states 22 seconds has
    answered about a sample.
    """
    seconds = _seconds(value, per=per)
    if seconds is None:
        return None
    if seconds < MIN_DURATION_SECONDS:
        return False
    return min(seconds, MAX_DURATION_SECONDS)


def _kw_read(row: Mapping[str, Any]) -> tuple:
    raw = row.get("MUSICRID") if isinstance(row.get("MUSICRID"), str) else None
    if not raw:
        raw = row.get("DC_TARGETID")
    if isinstance(raw, str) and raw.startswith("MUSIC_"):
        # The search API prefixes the rid it also returns bare as DC_TARGETID,
        # and every source resolves the bare number (measured 2026-09-17:
        # 128014 and 23928868 both resolve; ``MUSIC_`` does not).
        raw = raw[len("MUSIC_"):]
    return (raw, row.get("NAME") or row.get("SONGNAME"),
            row.get("ARTIST") or row.get("AARTIST"), row.get("ALBUM"),
            row.get("DURATION"), None)


def _wy_read(row: Mapping[str, Any]) -> tuple:
    album = row.get("album")
    return (row.get("id"), row.get("name"), _joined(row.get("artists"), key="name"),
            album.get("name") if isinstance(album, Mapping) else None,
            # NetEase states milliseconds; every other platform states seconds.
            row.get("duration"), 1000)


def _tx_read(row: Mapping[str, Any]) -> tuple:
    return (row.get("songmid"), row.get("songname"), _joined(row.get("singer"), key="name"),
            row.get("albumname"), row.get("interval"), None)


def _kg_read(row: Mapping[str, Any]) -> tuple:
    return (row.get("FileHash"), row.get("SongName"), _joined(row.get("Singers"), key="name"),
            row.get("AlbumName"), row.get("Duration"), None)


def _rows_of(payload: Mapping[str, Any], path: tuple[str, ...]) -> Sequence[Any]:
    """Walk one documented envelope and return its row list, or nothing."""
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return ()
        current = current.get(key)
    return current if isinstance(current, Sequence) and not isinstance(current, (str, bytes)) else ()


@dataclass(frozen=True)
class _Platform:
    """Everything one platform needs: where to ask, and how to read the answer."""

    name: str
    host: str
    referer: str
    url: Callable[[str, int], str]
    rows_path: tuple[str, ...]
    read: Callable[[Mapping[str, Any]], tuple]
    id_pattern: re.Pattern[str]
    # How many draws one query gets from this platform.  One everywhere the
    # platform answers what it was asked, because a second request there would
    # only be load on the upstream.
    attempts: int = 1


_PLATFORMS: dict[str, _Platform] = {
    # Kuwo answers a JavaScript object literal rather than JSON: single-quoted
    # keys, single-quoted values, and ``\u0027`` for an apostrophe inside one.
    "kw": _Platform(
        name="kw", host="search.kuwo.cn", referer="https://www.kuwo.cn/",
        url=lambda keyword, limit: (
            f"https://search.kuwo.cn/r.s?all={quote(keyword, safe='')}"
            f"&ft=music&itemset=web_2013&client=kt&pn=0&rn={limit}&rformat=json&encoding=utf8"),
        rows_path=("abslist",), read=_kw_read, id_pattern=_SAFE_ID),
    # NetEase answers the documented ``{"result": {"songs": [...]}}`` envelope
    # only on ``/api/search/get``.  Measured 2026-09-20: the ``/web`` suffix
    # this module used to ask answers a 36 KiB body whose ``result`` is one hex
    # string -- an encrypted envelope the row reader below can take nothing out
    # of, which is why every wy column was empty -- and ``/api/cloudsearch/pc``
    # answers the newer ``ar``/``al`` row shape this reader does not read.  The
    # suffix is the whole difference: one query parsed twenty rows.
    "wy": _Platform(
        name="wy", host="music.163.com", referer="https://music.163.com/",
        url=lambda keyword, limit: (
            f"https://music.163.com/api/search/get?s={quote(keyword, safe='')}"
            f"&type=1&offset=0&limit={limit}"),
        rows_path=("result", "songs"), read=_wy_read, id_pattern=_SAFE_ID),
    "tx": _Platform(
        name="tx", host="c.y.qq.com", referer="https://y.qq.com/",
        url=lambda keyword, limit: (
            f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w={quote(keyword, safe='')}"
            f"&format=json&n={limit}&p=1"),
        rows_path=("data", "song", "list"), read=_tx_read, id_pattern=_SAFE_ID),
    # Measured 2026-09-20 from this deployment.  This host answers a well-formed
    # envelope whose ``data.total`` is 0 and whose ``lists`` is empty for a
    # query it holds hundreds of rows for: twelve queries drawn once produced
    # rows four times, and forty spaced draws of eight of those queries
    # produced rows fourteen times, at 0.8-3.1 s each.  An empty envelope here
    # is therefore not evidence of an empty catalogue, and reading it as one is
    # what left every kg column of the download matrix blank.
    #
    # The draws are made one after another because drawing three at once was
    # measured to answer *less*: five of those same twelve queries, against
    # eight for three in a row.  A per-request random id, a larger parameter
    # set, and the signed ``complexsearch`` endpoint were each measured and
    # none of them changed the answer.
    #
    # Kuwo, NetEase, and QQ answered six of six queries with the same text and
    # never answered empty, so a second request to them would be load on the
    # upstream and nothing else.  They keep a single draw.
    "kg": _Platform(
        name="kg", host="songsearch.kugou.com", referer="https://www.kugou.com/",
        url=lambda keyword, limit: (
            f"https://songsearch.kugou.com/song_search_v2?keyword={quote(keyword, safe='')}"
            f"&page=1&pagesize={limit}"),
        rows_path=("data", "lists"), read=_kg_read, id_pattern=_HEX_ID, attempts=3),
}


def parse_hits(platform: str, payload: Any) -> tuple[PlatformHit, ...]:
    """Read one platform's answer, dropping rows that cannot become a candidate.

    A row is dropped -- never repaired -- when its id is missing or carries a
    character that would change what ``lx:<platform>:<id>`` means, when its
    title or artist is empty, or when its stated duration is that of an
    excerpt.  A payload of the wrong shape yields nothing rather than an
    exception: one platform answering nonsense must not fail the search.
    """
    spec = _PLATFORMS.get(platform)
    if spec is None or not isinstance(payload, Mapping):
        return ()
    hits: list[PlatformHit] = []
    for row in _rows_of(payload, spec.rows_path):
        if not isinstance(row, Mapping):
            continue
        try:
            raw_id, raw_title, raw_artist, raw_album, raw_duration, per = spec.read(row)
        except Exception:  # noqa: BLE001 -- one malformed row is not a failed search
            continue
        song_id = raw_id if isinstance(raw_id, str) else ("" if raw_id is None else str(raw_id))
        song_id = song_id.strip()
        if not spec.id_pattern.fullmatch(song_id):
            continue
        title, artist = _text(raw_title), _text(raw_artist)
        if not title or not artist:
            continue
        duration = _duration_or_drop(raw_duration, per=per or 1)
        if duration is False:
            continue
        hits.append(PlatformHit(platform=platform, song_id=song_id, title=title, artist=artist,
                                album=_text(raw_album) or None, duration=duration))
    return tuple(hits)


def decode_body(body: str) -> Any:
    """Decode one broker observation body, tolerating kuwo's object literal.

    Only the quoting is repaired, and only for a body that is not JSON: in a
    dialect whose strings are single-quoted, an apostrophe inside a value is
    escaped (``\\u0027``), so a rewritten delimiter cannot silently change a
    value.  A body that parses under neither reading raises, and the caller
    treats that platform as unanswered.
    """
    raw = base64.b64decode(body, validate=True)
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(text.replace("'", '"'))


class PlatformSearch:
    """The main process's own search, shared by every installed lx source."""

    def __init__(self, broker: HttpsActionBroker | None = None, *, timeout: float = 8.0,
                 limit: int = DEFAULT_LIMIT, platforms: Iterable[str] = PLATFORMS,
                 max_cached_queries: int = 4):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid_timeout")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError("invalid_limit")
        if isinstance(max_cached_queries, bool) or not isinstance(max_cached_queries, int) or max_cached_queries < 1:
            raise ValueError("invalid_cache_size")
        unknown = [name for name in platforms if name not in _PLATFORMS]
        if unknown:
            raise ValueError("unknown_platform")
        self.broker = broker or HttpsActionBroker(timeout=timeout)
        self.timeout = float(timeout)
        self.limit = limit
        self.platforms = tuple(platforms)
        self.max_cached_queries = max_cached_queries
        self._pending: OrderedDict[tuple[str, str], asyncio.Future] = OrderedDict()

    async def hits(self, platform: str, query: str) -> tuple[PlatformHit, ...]:
        """One platform's hits for one query, fetched once however many ask."""
        if platform not in self.platforms:
            raise PlatformSearchError("unknown_platform")
        key = (platform, query)
        pending = self._pending.get(key)
        if pending is None:
            pending = asyncio.ensure_future(self._fetch(platform, query))
            # An unawaited failure is still a failure the caller must see, and
            # a task nobody reads would report it only at garbage collection.
            pending.add_done_callback(_consume)
            self._pending[key] = pending
            while len(self._pending) > self.max_cached_queries:
                self._pending.popitem(last=False)
        # A cancelled caller must not cancel the fetch the others are waiting
        # for; the shared call finishes once for the whole search round.
        return await asyncio.shield(pending)

    async def _fetch(self, platform: str, query: str) -> tuple[PlatformHit, ...]:
        spec = _PLATFORMS[platform]
        action = HttpAction(action_id=f"search-{spec.name}", url=spec.url(query, self.limit),
                            headers={"Referer": spec.referer})
        # Every draw of one platform's query shares this one budget, so asking
        # a platform again -- which only happens where an empty envelope was
        # measured to be unreliable -- cannot make the platform as a whole
        # outlive the timeout its caller set.
        deadline = time.monotonic() + self.timeout
        for attempt in range(spec.attempts):
            remaining = deadline - time.monotonic()
            timeout = self.timeout if spec.attempts == 1 else remaining / (spec.attempts - attempt)
            try:
                observation = await asyncio.to_thread(self.broker.fetch, action, SEARCH_HOSTS,
                                                      timeout=timeout)
            except ActionDenied as exc:
                # Running out of time on one draw is one more reason to make
                # the next one, if there is a next one left to make.
                if exc.code == "timeout" and attempt + 1 < spec.attempts:
                    continue
                raise PlatformSearchError(f"search_{exc.code}") from exc
            except PlatformSearchError:
                raise
            except Exception as exc:  # noqa: BLE001 -- every transport failure is one outcome
                raise PlatformSearchError("search_failed") from exc
            if not 200 <= int(observation.status_code) < 300:
                raise PlatformSearchError("search_failed")
            try:
                payload = decode_body(observation.body)
            except Exception as exc:  # noqa: BLE001 -- a non-JSON answer is an empty answer
                raise PlatformSearchError("search_unreadable") from exc
            hits = parse_hits(platform, payload)
            if hits or attempt + 1 == spec.attempts:
                return hits
        return ()


def _consume(task: asyncio.Future) -> None:
    """Retrieve a shared fetch's outcome so a cancelled search reports nothing."""
    if not task.cancelled():
        task.exception()


class LxSearchAdapter:
    """The search half of one installed lx source.

    The source's own code never runs here.  Its plugin id and version are the
    identity every candidate carries, and the item id is the one its shim
    decodes back, so ``resolve`` still goes to the source that will answer it.
    """

    # Every adapter of this kind answers from the one catalogue search the main
    # process owns, rather than from an index of its own.  The portal reads this
    # to report that one shared search instead of once per installed channel.
    catalogue_shared = True

    def __init__(self, source_id: str, source_version: str, search: PlatformSearch, *,
                 platforms: Iterable[str] = PLATFORMS, limit: int = MAX_CANDIDATES):
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("invalid_source_id")
        if not isinstance(source_version, str) or not source_version:
            raise ValueError("invalid_source_version")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDIDATES:
            raise ValueError("invalid_limit")
        self.source_id = source_id
        self.source_version = source_version
        # Named ``engine`` rather than ``search``: an instance attribute of that
        # name would shadow the method below, and the registry's own
        # ``callable(getattr(source, "search", None))`` check would then refuse
        # the adapter as an invalid one.
        self.engine = search
        self.platforms = tuple(platforms)
        self.limit = limit

    async def search(self, query: str) -> tuple[Candidate, ...]:
        query = normalize_text(query)
        if not query or len(query) > 500:
            raise ValueError("invalid_query")
        outcomes = await asyncio.gather(*(self.engine.hits(platform, query) for platform in self.platforms),
                                        return_exceptions=True)
        answered = [outcome for outcome in outcomes
                    if not isinstance(outcome, BaseException)]
        if outcomes and not answered:
            first = next((outcome for outcome in outcomes if isinstance(outcome, BaseException)), None)
            code = getattr(first, "code", "search_unavailable") if first is not None else "search_unavailable"
            raise PlatformSearchError(code)
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for hits in answered:
            for hit in hits:
                if hit.item_id in seen:
                    continue
                seen.add(hit.item_id)
                if len(candidates) >= self.limit:
                    return tuple(candidates)
                candidates.append(Candidate(
                    source_id=self.source_id, source_version=self.source_version, item_id=hit.item_id,
                    title=hit.title, artist=hit.artist, album=hit.album, duration=hit.duration,
                    platform=hit.platform,
                    # Quality is what the resolve step negotiates, not what a
                    # search listing advertises, so nothing here claims a
                    # container the download has not been asked for yet.
                    bitrate=None, format=None, size=None))
        return tuple(candidates)
