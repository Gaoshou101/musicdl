from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from dataclasses import replace

from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult

from .download import download_candidate, source_download, _close_metadata
from musicdl.sources.quality import is_lossless, proven_lossy, requested_quality, quality_rank
from .models import (
    MAX_MEDIA_BYTES,
    ArtifactRecord,
    ArtifactStore,
    DownloadEvent,
    DownloadMetadata,
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


async def _resolve_media(source: DownloadSource, candidate: Candidate, quality: str | None, *,
                         request_id: str,
                         record: Callable[[DownloadEvent], None] | None) -> DownloadMetadata:
    """Resolve one descriptor the way ``download_candidate`` reports a dead channel.

    The pre-resolve and the re-resolve happen in this module rather than inside
    ``download_candidate``, so a resolver that fails in a way that function would have
    normalised has to be normalised here too.  A channel that could not answer is the
    business failure the refresh path replaces, never an uncertain effect that leaves
    the job to be retried and dead-lettered instead of re-prompted; and because
    ``download_candidate`` is not the one reporting it, the attempt is put on the event
    stream here so a channel's roll-up still counts the failure.  A ``MediaError`` keeps
    its own code and the caller's existing classification, with no second event.
    """
    try:
        return await source_download(source, candidate, quality=quality)
    except MediaError:
        raise
    except Exception:
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id,
                                         candidate.source_version, "download", "failed",
                                         error_code="download_failed",
                                         requested_quality=quality))
        raise MediaError("download_failed") from None


async def download_with_fallback(
    candidate: Candidate,
    sources: Mapping[str, DownloadSource],
    media_root: str | Path,
    *,
    request_id: str,
    quality: str | None = None,
    quality_policy: str = "lossless_first",
    max_quality_switches: int = 1,
    prepare: Callable | None = None,
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
    verify_duration: str = "lenient",
    record: Callable[[DownloadEvent], None] | None = None,
) -> FallbackResult:
    if verify_duration not in {"lenient", "strict"}:
        raise ValueError("invalid_verify_duration")
    resolve_stream_budget = _budget(resolve_stream_timeout)
    refresh_budget = _budget(refresh_timeout)
    health_budget = _budget(health_timeout, default=10.0)
    source = sources.get(candidate.source_id)
    failed_source = candidate.source_id
    quality_refreshed: SearchResult | None = None
    if source is None:
        download_error = "source_unavailable"
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                             "download", "failed", error_code=download_error))
    else:
        try:
            async def attempt():
                nonlocal quality_refreshed
                started = asyncio.get_running_loop().time()
                common = dict(request_id=request_id, reservation=reservation,
                              artifact_store=artifact_store, owner=owner, fence=fence,
                              language=language, max_bytes=max_bytes, verify_duration=verify_duration,
                              record=record, prepare=prepare)

                async def stream(source_obj, target, requested, resolved):
                    """Stream a descriptor this attempt already holds.

                    A descriptor whose stated lifetime has passed is never
                    streamed: the URL is retired, so the answer is resolved
                    again and the fresh one is used instead.
                    """
                    try:
                        return await download_candidate(target, source_obj, media_root, quality=requested,
                                                        resolved_metadata=resolved, **common)
                    except MediaError as exc:
                        if exc.code != "media_url_expired":
                            raise
                    await _close_metadata(resolved)
                    fresh = await _resolve_media(source_obj, target, requested,
                                               request_id=request_id, record=record)
                    try:
                        return await download_candidate(target, source_obj, media_root, quality=requested,
                                                        resolved_metadata=fresh, **common)
                    finally:
                        await _close_metadata(fresh)
                if quality_policy != "lossless_first" or not is_lossless(quality) or reservation is not None:
                    return await download_candidate(candidate, source, media_root, quality=quality, **common)
                metadata = await _resolve_media(source, candidate, quality,
                                               request_id=request_id, record=record)
                try:
                    downgraded = proven_lossy(metadata.quality)
                    if downgraded:
                        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id,
                                   candidate.source_version, "quality_downgraded", "observed",
                                   requested_quality=quality, actual_quality=metadata.quality))
                    if downgraded and max_quality_switches > 0:
                        try:
                            # Share the enclosing resolve/stream budget and leave time to retain
                            # the original stream if the alternate cannot supply a usable file.
                            remaining = (resolve_stream_budget or 15.0) - (asyncio.get_running_loop().time() - started)
                            async with asyncio.timeout(max(0.001, remaining / 2)):
                                if refresh_budget is None:
                                    quality_refreshed = await refresh(query, frozenset({candidate.source_id}))
                                else:
                                    async with asyncio.timeout(refresh_budget):
                                        quality_refreshed = await refresh(query, frozenset({candidate.source_id}))
                                matches = [item for item in quality_refreshed.candidates
                                           if item.canonical_version_key == candidate.canonical_version_key
                                           and item.source_id != candidate.source_id and item.source_id in sources]
                                if matches:
                                    alternate = matches[0]
                                    alternate_quality = requested_quality(alternate, preference=quality) or quality
                                    alternate_metadata = await source_download(sources[alternate.source_id], alternate,
                                                                               quality=alternate_quality)
                                    try:
                                        lower = (quality_rank(alternate_metadata.quality) > 0 and
                                                 quality_rank(alternate_metadata.quality) < quality_rank(metadata.quality))
                                        if not lower:
                                            downloaded = await stream(sources[alternate.source_id], alternate,
                                                                      alternate_quality, alternate_metadata)
                                            return replace(downloaded,
                                                           quality_downgraded=proven_lossy(downloaded.actual_quality))
                                    finally:
                                        await _close_metadata(alternate_metadata)
                        except MediaError as exc:
                            if exc.code == "artifact_uncertain":
                                raise
                        except (TimeoutError, RuntimeError, ValueError):
                            pass
                    downloaded = await stream(source, candidate, quality, metadata)
                    # The verdict belongs to the bytes that arrive, not to the
                    # descriptor that prompted the download.  ``stream`` may
                    # have re-resolved an answer whose URL had died, and the
                    # file that comes back then is the fresh one: a source that
                    # answered 320k, retired that URL, and answered FLAC on the
                    # second resolve has downgraded nothing.  The tier read here
                    # is the one the report prints -- the source's label while the
                    # verified container can carry it, the container otherwise --
                    # so a label the file contradicts cannot hide a downgrade.
                    return replace(downloaded, quality_downgraded=proven_lossy(downloaded.actual_quality))
                finally:
                    await _close_metadata(metadata)

            if resolve_stream_budget is None:
                downloaded = await attempt()
            else:
                async with asyncio.timeout(resolve_stream_budget):
                    downloaded = await attempt()
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
        if quality_refreshed is not None:
            refreshed_value = quality_refreshed
        elif refresh_budget is None:
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
