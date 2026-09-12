from __future__ import annotations

from collections.abc import Iterable

from musicdl.sources.models import Candidate


def _size(value: int | None) -> str:
    if value is None:
        return "未知"
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(value)
    unit = 0
    while amount >= 1024 and unit < len(units) - 1:
        amount /= 1024
        unit += 1
    return f"{amount:.1f} {units[unit]}" if unit else f"{value} B"


def _duration(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value // 60}m {value % 60}s"


def _fit(value: str, budget: int) -> str:
    while value and len(value.encode("utf-8")) > budget:
        value = value[:-1]
    return value


def format_results(candidates: Iterable[Candidate], *, max_items: int = 10, max_bytes: int = 2048) -> str:
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 100:
        raise ValueError("invalid_max_items")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 128 <= max_bytes <= 2048:
        raise ValueError("invalid_max_bytes")
    lines = []
    for index, c in enumerate(candidates):
        if index >= max_items:
            break
        source = f"{c.source_id}@{c.source_version}"
        details = [source, f"格式 {c.format or '未知'}", f"大小 {_size(c.size)}"]
        for label, value in (("专辑", c.album), ("时长", _duration(c.duration)), ("码率", f"{c.bitrate} kbps" if c.bitrate is not None else None)):
            if value is not None:
                details.append(f"{label} {value}")
        suffix = f"（{'；'.join(details)}）"
        title, artist = c.title, c.artist
        line = f"{index + 1}. {title} - {artist}{suffix}"
        while len(("\n".join(lines + [line])).encode("utf-8")) > max_bytes and (len(title) > 1 or len(artist) > 1):
            if len(title.encode("utf-8")) >= len(artist.encode("utf-8")) and len(title) > 1:
                title = _fit(title, max(1, len(title.encode("utf-8")) - 4))
            elif len(artist) > 1:
                artist = _fit(artist, max(1, len(artist.encode("utf-8")) - 4))
            line = f"{index + 1}. {title} - {artist}{suffix}"
        if len(("\n".join(lines + [line])).encode("utf-8")) > max_bytes:
            break
        lines.append(line)
    return "\n".join(lines)
