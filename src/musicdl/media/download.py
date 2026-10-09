from __future__ import annotations

import asyncio
import errno
import functools
import hashlib
import inspect
import os
import stat
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

from musicdl.contracts.plugin import has_expired
from musicdl.sources.models import Candidate
from musicdl.sources.quality import served_quality
from .admission import (
    ADMISSION_BLOCKED_CODE,
    ADMISSION_TIMEOUT_CODE,
    AdmissionPermit,
    AdmissionRejected,
    AdmissionTimeout,
)
from .history import trace_download_stage

from .models import (
    MAX_MEDIA_BYTES,
    ArtifactRecord,
    ArtifactStore,
    DownloadEvent,
    DownloadMetadata,
    DownloadResult,
    DownloadSource,
    MediaError,
    emit_event,
    _DOWNLOAD_CODES,
)
from .duration import (
    bitrate_kbps,
    duration_matches,
    duration_seconds,
    verify_stream_integrity,
    _verify_stream_integrity_with_diagnostics,
)
from .validation import normalize_language, validate_media, validated_destination

# ``lenient`` accepts what it cannot measure and says so; ``strict`` fails
# closed on the same file.
_DURATION_POLICIES = frozenset({"lenient", "strict"})
# "This call has no measurement to hand over yet", which is not the same as a
# measurement of ``None`` -- that one says the container could not be read.
_UNSET = object()
_MEDIA_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="musicdl-media")
_MEDIA_SUBMISSION_LIMIT = 4  # Two running jobs plus at most two queued in the executor.


class _OffloadWaiter:
    __slots__ = ("loop", "future", "permit", "abandoned")

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.future: asyncio.Future[_OffloadPermit] = loop.create_future()
        self.permit: _OffloadPermit | None = None
        self.abandoned = False


class _OffloadPermit:
    __slots__ = ("_owner", "_released")

    def __init__(self, owner: "_MediaSubmissionGate"):
        self._owner = owner
        self._released = False

    def release(self) -> None:
        self._owner.release(self)


class _MediaSubmissionGate:
    """Bound executor submissions across event loops without blocking them."""

    def __init__(self, limit: int):
        self._limit = limit
        self._active = 0
        self._waiters: list[_OffloadWaiter] = []
        self._lock = threading.Lock()

    async def acquire(self, *, max_waiters: int = 64) -> _OffloadPermit:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._active < self._limit:
                self._active += 1
                return _OffloadPermit(self)
            if len(self._waiters) >= max_waiters:
                raise RuntimeError("media work queue is full")
            waiter = _OffloadWaiter(loop)
            self._waiters.append(waiter)
        try:
            return await asyncio.shield(waiter.future)
        except BaseException:
            with self._lock:
                waiter.abandoned = True
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    if waiter.permit is not None:
                        self._release_locked(waiter.permit)
                        waiter.permit = None
                self._promote_locked()
            try:
                loop.call_soon_threadsafe(waiter.future.cancel)
            except RuntimeError:
                pass
            raise

    def release(self, permit: _OffloadPermit) -> None:
        with self._lock:
            self._release_locked(permit)
            self._promote_locked()

    def snapshot(self) -> tuple[int, int]:
        """Return submitted and waiting counts for bounded-work tests/diagnostics."""
        with self._lock:
            return self._active, len(self._waiters)

    def _release_locked(self, permit: _OffloadPermit) -> None:
        if permit._owner is not self or permit._released:
            return
        permit._released = True
        self._active -= 1

    def _promote_locked(self) -> None:
        while self._active < self._limit and self._waiters:
            waiter = self._waiters.pop(0)
            permit = _OffloadPermit(self)
            waiter.permit = permit
            self._active += 1
            try:
                waiter.loop.call_soon_threadsafe(_set_offload_result, waiter, permit)
            except RuntimeError:  # The waiting loop closed before its task was resumed.
                self._release_locked(permit)


def _set_offload_result(waiter: _OffloadWaiter, permit: _OffloadPermit) -> None:
    if not waiter.abandoned and not waiter.future.done():
        waiter.future.set_result(permit)
    else:
        permit.release()


_MEDIA_SUBMISSIONS = _MediaSubmissionGate(_MEDIA_SUBMISSION_LIMIT)
_DEFERRED_MEDIA_EVENTS: ContextVar[list[DownloadEvent] | None] = ContextVar(
    "musicdl_deferred_media_events", default=None)


