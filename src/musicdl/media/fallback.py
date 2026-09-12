from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult

from .download import download_candidate
from .models import MAX_MEDIA_BYTES, DownloadEvent, DownloadResult, DownloadSource, FallbackResult, MediaError


async def download_with_fallback(
    candidate: Candidate,
    sources: Mapping[str, DownloadSource],
    media_root: str | Path,
    *,
    request_id: str,
    query: str,
    refresh: Callable[[str, frozenset[str]], Awaitable[SearchResult]],
    language: str | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
    record: Callable[[DownloadEvent], None] | None = None,
) -> FallbackResult:
    source = sources.get(candidate.source_id)
    failed_source = candidate.source_id
    if source is None:
        download_error = "source_unavailable"
        if record:
            record(DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                 "download", "failed", error_code=download_error))
    else:
        try:
            downloaded = await download_candidate(candidate, source, media_root, request_id=request_id,
                                                  language=language, max_bytes=max_bytes, record=record)
            return FallbackResult(download=downloaded)
        except asyncio.CancelledError:
            raise
        except MediaError as exc:
            download_error = exc.code

    refreshed: SearchResult | None = None
    refresh_error: str | None = None
    try:
        refreshed_value = await refresh(query, frozenset({failed_source}))
        if any(item.source_id == failed_source for item in refreshed_value.candidates):
            refresh_error = "refresh_included_failed_source"
            if record:
                record(DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                     "refresh", "failed", error_code=refresh_error))
        else:
            refreshed = refreshed_value
            if record:
                record(DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                     "refresh", "success"))
    except asyncio.CancelledError:
        raise
    except Exception:
        refresh_error = "refresh_failed"
        if record:
            record(DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                 "refresh", "failed", error_code=refresh_error))

    healthy: bool | None
    if source is None:
        healthy = None
        if record:
            record(DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                 "health", "unavailable", error_code="source_unavailable", healthy=None))
    else:
        try:
            healthy = await source.health()
            if record:
                record(DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                     "health", "success", healthy=healthy))
        except asyncio.CancelledError:
            raise
        except Exception:
            healthy = None
            if record:
                record(DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                     "health", "failed", error_code="health_failed", healthy=None))
    return FallbackResult(refreshed=refreshed, failed_source_id=failed_source,
                          download_error=download_error, refresh_error=refresh_error, healthy=healthy)
