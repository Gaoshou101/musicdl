from __future__ import annotations

import re

from collections.abc import Iterable, Sequence
from itertools import islice

from musicdl.media.models import DownloadResult
from musicdl.sources.quality import format_bytes, bitrate_kbps
from musicdl.sources.models import Candidate, normalize_text


# The four catalogues the supplied lx sources resolve against, and the name a
# person reads.  A row that came out of a channel's own index carries no
# platform at all, and a catalogue nobody has mapped yet keeps its own token:
# an unreadable name still beats hiding where the bytes will come from.
PLATFORM_NAMES = {"kg": "酷狗", "kw": "酷我", "tx": "QQ音乐", "wy": "网易云"}

# A candidate is a compact two-line card: identity first, catalogue details second.
TITLE_ARTIST = " — "
META_SEPARATOR = " · "
SELECTION_PROMPT = "\n\n回复序号下载。"
NO_RESULTS_TEXT = "没有找到匹配结果。"
# A query somebody pasted by mistake may not spend the whole reply on itself.
HEADER_QUERY_LIMIT = 40
# What a header has to leave behind: the smallest listing ``format_results``
# will render at all.
MIN_LISTING_BYTES = 128
_ELLIPSIS = "…"
_VERSION_MARKER = re.compile(
    r"(?i)\b(?:live(?:\s+(?:version|recording))?|cover|instrumental(?:\s+version)?)\b"
    r"|现场(?:演唱会)?(?:版)?|翻唱(?:版)?|伴奏(?:版)?|纯音乐"
)


def platform_name(value: str | None) -> str:
    """One catalogue token as a person reads it, and anything else unchanged."""
    if not value:
        return ""
    return PLATFORM_NAMES.get(value.casefold(), value)


def _duration(value: int | None) -> str:
    """``4:29``, and ``1:02:03`` once a recording runs past the hour."""
    if value is None:
        return ""
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _quality(candidate: Candidate) -> str:
    """State the catalogue's advertised format without implying byte validation."""
    parts = []
    if candidate.format:
        parts.append(_safe_text(candidate.format).upper())
    if candidate.bitrate is not None:
        parts.append(f"{candidate.bitrate} kbps")
    return f"标称 {' '.join(parts)}" if parts else ""


def quality_summary(result) -> str:
    """Show the requested tier beside the tier verified from delivered bytes."""
    requested = getattr(result, "requested_quality", None)
    actual = getattr(result, "actual_quality", None)
    requested = requested.strip().upper() if isinstance(requested, str) and requested.strip() else None
    actual = actual.strip().upper() if isinstance(actual, str) and actual.strip() else None
    if requested and actual:
        return f"音质 {requested}→{actual}"
    if requested:
        return f"请求音质 {requested}"
    if actual:
        return f"实际音质 {actual}"
    return ""


def success_message(result: DownloadResult) -> str:
    """Describe the bytes actually downloaded, using their final container."""
    label = result.extension.lstrip(".").upper() or result.media_type
    bitrate = bitrate_kbps(getattr(result, "quality", None))
    if bitrate is not None:
        label += f" {bitrate}kbps"
    downgrade = "未取到无损" if getattr(result, "quality_downgraded", False) else ""
    details = " · ".join(part for part in
                         (label, format_bytes(result.size_bytes), downgrade, quality_summary(result)) if part)
    return f"下载成功：{result.relative_path}（{details}）"


def _album(candidate: Candidate, *, useful: bool) -> str:
    """Include an album only when it distinguishes repeated title/artist rows."""
    album = _safe_text(candidate.album) if candidate.album else ""
    title = _safe_text(candidate.title)
    if not useful or not album or album.casefold() == title.casefold():
        return ""
    return f"专辑《{album}》"


def _row(index: int, title: str, artist: str, meta: Sequence[str]) -> str:
    identity = f"{index}. {title}{TITLE_ARTIST}{artist}"
    return identity + ("\n  " + META_SEPARATOR.join(meta) if meta else "")


