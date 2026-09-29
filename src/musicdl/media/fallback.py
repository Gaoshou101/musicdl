from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from dataclasses import replace

from musicdl.sources.models import Candidate, normalize_text
from musicdl.sources.search import SearchResult

from .download import download_candidate, source_download, _close_metadata
from musicdl.sources.quality import is_lossless, proven_lossy, requested_quality, quality_rank
from .models import (
    MAX_MEDIA_BYTES,
    ArtifactRecord,
    ArtifactStore,
    DownloadEvent,
    DownloadMetadata,
    DownloadSource,
    DownloadResult,
    FallbackResult,
    MediaError,
    emit_event,
    _DOWNLOAD_CODES,
)


MAX_CHANNEL_SWITCHES = 3
CONTENT_FAILURE_CODES = frozenset({
    "media_response_invalid", "signature_mismatch", "mime_mismatch",
    "extension_mismatch", "size_mismatch", "incomplete_audio",
})


def _budget(value: float | None, *, default: float | None = None) -> float | None:
    """Return a validated positive budget in seconds, or the default when unset."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_timeout")
    return float(value)


def _lookup(value, source_id: str, *, default=None):
    if value is None:
        return default
    try:
        found = value(source_id) if callable(value) else value.get(source_id, default)
    except Exception:
        return default
    return found


def replacement_candidates(
    wanted: Candidate,
    candidates,
    resolvers: Mapping[str, DownloadSource],
    *,
    attempted_source_ids=(),
    preference=None,
    health_status=None,
    lossless_capability=None,
    quality_policy: str = "lossless_first",
    quality: str | None = None,
    reservation: ArtifactRecord | None = None,
    recording_match: Callable[[Candidate, Candidate], bool] | None = None,
) -> tuple[list[Candidate], dict[str, str]]:
    """Rank unused resolvers for the same recording and explain exclusions."""
    attempted = set(attempted_source_ids or ())
    skipped: dict[str, str] = {}
    grouped: dict[str, Candidate] = {}
    active_match = recording_match or (
        lambda wanted_item, other: normalize_text(wanted_item.title) == normalize_text(other.title)
        and normalize_text(wanted_item.artist) == normalize_text(other.artist))
    for item in candidates or ():
        source_id = getattr(item, "source_id", None)
        if not isinstance(source_id, str) or not source_id:
            continue
        if source_id == wanted.source_id or source_id in attempted:
            skipped.setdefault(source_id, "already_attempted")
            continue
        if source_id not in resolvers:
            skipped.setdefault(source_id, "no_resolver")
            continue
        if not active_match(wanted, item):
            skipped.setdefault(source_id, "different_recording")
            continue
        if reservation is not None and item.item_id != reservation.candidate_id:
            skipped.setdefault(source_id, "artifact_identity_mismatch")
            continue
        if reservation is not None and quality is None and item.format != wanted.format:
            skipped.setdefault(source_id, "artifact_format_mismatch")
            continue
        previous = grouped.get(source_id)
        if previous is None:
            grouped[source_id] = item
        else:
            if (wanted.duration is not None and item.duration is not None and
                    (previous.duration is None or abs(item.duration - wanted.duration) <
                     abs(previous.duration - wanted.duration))):
                grouped[source_id] = item

    use_capability = quality_policy == "lossless_first" and is_lossless(quality)

    observed = {item.source_id: _lookup(health_status, item.source_id) for item in grouped.values()}
    viable_health = any(value is not False for value in observed.values())
    for source_id, value in observed.items():
        if value is False:
            skipped[source_id] = "known_unavailable" if viable_health else "all_candidates_unavailable"
    grouped = {source_id: item for source_id, item in grouped.items() if observed[source_id] is not False}

    def order(item: Candidate):
        observed_health = observed[item.source_id]
        health_rank = 0 if observed_health is True else (2 if observed_health is False else 1)
        observed_preference = _lookup(preference, item.source_id, default=0)
        if isinstance(observed_preference, bool) or not isinstance(observed_preference, (int, float)):
            observed_preference = 0
        capability = _lookup(lossless_capability, item.source_id)
        if capability not in (True, False, None):
            capability = None
        capability_rank = (0 if capability is True else (2 if capability is False else 1)) if use_capability else 0
        distance = (abs(item.duration - wanted.duration)
                    if wanted.duration is not None and item.duration is not None else 0)
        return health_rank, capability_rank, -observed_preference, distance

    ranked = sorted(grouped.values(), key=order)
    return ranked, skipped


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
    except MediaError as exc:
        error_code = exc.code if exc.code in _DOWNLOAD_CODES else "download_failed"
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id,
                                         candidate.source_version, "download", "failed",
                                         error_code=error_code, requested_quality=quality))
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
    max_channel_switches: int = MAX_CHANNEL_SWITCHES,
    preference=None,
    channel_health=None,
    lossless_capability=None,
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
    if (isinstance(max_channel_switches, bool) or not isinstance(max_channel_switches, int)
            or max_channel_switches < 0 or max_channel_switches > MAX_CHANNEL_SWITCHES):
        raise ValueError("invalid_max_channel_switches")
    source = sources.get(candidate.source_id)
    failed_source = candidate.source_id
    original_candidate = candidate
    attempted_sources = {candidate.source_id}
    switch_count = 0
    successful_source = candidate.source_id
    quality_refreshed: SearchResult | None = None
    channel_refreshed: SearchResult | None = None
    content_refresh_attempted = False
    if source is None:
        download_error = "source_unavailable"
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                             "download", "failed", error_code=download_error))
    else:
        try:
            async def attempt(target: Candidate, source_obj: DownloadSource, *, allow_quality_switch: bool = True,
                              record_callback=None):
                nonlocal quality_refreshed, switch_count, successful_source
                started = asyncio.get_running_loop().time()
                attempt_record = record if record_callback is None else record_callback
                common = dict(request_id=request_id, reservation=reservation,
                              artifact_store=artifact_store, owner=owner, fence=fence,
                              language=language, max_bytes=max_bytes, verify_duration=verify_duration,
                              record=attempt_record, prepare=prepare)

                async def stream(stream_source, stream_target, requested, resolved):
                    nonlocal successful_source
                    """Stream a descriptor this attempt already holds.

                    A descriptor whose stated lifetime has passed is never
                    streamed: the URL is retired, so the answer is resolved
                    again and the fresh one is used instead.
                    """
                    try:
                        result = await download_candidate(stream_target, stream_source, media_root, quality=requested,
                                                        resolved_metadata=resolved, **common)
                        successful_source = stream_target.source_id
                        return result
                    except MediaError as exc:
                        if exc.code != "media_url_expired":
                            raise
                    await _close_metadata(resolved)
                    fresh = await _resolve_media(stream_source, stream_target, requested,
                                               request_id=request_id, record=attempt_record)
                    try:
                        result = await download_candidate(stream_target, stream_source, media_root, quality=requested,
                                                          resolved_metadata=fresh, **common)
                        successful_source = stream_target.source_id
                        return result
                    finally:
                        await _close_metadata(fresh)
                # ``quality`` is the request the caller selected for the
                # original candidate. Preserve it verbatim: ``None`` is a
                # real choice (notably under ``best_available``), and deriving
                # a tier for the first source here changes the resolver path.
                # A replacement may advertise different tiers, so choose its
                # ask from its own declaration using the original ask as the
                # preference.
                target_quality = quality
                if target.source_id != candidate.source_id:
                    target_quality = (requested_quality(target, policy=quality_policy,
                                                        preference=quality) or quality)
                if (quality_policy != "lossless_first" or not is_lossless(target_quality)
                        or reservation is not None):
                    result = await download_candidate(target, source_obj, media_root,
                                                      quality=target_quality, **common)
                    successful_source = target.source_id
                    return result
                metadata = await _resolve_media(source_obj, target, target_quality,
                                               request_id=request_id, record=attempt_record)
                try:
                    downgraded = proven_lossy(metadata.quality)
                    if downgraded:
                        emit_event(record, DownloadEvent(request_id, target.item_id, target.source_id,
                                   target.source_version, "quality_downgraded", "observed",
                                   requested_quality=target_quality, actual_quality=metadata.quality))
                    if (downgraded and allow_quality_switch and max_quality_switches > 0
                            and switch_count < max_channel_switches and reservation is None):
                        try:
                            # Share the enclosing resolve/stream budget and leave time to retain
                            # the original stream if the alternate cannot supply a usable file.
                            remaining = (resolve_stream_budget or 15.0) - (asyncio.get_running_loop().time() - started)
                            async with asyncio.timeout(max(0.001, remaining / 2)):
                                if refresh_budget is None:
                                    quality_refreshed = await refresh(query, frozenset(attempted_sources))
                                else:
                                    async with asyncio.timeout(refresh_budget):
                                        quality_refreshed = await refresh(query, frozenset(attempted_sources))
                                matches, skipped = replacement_candidates(
                                    target, quality_refreshed.candidates, sources,
                                    attempted_source_ids=attempted_sources, preference=preference,
                                    health_status=channel_health, lossless_capability=lossless_capability,
                                    quality_policy=quality_policy, quality=target_quality)
                                if matches:
                                    alternate = matches[0]
                                    alternate_quality = requested_quality(alternate, policy=quality_policy,
                                                                          preference=target_quality) or target_quality
                                    attempted_sources.add(alternate.source_id)
                                    switch_count += 1
                                    emit_event(record, DownloadEvent(
                                        request_id, target.item_id, target.source_id, target.source_version,
                                        "channel_switch", "selected", from_source_id=target.source_id,
                                        to_source_id=alternate.source_id, reason="quality_downgrade",
                                        skipped_sources=skipped))
                                    alternate_metadata = await _resolve_media(
                                        sources[alternate.source_id], alternate, alternate_quality,
                                        request_id=request_id, record=record)
                                    try:
                                        lower = (quality_rank(alternate_metadata.quality) > 0 and
                                                 quality_rank(alternate_metadata.quality) < quality_rank(metadata.quality))
                                        if not lower:
                                            downloaded = await stream(sources[alternate.source_id], alternate,
                                                                      alternate_quality, alternate_metadata)
                                            successful_source = alternate.source_id
                                            return replace(downloaded,
                                                           quality_downgraded=proven_lossy(downloaded.actual_quality))
                                    finally:
                                        await _close_metadata(alternate_metadata)
                        except MediaError as exc:
                            if exc.code == "artifact_uncertain":
                                raise
                        except (TimeoutError, RuntimeError, ValueError):
                            pass
                    downloaded = await stream(source_obj, target, target_quality, metadata)
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
                downloaded = await attempt(candidate, source)
            else:
                async with asyncio.timeout(resolve_stream_budget):
                    downloaded = await attempt(candidate, source)
            return FallbackResult(download=downloaded, download_source_id=successful_source,
                                  attempted_source_ids=tuple(sorted(attempted_sources)),
                                  channel_switches=switch_count)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # The shared resolve+stream budget expired; the inner attempt already closed its stream.
            download_error = "media_timeout"
            emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                 "download", "failed", error_code=download_error))
        except MediaError as exc:
            download_error = exc.code if exc.code in _DOWNLOAD_CODES else "download_failed"

    if (quality_policy == "lossless_first" and is_lossless(quality)
            and download_error in CONTENT_FAILURE_CODES and switch_count < max_channel_switches):
        content_refresh_attempted = quality_refreshed is None
        try:
            if quality_refreshed is not None:
                channel_refreshed = quality_refreshed
            elif refresh_budget is None:
                channel_refreshed = await refresh(query, frozenset(attempted_sources))
            else:
                async with asyncio.timeout(refresh_budget):
                    channel_refreshed = await refresh(query, frozenset(attempted_sources))
        except asyncio.CancelledError:
            raise
        except Exception:
            channel_refreshed = None

        current = original_candidate
        while channel_refreshed is not None and switch_count < max_channel_switches:
            ranked, skipped = replacement_candidates(
                current, channel_refreshed.candidates, sources,
                attempted_source_ids=attempted_sources, preference=preference,
                health_status=channel_health, lossless_capability=lossless_capability,
                quality_policy=quality_policy, quality=quality, reservation=reservation)
            if not ranked:
                break
            alternate = ranked[0]
            prior_error = download_error
            attempted_sources.add(alternate.source_id)
            switch_count += 1
            emit_event(record, DownloadEvent(
                request_id, current.item_id, current.source_id, current.source_version,
                "channel_switch", "selected", from_source_id=current.source_id,
                to_source_id=alternate.source_id, reason=f"content_error:{prior_error}",
                skipped_sources=skipped))
            current = alternate
            alternate_quality = (requested_quality(alternate, policy=quality_policy,
                                                   preference=quality) or quality)
            attempt_failures: list[DownloadEvent] = []

            def record_attempt(event: DownloadEvent) -> None:
                if event.stage == "download" and event.status == "failed":
                    attempt_failures.append(event)
                else:
                    emit_event(record, event)

            def finish_attempt_failure(code: str) -> None:
                if attempt_failures:
                    # A timed-out stream first reports cancellation. Replace
                    # that lower-level event with the final bounded outcome.
                    event = replace(attempt_failures[0], error_code=code,
                                    requested_quality=(attempt_failures[0].requested_quality
                                                       or alternate_quality))
                else:
                    event = DownloadEvent(
                        request_id, alternate.item_id, alternate.source_id, alternate.source_version,
                        "download", "failed", error_code=code, requested_quality=alternate_quality)
                emit_event(record, event)

            try:
                if resolve_stream_budget is None:
                    downloaded = await attempt(alternate, sources[alternate.source_id],
                                               allow_quality_switch=False, record_callback=record_attempt)
                else:
                    async with asyncio.timeout(resolve_stream_budget):
                        downloaded = await attempt(alternate, sources[alternate.source_id],
                                                   allow_quality_switch=False, record_callback=record_attempt)
            except asyncio.CancelledError:
                for event in attempt_failures:
                    emit_event(record, event)
                raise
            except TimeoutError:
                download_error = "media_timeout"
                finish_attempt_failure(download_error)
                break
            except MediaError as exc:
                download_error = exc.code if exc.code in _DOWNLOAD_CODES else "download_failed"
                finish_attempt_failure(download_error)
                if download_error not in CONTENT_FAILURE_CODES:
                    break
            else:
                return FallbackResult(download=downloaded, download_source_id=successful_source,
                                      attempted_source_ids=tuple(sorted(attempted_sources)),
                                      channel_switches=switch_count)

    refreshed: SearchResult | None = None
    refresh_error: str | None = None
    try:
        if channel_refreshed is not None:
            refreshed_value = channel_refreshed
        elif quality_refreshed is not None:
            refreshed_value = quality_refreshed
        elif content_refresh_attempted:
            raise RuntimeError("refresh_failed")
        elif refresh_budget is None:
            refreshed_value = await refresh(query, frozenset({failed_source}))
        else:
            async with asyncio.timeout(refresh_budget):
                refreshed_value = await refresh(query, frozenset({failed_source}))
        if (any(item.source_id == failed_source for item in refreshed_value.candidates)
                and not (download_error in CONTENT_FAILURE_CODES and channel_refreshed is not None)):
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
                          download_error=download_error, refresh_error=refresh_error, healthy=healthy,
                          attempted_source_ids=tuple(sorted(attempted_sources)),
                          channel_switches=switch_count)
