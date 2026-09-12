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


def format_results(candidates: Iterable[Candidate], *, max_items: int = 10) -> str:
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 100:
        raise ValueError("invalid_max_items")
    lines = []
    for index, c in enumerate(candidates):
        if index >= max_items:
            break
        details = [f"{c.source_id}@{c.source_version}", f"大小 {_size(c.size)}"]
        for label, value in (("专辑", c.album), ("时长", _duration(c.duration)), ("格式", c.format), ("码率", f"{c.bitrate} kbps" if c.bitrate is not None else None)):
            if value is not None:
                details.append(f"{label} {value}")
        lines.append(f"{index + 1}. {c.title} - {c.artist}（{'；'.join(details)}）")
    return "\n".join(lines)
