from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult

from .download import download_candidate
from .models import (
    MAX_MEDIA_BYTES,
    ArtifactRecord,
    ArtifactStore,
    DownloadEvent,
    DownloadResult,
    DownloadSource,
    FallbackResult,
    MediaError,
    emit_event,
    _DOWNLOAD_CODES,
)


def _budget(value: float | None, *, default: float | None = None) -> float | None:
    """Return a validated positive budget in seconds, or the default when unset."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_timeout")
    return float(value)


async def download_with_fallback(
    candidate: Candidate,
    sources: Mapping[str, DownloadSource],
    media_root: str | Path,
    *,
    request_id: str,
    query: str,
    refresh: Callable[[str, frozenset[str]], Awaitable[SearchResult]],
    resolve_stream_timeout: float | None = None,
    refresh_timeout: float | None = None,
    health_timeout: float = 10.0,
    reservation: ArtifactRecord | None = None,
    artifact_store: ArtifactStore | None = None,
    owner: str | None = None,
    fence: int | None = None,
    language: str | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
    record: Callable[[DownloadEvent], None] | None = None,
) -> FallbackResult:
    resolve_stream_budget = _budget(resolve_stream_timeout)
    refresh_budget = _budget(refresh_timeout)
    health_budget = _budget(health_timeout, default=10.0)
    source = sources.get(candidate.source_id)
    failed_source = candidate.source_id
    if source is None:
        download_error = "source_unavailable"
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                             "download", "failed", error_code=download_error))
    else:
        try:
            if resolve_stream_budget is None:
                downloaded = await download_candidate(
                    candidate, source, media_root, request_id=request_id, reservation=reservation,
                    artifact_store=artifact_store, owner=owner, fence=fence, language=language,
                    max_bytes=max_bytes, record=record)
            else:
                async with asyncio.timeout(resolve_stream_budget):
                    downloaded = await download_candidate(
                        candidate, source, media_root, request_id=request_id, reservation=reservation,
                        artifact_store=artifact_store, owner=owner, fence=fence, language=language,
                        max_bytes=max_bytes, record=record)
            return FallbackResult(download=downloaded)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # The shared resolve+stream budget expired; the inner attempt already closed its stream.
            download_error = "media_timeout"
            emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                 "download", "failed", error_code=download_error))
        except MediaError as exc:
            download_error = exc.code if exc.code in _DOWNLOAD_CODES else "download_failed"

    refreshed: SearchResult | None = None
    refresh_error: str | None = None
    try:
        if refresh_budget is None:
            refreshed_value = await refresh(query, frozenset({failed_source}))
        else:
            async with asyncio.timeout(refresh_budget):
                refreshed_value = await refresh(query, frozenset({failed_source}))
        if any(item.source_id == failed_source for item in refreshed_value.candidates):
            refresh_error = "refresh_included_failed_source"
            emit_event(record, DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                 "refresh", "failed", error_code=refresh_error))
        else:
            refreshed = refreshed_value
            emit_event(record, DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                 "refresh", "success"))
    except asyncio.CancelledError:
        raise
    except Exception:
        refresh_error = "refresh_failed"
        emit_event(record, DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                             "refresh", "failed", error_code=refresh_error))

    healthy: bool | None
    if source is None:
        healthy = None
        emit_event(record, DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                             "health", "unavailable", error_code="source_unavailable", healthy=None))
    else:
        try:
            healthy = await asyncio.wait_for(source.health(), timeout=health_budget)
            emit_event(record, DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                 "health", "success", healthy=healthy))
        except asyncio.CancelledError:
            raise
        except Exception:
            healthy = None
            emit_event(record, DownloadEvent(request_id, candidate.item_id, failed_source, candidate.source_version,
                                 "health", "failed", error_code="health_failed", healthy=None))
    return FallbackResult(refreshed=refreshed, failed_source_id=failed_source,
                          download_error=download_error, refresh_error=refresh_error, healthy=healthy)
