from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from musicdl.sources.models import Candidate

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
from .validation import normalize_language, validate_media, validated_destination


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
) -> None:
    emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                     "download", "failed", error_code=code, size_bytes=size or None))


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


def _replayed_result(root: Path, target: Path, candidate: Candidate, reservation: ArtifactRecord, language: str | None) -> DownloadResult:
    size, digest = _verify_artifact(target, reservation)
    extension = reservation.extension if reservation.extension.startswith(".") else "." + reservation.extension
    expected = "." + candidate.format.lstrip(".").lower()
    if extension.lower() != expected or reservation.candidate_id != candidate.item_id:
        raise MediaError("artifact_uncertain")
    return DownloadResult(target.relative_to(root), digest, size, reservation.media_type, extension,
                          normalize_language(language))


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
    if target.exists() or target.is_symlink():
        raise MediaError("artifact_uncertain")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(temporary, target)
    except (FileExistsError, OSError) as exc:
        raise MediaError("artifact_uncertain") from exc
    try:
        _fsync_path(target)
        try:
            _fsync_path(target.parent)
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
) -> DownloadResult | None:
    temporary, target = _safe_artifact_paths(root, reservation)
    if reservation.candidate_id != candidate.item_id:
        raise MediaError("artifact_uncertain")
    if reservation.state == "uncertain":
        raise MediaError("artifact_uncertain")
    if reservation.state == "published":
        return _replayed_result(root, target, candidate, reservation, language)
    if reservation.state == "prepared":
        if temporary.exists() or temporary.is_symlink() or target.exists() or target.is_symlink():
            raise MediaError("artifact_uncertain")
        return None
    if reservation.state == "external_started":
        raise MediaError("artifact_uncertain")
    if reservation.state in {"stream_complete", "publishing", "physically_published"}:
        if reservation.state == "stream_complete":
            _verify_artifact(temporary, reservation)
            await _publish_reserved(temporary, target, reservation, artifact_store,
                                    owner=owner, fence=fence, ttl=ttl)
        else:
            if not target.exists() or target.is_symlink():
                raise MediaError("artifact_uncertain")
            _verify_artifact(target, reservation)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return _replayed_result(root, target, candidate, reservation, language)
    raise MediaError("artifact_uncertain")


async def download_candidate(
    candidate: Candidate,
    source: DownloadSource,
    media_root: str | Path,
    *,
    request_id: str,
    reservation: ArtifactRecord | None = None,
    artifact_store: ArtifactStore | None = None,
    owner: str | None = None,
    fence: int | None = None,
    language: str | None = None,
    max_bytes: int = MAX_MEDIA_BYTES,
    record: Callable[[DownloadEvent], None] | None = None,
) -> DownloadResult:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise MediaError("invalid_max_bytes")
    temp_path: Path | None = None
    cleanup_event_emitted = False
    size = 0
    digest = hashlib.sha256()
    metadata: DownloadMetadata | None = None
    primary_error: BaseException | None = None
    try:
        root = Path(media_root).resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        if reservation is not None:
            replayed = await _resume_reservation(root, candidate, reservation, artifact_store,
                                                 owner=owner, fence=fence, ttl=0,
                                                 language=language)
            if replayed is not None:
                return replayed
            temp_path, target = _safe_artifact_paths(root, reservation)
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(os.fspath(temp_path), flags, 0o600)
            metadata = await source.download(candidate)
        else:
            metadata = await source.download(candidate)
            fd, temp_name = tempfile.mkstemp(prefix=".musicdl-", suffix=".part", dir=root)
            temp_path = Path(temp_name)
        header = bytearray()
        try:
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
                os.fsync(fd)
            except Exception:
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
        extension, media_type = validate_media(bytes(header), metadata, candidate.format)

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
                    if getattr(exc, "winerror", None) == 1 or getattr(exc, "errno", None) in {18, 38, 95}:
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
                emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                     "cleanup", "failed", error_code="cleanup_failed", size_bytes=size or None))
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            temp_path = None
            relative = target.relative_to(root)
        event = DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                              "download", "success", size_bytes=size, sha256=digest.hexdigest(),
                              relative_path=relative.as_posix())
        emit_event(record, event)
        return DownloadResult(relative, digest.hexdigest(), size, media_type, extension,
                              normalize_language(language))
    except asyncio.CancelledError as exc:
        primary_error = exc
        _record_failure(record, candidate, request_id, size, "download_cancelled")
        raise
    except MediaError as exc:
        primary_error = exc
        code = exc.code if exc.code in _DOWNLOAD_CODES else "download_failed"
        _record_failure(record, candidate, request_id, size, code)
        if code == exc.code:
            raise
        raise MediaError(code) from None
    except Exception as exc:
        primary_error = exc
        _record_failure(record, candidate, request_id, size, "download_failed")
        raise MediaError("download_failed") from None
    finally:
        if metadata is not None:
            try:
                await _close_metadata(metadata)
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
            except BaseException:
                if primary_error is None:
                    _record_failure(record, candidate, request_id, size, "download_failed")
                    raise MediaError("download_failed") from None
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                if not cleanup_event_emitted:
                    emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                         "cleanup", "failed", error_code="cleanup_failed", size_bytes=size or None))