async def _offload(function: Callable, /, *args, **kwargs):
    """Run bounded synchronous media work and drain it before cancellation returns.

    Cancelling the awaiting coroutine cannot stop a running thread. Shield the
    executor future and wait for it to finish before the caller closes
    descriptors or removes staging files. Submitted work is not cancelled while
    queued so cancelled work items cannot accumulate in the executor's queue.
    """
    permit = await _MEDIA_SUBMISSIONS.acquire()
    try:
        concurrent = _MEDIA_WORKERS.submit(functools.partial(function, *args, **kwargs))
        future = asyncio.wrap_future(concurrent)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancellation:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if future.done():
                try:
                    future.result()
                except BaseException:
                    pass
            raise cancellation
    finally:
        permit.release()


async def _offload_duration(candidate: Candidate, path: Path, verify_duration: str, request_id: str,
                            record: Callable[[DownloadEvent], None] | None, *, enforce: bool = True):
    """Measure media off-loop and replay deferred events on the owning loop."""
    events: list[DownloadEvent] = []

    def measure():
        token = _DEFERRED_MEDIA_EVENTS.set(events)
        try:
            return _verified_duration(candidate, path, verify_duration, request_id, record, enforce=enforce)
        finally:
            _DEFERRED_MEDIA_EVENTS.reset(token)

    try:
        result = await _offload(measure)
    except BaseException:
        for event in events:
            emit_event(record, event)
        raise
    for event in events:
        emit_event(record, event)
    return result


def _emit_media_event(record: Callable[[DownloadEvent], None] | None, event: DownloadEvent) -> None:
    deferred = _DEFERRED_MEDIA_EVENTS.get()
    if deferred is None:
        emit_event(record, event)
    else:
        deferred.append(event)


async def _close_metadata(metadata: DownloadMetadata) -> None:
    closer = getattr(metadata, "aclose", None) or getattr(metadata, "close", None)
    if closer is None:
        return
    task = asyncio.create_task(closer())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
        raise cancellation


def _record_failure(
    record: Callable[[DownloadEvent], None] | None,
    candidate: Candidate,
    request_id: str,
    size: int,
    code: str,
    quality: str | None = None,
) -> None:
    # A refused attempt has no answer to describe, so the failure reports only
    # the tier that was asked for and leaves ``actual_quality`` empty.
    emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                     "download", "failed", error_code=code, size_bytes=size or None,
                                     requested_quality=quality))


def _record_admission_interruption(record, candidate, request_id, quality, error_code) -> None:
    """Close only the history row; admission delay is not source telemetry."""
    from .history import current_download_journal

    journal = current_download_journal()
    if journal is not None:
        journal.record({"stage": "task", "status": "interrupted",
                        "candidate_id": candidate.item_id,
                        "error_code": error_code})


def _path_too_long(error: BaseException) -> bool:
    return isinstance(error, OSError) and (
        getattr(error, "errno", None) == errno.ENAMETOOLONG
        or getattr(error, "winerror", None) == 206
    )


