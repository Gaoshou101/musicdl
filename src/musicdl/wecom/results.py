from __future__ import annotations

import re

from collections.abc import Iterable, Sequence

from musicdl.media.models import DownloadResult
from musicdl.sources.quality import format_bytes, bitrate_kbps
from musicdl.sources.models import Candidate, normalize_text


# The four catalogues the supplied lx sources resolve against, and the name a
# person reads.  A row that came out of a channel's own index carries no
# platform at all, and a catalogue nobody has mapped yet keeps its own token:
# an unreadable name still beats hiding where the bytes will come from.
PLATFORM_NAMES = {"kg": "酷狗", "kw": "酷我", "tx": "QQ音乐", "wy": "网易云"}

# One row is ``1. title — artist《album》  ·  duration  ·  quality  ·  size  ·  catalogue``.
TITLE_ARTIST = " — "
META_SEPARATOR = "  ·  "
SELECTION_PROMPT = "\n\n回复序号下载。"
NO_RESULTS_TEXT = "没有找到匹配结果。"
# A query somebody pasted by mistake may not spend the whole reply on itself.
HEADER_QUERY_LIMIT = 40
# What a header has to leave behind: the smallest listing ``format_results``
# will render at all.
MIN_LISTING_BYTES = 128


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
    """``FLAC 1411kbps``, or whichever half of it the channel actually stated."""
    parts = []
    if candidate.format:
        parts.append(candidate.format.upper())
    if candidate.bitrate is not None:
        parts.append(f"{candidate.bitrate}kbps")
    return " ".join(parts)


def _size(candidate: Candidate) -> str:
    return format_bytes(candidate.size)


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


def _album(candidate: Candidate) -> str:
    """``《album》``, unless the album only repeats the song it belongs to."""
    album = candidate.album
    if not album or album.casefold() == candidate.title.casefold():
        return ""
    return f"《{album}》"


def _meta(candidate: Candidate) -> list[str]:
    """What tells two rows with one title and artist apart."""
    return [part for part in (_duration(candidate.duration), _quality(candidate),
                              _size(candidate), platform_name(candidate.platform)) if part]


def _row(index: int, title: str, artist: str, album: str, meta: Sequence[str]) -> str:
    return f"{index}. {title}{TITLE_ARTIST}{artist}{album}" + "".join(
        META_SEPARATOR + part for part in meta)

def _bounded(value: int, low: int, high: int, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(code)
    return value


def _fits(rows: Sequence[str], row: str, max_bytes: int) -> bool:
    return len("\n".join((*rows, row)).encode("utf-8")) <= max_bytes


def _fit_row(index: int, candidate: Candidate, rows: Sequence[str], max_bytes: int) -> str | None:
    """One row, giving up what it can spare until it fits the budget.

    What one row can spare, in the order it gives it up: the catalogue, the
    size, the quality, the running time, the album, and only then a character of
    the title or the artist, the two fields a row cannot lose.  A row that cannot fit even
    then is not rendered -- and neither is anything after it, because a listing
    with a hole in its numbering reads as the wrong answer to the number above
    it.
    """
    title, artist = candidate.title, candidate.artist
    album, meta = _album(candidate), _meta(candidate)

    def render() -> str:
        return _row(index, title, artist, album, meta)

    while not _fits(rows, render(), max_bytes):
        if meta:
            meta.pop()
        elif album:
            album = ""
        elif len(title) > 1 or len(artist) > 1:
            longest = max((title, artist), key=lambda value: len(value.encode("utf-8")))
            if longest == title:
                title = title[:-1]
            else:
                artist = artist[:-1]
        else:
            return None
    return render()


def format_results(candidates: Iterable[Candidate], *, max_items: int = 10, max_bytes: int = 2048,
                   start_index: int = 1) -> str:
    """One short line per candidate, never more than ``max_bytes`` of UTF-8.

    A row leads with what a person chooses by -- the title, the artist, the
    album -- and spends whatever room is left on the running time, the quality,
    the size and the catalogue.  A field the channel did not state is left out instead
    of printed as ``未知``: ten rows of one catalogue's placeholders is what
    made the reply unreadable.
    """
    _bounded(max_items, 1, 100, "invalid_max_items")
    _bounded(max_bytes, MIN_LISTING_BYTES, 2048, "invalid_max_bytes")
    _bounded(start_index, 1, 100, "invalid_start_index")
    rows: list[str] = []
    for offset, candidate in enumerate(candidates):
        index = start_index + offset
        if offset >= max_items or index > 100:
            break
        row = _fit_row(index, candidate, rows, max_bytes)
        if row is None:
            break
        rows.append(row)
    return "\n".join(rows)


def _query_line(query: str, total: int) -> str:
    """What was searched, so a corrected phrase is visible as a corrected answer."""
    name = normalize_text(query)
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
    rows = tuple(candidates)[:100]
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
        offset += len(listing.splitlines())
    return tuple((header if index == 1 else _header(query, len(rows), ""))
                 + f"第 {index}/{len(pages)} 页" + chr(10) * 2 + listing + tail
                 for index, listing in enumerate(pages, 1))
