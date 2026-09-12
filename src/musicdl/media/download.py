from __future__ import annotations

import hashlib
import os
import tempfile
import asyncio
from collections.abc import Callable
from pathlib import Path

from musicdl.sources.models import Candidate

from .models import MAX_MEDIA_BYTES, DownloadEvent, DownloadResult, DownloadSource, MediaError, emit_event, _DOWNLOAD_CODES
from .validation import normalize_language, validate_media, validated_destination



async def download_candidate(
    candidate: Candidate,
    source: DownloadSource,
    media_root: str | Path,
    *,
    request_id: str,
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
    try:
        root = Path(media_root).resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
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
                if len(header) < 64:
                    header.extend(chunk[: 64 - len(header)])
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
    except asyncio.CancelledError:
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                             "download", "failed", error_code="download_cancelled", size_bytes=size or None))
        raise
    except MediaError as exc:
        code = exc.code if exc.code in _DOWNLOAD_CODES else "download_failed"
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                             "download", "failed", error_code=code, size_bytes=size or None))
        if code == exc.code:
            raise
        raise MediaError(code) from None
    except Exception as exc:
        emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                             "download", "failed", error_code="download_failed", size_bytes=size or None))
        raise MediaError("download_failed") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                if not cleanup_event_emitted:
                    emit_event(record, DownloadEvent(request_id, candidate.item_id, candidate.source_id, candidate.source_version,
                                         "cleanup", "failed", error_code="cleanup_failed", size_bytes=size or None))
