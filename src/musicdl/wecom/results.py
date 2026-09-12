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


def format_results(candidates: Iterable[Candidate], *, max_items: int = 10, max_bytes: int = 2048) -> str:
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 100:
        raise ValueError("invalid_max_items")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 128 <= max_bytes <= 2048:
        raise ValueError("invalid_max_bytes")
    lines = []
    for index, c in enumerate(candidates):
        if index >= max_items:
            break
        title, artist = c.title, c.artist
        source_id, version, file_format = c.source_id, c.source_version, c.format or "未知"

        def required() -> str:
            return f"{index + 1}. {title} - {artist}（{source_id}@{version}；格式 {file_format}；大小 {_size(c.size)}）"

        # Keep every required field non-empty, shortening the largest value safely.
        min_len = 3 if lines else 1
        while len(("\n".join(lines + [required()])).encode("utf-8")) > max_bytes:
            values = [("title", title), ("artist", artist), ("source_id", source_id), ("version", version), ("format", file_format)]
            candidates_to_trim = [(name, value) for name, value in values if len(value) > min_len]
            if not candidates_to_trim:
                break
            name, value = max(candidates_to_trim, key=lambda item: len(item[1].encode("utf-8")))
            trimmed = value[:-1]
            if name == "title": title = trimmed
            elif name == "artist": artist = trimmed
            elif name == "source_id": source_id = trimmed
            elif name == "version": version = trimmed
            else: file_format = trimmed
        line = required()
        if len(("\n".join(lines + [line])).encode("utf-8")) > max_bytes:
            break
        optional = [("专辑", c.album), ("时长", _duration(c.duration)), ("码率", f"{c.bitrate} kbps" if c.bitrate is not None else None)]
        for label, value in optional:
            if value is None:
                continue
            candidate_line = f"{line[:-1]}；{label} {value}）"
            if len(("\n".join(lines + [candidate_line])).encode("utf-8")) <= max_bytes:
                line = candidate_line
        lines.append(line)
    return "\n".join(lines)