def _safe_text(value: str) -> str:
    """Replace invalid UTF-16 surrogate code points before measuring/sending UTF-8."""
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Clip at Unicode code-point boundaries and visibly mark the omitted tail."""
    value = _safe_text(value)
    if _byte_length(value) <= max_bytes:
        return value
    marker = _ELLIPSIS if max_bytes >= _byte_length(_ELLIPSIS) else "."
    content_budget = max(0, max_bytes - _byte_length(marker))
    result: list[str] = []
    used = 0
    for char in value:
        size = _byte_length(char)
        if used + size > content_budget:
            break
        result.append(char)
        used += size
    return "".join(result) + marker


def _version_suffix(value: str) -> str:
    """Return a terminal version marker such as ``(Live)`` when present."""
    for match in reversed(tuple(_VERSION_MARKER.finditer(value))):
        tail = value[match.end():]
        if tail.strip(" \t)]】）}·.-—"):
            continue
        start = match.start()
        while start and value[start - 1] in " \t([{【（-—":
            start -= 1
        suffix = value[start:]
        if _byte_length(suffix) <= 64:
            return suffix
    return ""


def _truncate_title(value: str, max_bytes: int) -> str:
    value = _safe_text(value)
    if _byte_length(value) <= max_bytes:
        return value
    suffix = _version_suffix(value)
    suffix_bytes = _byte_length(suffix)
    if suffix and max_bytes >= suffix_bytes + _byte_length(_ELLIPSIS):
        prefix = value[:-len(suffix)]
        return _truncate_utf8(prefix, max_bytes - suffix_bytes) + suffix
    return _truncate_utf8(value, max_bytes)


def _minimum_text_budget(value: str, *, title: bool) -> int:
    value = _safe_text(value)
    if title:
        suffix = _version_suffix(value)
        if suffix:
            return min(_byte_length(value), _byte_length(suffix) + _byte_length(_ELLIPSIS))
    if not value:
        return 0
    return min(_byte_length(value), _byte_length(value[0]) + _byte_length(_ELLIPSIS))


def _fit_names(title: str, artist: str, max_bytes: int) -> tuple[str, str] | None:
    """Share a tight name budget across title and artist instead of losing one."""
    title, artist = _safe_text(title), _safe_text(artist)
    title_size, artist_size = _byte_length(title), _byte_length(artist)
    if title_size + artist_size <= max_bytes:
        return title, artist
    title_min = _minimum_text_budget(title, title=True)
    artist_min = _minimum_text_budget(artist, title=False)
    if title_min + artist_min > max_bytes:
        return None

    extra = max_bytes - title_min - artist_min
    title_room, artist_room = title_size - title_min, artist_size - artist_min
    total_room = title_room + artist_room
    title_extra = min(title_room, (extra * title_room // total_room) if total_room else 0)
    artist_extra = min(artist_room, extra - title_extra)
    leftover = extra - title_extra - artist_extra
    if leftover:
        add_title = min(leftover, title_room - title_extra)
        title_extra += add_title
        leftover -= add_title
        artist_extra += min(leftover, artist_room - artist_extra)

    return (_truncate_title(title, title_min + title_extra),
            _truncate_utf8(artist, artist_min + artist_extra))


def _bounded(value: int, low: int, high: int, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(code)
    return value


def _fit_row(index: int, candidate: Candidate, rows: Sequence[str], max_bytes: int,
             *, row_limit: int, show_album: bool = False) -> str | None:
    """Fit one numbered card, keeping identity and labelling source quality."""
    used = _byte_length("\n".join(rows)) + (1 if rows else 0)
    budget = min(row_limit, max_bytes - used)
    if budget <= 0:
        return None
    title, artist = _safe_text(candidate.title), _safe_text(candidate.artist)
    duration = _duration(candidate.duration)
    quality = _quality(candidate)
    source = platform_name(candidate.platform)
    if source:
        source = f"来源 {source}"
    album = _album(candidate, useful=show_album)
    while True:
        meta = [part for part in (duration, quality, source, album) if part]
        rendered = _row(index, title, artist, meta)
        if _byte_length(rendered) <= budget:
            return rendered
        if album:
            album = ""
            continue
        if source:
            source = ""
            continue
        fixed = _row(index, "", "", meta)
        fitted = _fit_names(title, artist, budget - _byte_length(fixed))
        if fitted is not None:
            return _row(index, *fitted, meta)
        if quality:
            quality = ""
        elif duration:
            duration = ""
        else:
            return None


def format_results(candidates: Iterable[Candidate], *, max_items: int = 10, max_bytes: int = 2048,
                   start_index: int = 1) -> str:
    """Two-line identity/details cards, bounded by UTF-8 bytes and row count."""
    _bounded(max_items, 1, 100, "invalid_max_items")
    _bounded(max_bytes, MIN_LISTING_BYTES, 2048, "invalid_max_bytes")
    _bounded(start_index, 1, 100, "invalid_start_index")
    candidates = tuple(islice(candidates, min(max_items, 101 - start_index)))
    if not candidates:
        return ""
    row_limit = max(MIN_LISTING_BYTES, max_bytes // len(candidates))
    albums: dict[tuple[str, str], set[str | None]] = {}
    for candidate in candidates:
        key = (_safe_text(candidate.title).casefold(), _safe_text(candidate.artist).casefold())
        album = _safe_text(candidate.album).casefold() if candidate.album else None
        albums.setdefault(key, set()).add(album)

    rows: list[str] = []
    for offset, candidate in enumerate(candidates):
        index = start_index + offset
        if index > 100:
            break
        key = (_safe_text(candidate.title).casefold(), _safe_text(candidate.artist).casefold())
        show_album = len(albums[key]) > 1
        row = _fit_row(index, candidate, rows, max_bytes, row_limit=row_limit, show_album=show_album)
        if row is None:
            break
        rows.append(row)
    return "\n".join(rows)


def _query_line(query: str, total: int) -> str:
    """What was searched, so a corrected phrase is visible as a corrected answer."""
    name = normalize_text(_safe_text(query))
    if not name:
        return ""
    if len(name) > HEADER_QUERY_LIMIT:
        name = name[: HEADER_QUERY_LIMIT - 1] + "…"
    return f"「{name}」找到 {total} 个结果："


def _header(query: str, total: int, notice: str) -> str:
    lines = [line for line in (notice.strip(), _query_line(query, total)) if line]
    return "\n".join(lines) + "\n\n" if lines else ""


def selection_message(query: str, candidates: Iterable[Candidate], *, max_items: int = 10,
                      max_bytes: int = 2048, notice: str = "") -> str:
    """The whole reply to one search: what was searched, the rows, the prompt.

    A search nothing answered says so on its own: there are no rows to answer
    with a number, so "回复序号下载" would only invite one that cannot arrive.
    """
    _bounded(max_items, 1, 100, "invalid_max_items")
    _bounded(max_bytes, 256, 2048, "invalid_max_bytes")
    if not isinstance(query, str):
        raise ValueError("invalid_query")
    rows = tuple(candidates)
    if not rows:
        return NO_RESULTS_TEXT
    tail = SELECTION_PROMPT
    head = _header(query, len(rows), notice)
    budget = max_bytes - len((head + tail).encode("utf-8"))
    if budget < MIN_LISTING_BYTES:
        # Whatever it has to say, the header never outranks the row it introduces.
        head = _header("", 0, notice)
        budget = max_bytes - len((head + tail).encode("utf-8"))
    if budget < MIN_LISTING_BYTES:
        raise ValueError("invalid_max_bytes")
    listing = format_results(rows, max_items=max_items, max_bytes=budget)
    return head + listing + tail if listing else NO_RESULTS_TEXT


def selection_pages(query: str, candidates: Iterable[Candidate], *, max_items: int = 10,
                    max_bytes: int = 2048, notice: str = "") -> tuple[str, ...]:
    """Build byte-bounded pages with the snapshot's original selection numbers."""
    _bounded(max_items, 1, 100, "invalid_max_items")
    _bounded(max_bytes, 512, 2048, "invalid_max_bytes")
    if not isinstance(query, str):
        raise ValueError("invalid_query")
    rows = tuple(islice(candidates, 100))
    if not rows:
        return (NO_RESULTS_TEXT,)
    plain = selection_message(query, rows, max_items=max_items, max_bytes=max_bytes, notice=notice)
    # Preserve established replies when every frozen row fits in one message.
    if len(rows) <= max_items and sum(bool(re.match(r"^[0-9]+[.] ", line))
                                       for line in plain.splitlines()) == len(rows):
        return (plain,)
    # Reserve room for the final page count before deciding page boundaries.
    tail = chr(10) * 2 + "回复序号下载；n 下一页，p 上一页，/cancel 取消选曲。"
    header = _header(query, len(rows), notice)
    reserve = len((header + "第 100/100 页" + chr(10) * 2 + tail).encode("utf-8"))
    budget = max_bytes - reserve
    pages: list[str] = []
    offset = 0
    while offset < len(rows):
        listing = format_results(rows[offset:], max_items=max_items, max_bytes=budget,
                                 start_index=offset + 1)
        if not listing:
            break
        pages.append(listing)
        offset += sum(bool(re.match(r"^[0-9]+[.] ", line)) for line in listing.splitlines())
    return tuple((header if index == 1 else _header(query, len(rows), ""))
                 + f"第 {index}/{len(pages)} 页" + chr(10) * 2 + listing + tail
                 for index, listing in enumerate(pages, 1))