def _relative_path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MediaError("artifact_uncertain")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MediaError("artifact_uncertain")
    if ":" in path.parts[0]:
        raise MediaError("artifact_uncertain")
    result = root.joinpath(*path.parts)
    try:
        result.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise MediaError("artifact_uncertain") from exc
    for parent in [result.parent, *result.parent.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise MediaError("artifact_uncertain")
    return result


def _safe_artifact_paths(root: Path, reservation: ArtifactRecord) -> tuple[Path, Path]:
    temporary = _relative_path(root, reservation.temporary_relative_path)
    target = _relative_path(root, reservation.target_relative_path)
    if temporary == target:
        raise MediaError("artifact_uncertain")
    return temporary, target


def _hash_file(path: Path) -> tuple[int, str]:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise MediaError("artifact_uncertain")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return info.st_size, digest.hexdigest()
    except MediaError:
        raise
    except (OSError, ValueError) as exc:
        raise MediaError("artifact_uncertain") from exc


def _verify_artifact(path: Path, reservation: ArtifactRecord) -> tuple[int, str]:
    size, digest = _hash_file(path)
    if reservation.size_bytes is None or reservation.sha256 is None:
        raise MediaError("artifact_uncertain")
    if size != reservation.size_bytes or digest != reservation.sha256:
        raise MediaError("artifact_uncertain")
    return size, digest


def _verified_duration(candidate: Candidate, path: Path, verify_duration: str, request_id: str,
                       record: Callable[[DownloadEvent], None] | None, *, enforce: bool = True) -> float | None:
    """The real playing time of a finished file, or a refusal to deliver it.

    ``None`` means the container could not be read.  The event says so, and
    ``strict`` turns that into a refusal while the default ``lenient`` keeps the
    file: deliberately unlike the upstream policy, because a good part of the
    supplied lx sources never state a duration and failing closed would refuse
    those sources wholesale.  A file that does measure, and measures outside the
    catalogue's own length, is refused under either policy -- that is the whole
    point of asking.

    ``enforce=False`` is for bytes that are already published library content:
    the gate belongs to publication, so the measurement is still taken and
    still reported, but it is not turned into a refusal.  Refusing there while
    keeping the bytes would be a refusal that changed nothing, and deleting
    them would take away a file the library may already be serving -- so the
    only way a refusal can leave nothing behind is for it to happen before the
    artifact is published.  ``strict`` answers to the same rule: it refuses
    what it cannot measure, and that refusal is a publication gate as well,
    so it does not fire where ``enforce`` is false.
    """
    measured = duration_seconds(path)
    expected = float(candidate.duration) if candidate.duration and candidate.duration > 0 else None
    stream_integrity = None
    diagnostics = None
    if enforce:
        stream_integrity, diagnostics = _verify_stream_integrity_with_diagnostics(
            path, expected_duration=expected, verifier=verify_stream_integrity)
        if stream_integrity.format in {"flac", "mp4"} and stream_integrity.status == "incomplete":
            if enforce:
                raise MediaError("incomplete_audio")
        elif stream_integrity.format in {"flac", "mp4"} and stream_integrity.status == "unverified":
            if stream_integrity.format == "flac" and not diagnostics.flac_duration_trusted:
                measured = None
            _emit_media_event(record, DownloadEvent(
                request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                "duration", "unverified", error_code="duration_unverified"))
            duration_is_trusted = (
                diagnostics.flac_duration_trusted if stream_integrity.format == "flac"
                else measured is not None)
            if expected is not None and measured is not None and duration_is_trusted:
                if not duration_matches(expected, measured):
                    raise MediaError("incomplete_audio")
            if verify_duration == "strict" and enforce:
                raise MediaError("incomplete_audio")
            # ``unverified`` still carries an event but not a duration receipt.
            # A trusted measurement has already been compared above; keeping it
            # here would change the existing result/bitrate contract.
            return None
    # A catalogue that states ``0`` has stated nothing: Telegram reports an
    # unknown length that way, and reading it as a real expectation would
    # refuse a complete recording for a number the channel never claimed.
    if expected is not None and measured is not None:
        if enforce and not duration_matches(expected, measured):
            raise MediaError("incomplete_audio")
        return measured
    _emit_media_event(record, DownloadEvent(
        request_id, candidate.item_id, candidate.source_id, candidate.source_version,
        "duration", "unverified", error_code="duration_unverified"))
    if verify_duration == "strict" and enforce:
        raise MediaError("incomplete_audio")
    return measured


def _replay_checks(candidate: Candidate, reservation: ArtifactRecord,
                   path: Path, *, allow_format_change: bool = False) -> tuple[int, str, str]:
    """The guard every replay runs before it hands bytes over.

    Both the already-published path and the staged one answer to it, so a
    refusal can never be something that happened only after a file was
    published.  It returns ``(size, digest, extension)``.

    ``allow_format_change`` is for a caller that asked for a tier rather than a
    container: the answer may legitimately arrive in a different container, and
    the record it was written under is the one that names the bytes.
    """
    size, digest = _verify_artifact(path, reservation)
    extension = reservation.extension if reservation.extension.startswith(".") else "." + reservation.extension
    expected = "." + str(candidate.format or "").lstrip(".").lower()
    if reservation.candidate_id != candidate.item_id or (not allow_format_change and extension.lower() != expected):
        raise MediaError("artifact_uncertain")
    return size, digest, extension


async def _replayed_result(root: Path, target: Path, candidate: Candidate, reservation: ArtifactRecord,
                           language: str | None, *, verify_duration: str, request_id: str,
                           record: Callable[[DownloadEvent], None] | None,
                           allow_format_change: bool = False,
                           measured: float | None | object = _UNSET,
                           checked: tuple[int, str, str] | None = None,
                           enforce_duration: bool = True,
                           requested_quality: str | None = None) -> DownloadResult:
    size, digest, extension = (
        await _offload(_replay_checks, candidate, reservation, target,
                       allow_format_change=allow_format_change)
        if checked is None else checked)
    # The name the bytes are delivered under has to agree with the container the
    # record names, or the two describe different files.
    if extension.lower() != target.suffix.lower():
        raise MediaError("artifact_uncertain")
    # A replay hands over an artifact this job already wrote, so it answers to
    # the same policy as a fresh download.
    if measured is _UNSET:
        measured = await _offload_duration(candidate, target, verify_duration, request_id, record,
                                            enforce=enforce_duration)
    # A replay answers the same question a fresh download does, so it names the
    # tier the job asked for as well as the container the bytes prove; the two
    # fields are what the record and the success notice report.
    return DownloadResult(target.relative_to(root), digest, size, reservation.media_type, extension,
                          normalize_language(language), duration_seconds=measured,
                          bitrate_kbps=None if measured is None else bitrate_kbps(size, measured),
                          requested_quality=requested_quality,
                          actual_quality=extension.lstrip(".").lower() or None)


def _discard(path: Path) -> bool:
    """Remove one path this job owns, and say whether it is really gone.

    A filesystem that refuses the unlink -- a handle still open, a read-only
    mount -- leaves the bytes exactly where they were, so a caller that promised
    a refusal left nothing behind has to look rather than assume: the answer is
    the check, not the attempt.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return not (path.exists() or path.is_symlink())


def _report_cleanup_failure(record: Callable[[DownloadEvent], None] | None, candidate: Candidate,
                            request_id: str, size: int | None) -> None:
    """Say out loud that bytes this job tried to remove are still on disk."""
    emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id,
                                     candidate.source_version, "cleanup", "failed",
                                     error_code="cleanup_failed", size_bytes=size or None))


def _fsync_path(path: Path) -> None:
    # Windows refuses fsync on a read-only handle (OSError 9), so flush through a writable one.
    flags = (os.O_RDWR if os.name == "nt" else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def _publish_reserved(
    temporary: Path,
    target: Path,
    reservation: ArtifactRecord,
    artifact_store: ArtifactStore | None,
    *,
    owner: str | None,
    fence: int | None,
    ttl: int,
) -> None:
    if artifact_store is not None:
        if owner is None or fence is None:
            raise MediaError("artifact_uncertain")
        try:
            await artifact_store.claim_artifact_publish(reservation.job_id, owner=owner, fence=fence, ttl=ttl)
        except Exception as exc:
            raise MediaError("artifact_uncertain") from exc
    async def publish_and_record() -> None:
        if target.exists() or target.is_symlink():
            raise MediaError("artifact_uncertain")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(temporary, target)
        except (FileExistsError, OSError) as exc:
            raise MediaError("artifact_uncertain") from exc
        try:
            await _offload(_fsync_path, target)
            try:
                await _offload(_fsync_path, target.parent)
            except OSError:
                if os.name != "nt":
                    raise
        except Exception as exc:
            raise MediaError("artifact_uncertain") from exc
        if artifact_store is not None:
            try:
                await artifact_store.mark_artifact_published(reservation.job_id, owner=owner, fence=fence, ttl=ttl)
            except Exception as exc:
                raise MediaError("artifact_uncertain") from exc

    # Once a fenced publish starts, cancellation cannot safely abandon the
    # interval between creating the destination and recording it as published.
    # Let that interval settle before the caller cleans its staging path.
    publish_task = asyncio.create_task(publish_and_record())
    try:
        await asyncio.shield(publish_task)
    except asyncio.CancelledError as cancellation:
        while not publish_task.done():
            try:
                await asyncio.shield(publish_task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if publish_task.done():
            try:
                publish_task.result()
            except BaseException:
                pass
        raise cancellation


async def _resume_reservation(
    root: Path,
    candidate: Candidate,
    reservation: ArtifactRecord,
    artifact_store: ArtifactStore | None,
    *,
    owner: str | None,
    fence: int | None,
    ttl: int,
    language: str | None,
    verify_duration: str,
    request_id: str,
    record: Callable[[DownloadEvent], None] | None,
    quality: str | None = None,
) -> DownloadResult | None:
    temporary, target = _safe_artifact_paths(root, reservation)
    if reservation.candidate_id != candidate.item_id:
        raise MediaError("artifact_uncertain")
    if reservation.state == "uncertain":
        raise MediaError("artifact_uncertain")
    if reservation.state == "published":
        # Already delivered, so the bytes stay where a person may already have
        # found them: only a refusal reached before publication can take a file
        # away, because only then does nothing yet refer to it.  This artifact
        # is therefore measured and reported rather than judged again -- a
        # refusal here would either leave the very bytes it refused or delete
        # library content the delivery may already refer to.
        return await _replayed_result(root, target, candidate, reservation, language,
                                      verify_duration=verify_duration, request_id=request_id, record=record,
                                      allow_format_change=quality is not None,
                                      enforce_duration=False, requested_quality=quality)
    if reservation.state == "prepared":
        if temporary.exists() or temporary.is_symlink() or target.exists() or target.is_symlink():
            raise MediaError("artifact_uncertain")
        return None
    if reservation.state == "external_started":
        raise MediaError("artifact_uncertain")
    if reservation.state in {"stream_complete", "publishing", "physically_published"}:
        if reservation.state == "stream_complete":
            # The staged bytes answer to the delivery policy before anything is
            # published, so a refusal here cannot be the reason a new file
            # appeared in the library.
            checked = await _offload(_replay_checks, candidate, reservation, temporary,
                                     allow_format_change=quality is not None)
            try:
                measured = await _offload_duration(candidate, temporary, verify_duration, request_id, record)
            except MediaError:
                # Refused before it was published, so the staged bytes are
                # scratch that failed its own policy rather than library
                # content: they go the way a fresh attempt's scratch goes, and
                # a refusal never leaves a file behind.  A removal the
                # filesystem refused is reported rather than passed off as a
                # refusal that cleaned up after itself.
                if not _discard(temporary):
                    _report_cleanup_failure(record, candidate, request_id, reservation.size_bytes)
                raise
            await _publish_reserved(temporary, target, reservation, artifact_store,
                                    owner=owner, fence=fence, ttl=ttl)
        else:
            if not target.exists() or target.is_symlink():
                raise MediaError("artifact_uncertain")
            checked = await _offload(_replay_checks, candidate, reservation, target,
                                     allow_format_change=quality is not None)
            measured = _UNSET
        _discard(temporary)
        try:
            return await _replayed_result(root, target, candidate, reservation, language,
                                          verify_duration=verify_duration, request_id=request_id, record=record,
                                          allow_format_change=quality is not None,
                                          measured=measured, checked=checked, requested_quality=quality)
        except MediaError as exc:
            if exc.code == "incomplete_audio":
                # This artifact never reached ``published``, so nothing refers
                # to it and the library must not go on serving what was just
                # refused: the bytes go the way a fresh attempt's scratch goes.
                # A removal the filesystem refused is reported rather than
                # passed off as a refusal that cleaned up after itself.
                gone = _discard(temporary)
                if not _discard(target):
                    gone = False
                if not gone:
                    _report_cleanup_failure(record, candidate, request_id, reservation.size_bytes)
            raise
    raise MediaError("artifact_uncertain")


@trace_download_stage("resolve")
async def source_download(source: DownloadSource, candidate: Candidate, *,
                          quality: str | None = None,
                          admission_permit: AdmissionPermit | None = None) -> DownloadMetadata:
    """Omit the optional keyword for old resolvers without masking their errors."""
    if admission_permit is not None:
        await admission_permit.switch_source(candidate.source_id)
    parameters = inspect.signature(source.download).parameters
    supports_quality = "quality" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if quality is not None and supports_quality:
        return await source.download(candidate, quality=quality)
    return await source.download(candidate)


@trace_download_stage("transfer")
async def download_candidate(
    candidate: Candidate,
    source: DownloadSource,
    media_root: str | Path,
    *,
    request_id: str,
    quality: str | None = None,
    resolved_metadata: DownloadMetadata | None = None,
    admission_permit: AdmissionPermit | None = None,
    prepare: Callable[[str, str], Awaitable[ArtifactRecord | None]] | None = None,
    reservation: ArtifactRecord | None = None,
    artifact_store: ArtifactStore | None = None,
    owner: str | None = None,
    fence: int | None = None,
    language: str | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
    verify_duration: str = "lenient",
    record: Callable[[DownloadEvent], None] | None = None,
) -> DownloadResult:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise MediaError("invalid_max_bytes")
    if verify_duration not in _DURATION_POLICIES:
        raise MediaError("invalid_verify_duration")
    temp_path: Path | None = None
    cleanup_event_emitted = False
    size = 0
    digest = hashlib.sha256()
    metadata: DownloadMetadata | None = resolved_metadata
    primary_error: BaseException | None = None
    try:
        if resolved_metadata is not None and has_expired(resolved_metadata.expires_at):
            # A held descriptor is only reused while its URL is still alive.
            # Refusing here makes the caller resolve again instead of streaming
            # a URL the source has already retired.
            raise MediaError("media_url_expired")
        root = Path(media_root).resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        if reservation is not None:
            replayed = await _resume_reservation(root, candidate, reservation, artifact_store,
                                                 owner=owner, fence=fence, ttl=0,
                                                 language=language, verify_duration=verify_duration,
                                                 request_id=request_id, record=record, quality=quality)
            if replayed is not None:
                return replayed
            temp_path, target = _safe_artifact_paths(root, reservation)
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(os.fspath(temp_path), flags, 0o600)
            try:
                metadata = metadata or await source_download(source, candidate, quality=quality,
                                                             admission_permit=admission_permit)
            except BaseException:
                # Close the reserved staging file before the failure unwinds, otherwise the
                # cleanup unlink cannot remove it and a later retry of the job is refused as
                # uncertain because the staging path still exists.
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        else:
            metadata = metadata or await source_download(source, candidate, quality=quality,
                                                         admission_permit=admission_permit)
            fd, temp_name = tempfile.mkstemp(prefix=".musicdl-", suffix=".part", dir=root)
            temp_path = Path(temp_name)
        header = bytearray()
        try:
            # A descriptor may have been resolved earlier while fallback tried
            # another channel. Reacquire its source slot before consuming it.
            if admission_permit is not None:
                await admission_permit.switch_source(candidate.source_id)
            async for chunk in metadata.chunks:
                if not isinstance(chunk, bytes):
                    raise MediaError("invalid_chunk")
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise MediaError("file_too_large")
                if metadata.declared_size is not None and size > metadata.declared_size:
                    raise MediaError("size_mismatch")
                if len(header) < 512:
                    header.extend(chunk[: 512 - len(header)])
                pending = memoryview(chunk)
                while pending:
                    written = os.write(fd, pending)
                    if not isinstance(written, int) or written <= 0:
                        raise MediaError("download_failed")
                    pending = pending[written:]
                digest.update(chunk)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        else:
            try:
                await _offload(os.fsync, fd)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            try:
                os.close(fd)
            except OSError as exc:
                raise MediaError("download_failed") from exc
        if size == 0:
            raise MediaError("empty_download")
        if metadata.declared_size is not None and metadata.declared_size != size:
            raise MediaError("size_mismatch")
        extension, media_type = validate_media(bytes(header), metadata,
                                                None if quality is not None else candidate.format)
        # The bytes are on disk and hashed by now, so this measures the file
        # that would be delivered rather than what any channel said about it.
        measured = await _offload_duration(candidate, temp_path, verify_duration, request_id, record)
        bitrate = None if measured is None else bitrate_kbps(size, measured)

        if reservation is None and prepare is not None:
            reservation = await prepare(extension, media_type)
            if reservation is not None:
                staging, target = _safe_artifact_paths(root, reservation)
                staging.parent.mkdir(parents=True, exist_ok=True)
                if staging.exists() or staging.is_symlink():
                    raise MediaError("artifact_uncertain")
                os.link(temp_path, staging)
                temp_path.unlink()
                temp_path = staging

        if reservation is not None:
            try:
                await metadata.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MediaError("download_failed") from exc
            if artifact_store is not None:
                if owner is None or fence is None:
                    raise MediaError("artifact_uncertain")
                try:
                    reservation = await artifact_store.record_stream_complete(
                        reservation.job_id, owner=owner, fence=fence, size_bytes=size,
                        sha256=digest.hexdigest(), extension=extension, media_type=media_type,
                        declared_size=metadata.declared_size, ttl=0)
                except Exception as exc:
                    raise MediaError("artifact_uncertain") from exc
            await _publish_reserved(temp_path, target, reservation, artifact_store,
                                    owner=owner, fence=fence, ttl=0)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            temp_path = None
            relative = target.relative_to(root)
        else:
            base = validated_destination(root, language, candidate.artist, candidate.title, extension)
            base.parent.mkdir(parents=True, exist_ok=True)
            target = base
            counter = 1
            while True:
                try:
                    os.link(temp_path, target)
                    break
                except FileExistsError:
                    counter += 1
                    target = base.with_name(f"{base.stem} ({counter}){base.suffix}")
                except OSError as exc:
                    fallback_errnos = {errno.EXDEV, getattr(errno, "EOPNOTSUPP", -1)}
                    if getattr(exc, "winerror", None) == 1 or (
                        getattr(exc, "errno", None) in fallback_errnos and not _path_too_long(exc)
                    ):
                        while target.exists():
                            counter += 1
                            target = base.with_name(f"{base.stem} ({counter}){base.suffix}")
                        os.replace(temp_path, target)
                        break
                    raise
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                cleanup_event_emitted = True
                _report_cleanup_failure(record, candidate, request_id, size)
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            temp_path = None
            relative = target.relative_to(root)
        # The bytes on disk prove their own container, so an answer that named no
        # tier still reports one and the report is never emptier than the verified
        # file.  ``quality`` keeps its separate meaning -- the tier the source
        # stated, empty when it stated none -- because the downgrade verdict is
        # derived from stated tiers only; a container is not a downgrade proof.
        actual_quality = served_quality(metadata.quality, extension)
        event = DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                              "download", "success", size_bytes=size, sha256=digest.hexdigest(),
                              relative_path=relative.as_posix(),
                              requested_quality=quality, actual_quality=actual_quality)
        emit_event(record, event)
        return DownloadResult(relative, digest.hexdigest(), size, media_type, extension,
                              normalize_language(language), duration_seconds=measured,
                              bitrate_kbps=bitrate, quality=metadata.quality,
                              requested_quality=quality, actual_quality=actual_quality)
    except asyncio.CancelledError as exc:
        primary_error = exc
        _record_failure(record, candidate, request_id, size, "download_cancelled", quality=quality)
        raise
    except AdmissionRejected as exc:
        # Admission is local capacity control, not a source failure.
        primary_error = exc
        code = ADMISSION_TIMEOUT_CODE if isinstance(exc, AdmissionTimeout) else ADMISSION_BLOCKED_CODE
        _record_admission_interruption(record, candidate, request_id, quality, code)
        raise
    except MediaError as exc:
        primary_error = exc
        code = exc.code if exc.code in _DOWNLOAD_CODES else "download_failed"
        _record_failure(record, candidate, request_id, size, code, quality=quality)
        if code == exc.code:
            raise
        raise MediaError(code) from None
    except Exception as exc:
        primary_error = exc
        code = "path_too_long" if _path_too_long(exc) else "download_failed"
        _record_failure(record, candidate, request_id, size, code, quality=quality)
        raise MediaError(code) from None
    finally:
        if metadata is not None:
            try:
                await _close_metadata(metadata)
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
            except AdmissionRejected as exc:
                if primary_error is None:
                    code = ADMISSION_TIMEOUT_CODE if isinstance(exc, AdmissionTimeout) else ADMISSION_BLOCKED_CODE
                    _record_admission_interruption(record, candidate, request_id, quality, code)
                    raise
            except BaseException:
                if primary_error is None:
                    _record_failure(record, candidate, request_id, size, "download_failed", quality=quality)
                    raise MediaError("download_failed") from None
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                if not cleanup_event_emitted:
                    _report_cleanup_failure(record, candidate, request_id, size)
