from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, get_args

from musicdl.sources.models import Candidate
from musicdl.sources.quality import QUALITY_REVISION
from musicdl.sources.search import SearchResult

MAX_MEDIA_BYTES = 500 * 1024 * 1024
_DOWNLOAD_CODES = frozenset({
    "invalid_max_bytes", "invalid_chunk", "file_too_large", "download_failed", "empty_download",
    "size_mismatch", "unsupported_extension", "signature_mismatch", "extension_mismatch",
    "mime_mismatch", "path_escape", "path_too_long", "artifact_uncertain", "media_url_denied", "media_host_denied",
    # ``incomplete_audio`` refuses a preview or a stream that stopped early;
    # ``duration_unverified`` records an accepted download whose length could
    # not be checked, and ``invalid_verify_duration`` refuses a policy this
    # build does not know.
    "incomplete_audio", "duration_unverified", "invalid_verify_duration",
    "media_dns_failed", "media_address_denied", "media_connect_failed", "media_tls_failed",
    "media_timeout", "media_redirect_denied", "media_response_invalid", "media_url_expired",
})
Language = Literal["华语", "欧美", "日韩", "未知"]
LANGUAGES: frozenset[str] = frozenset(get_args(Language))


@dataclass
class _CloseOnce:
    closer: Callable[[], Awaitable[None]]
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def close(self) -> None:
        async with self._lock:
            if self._task is None:
                self._task = asyncio.create_task(self.closer())
            task = self._task
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


class DeclaredSize:
    """The size a download is answerable for, filled in while its bytes arrive.

    A size a source states is a reference rather than a contract -- measured
    2026-09-27, a QQ 音乐 FLAC answer carried 15 bytes more than the entry its
    洛雪 source repeats -- so the number a download is answerable for is the one
    the server really sent.  A transport that was handed only a reference
    learns that number while the body streams, and everything downstream (the
    download record, the panel, the success message) reads it afterwards, so
    the value has to be shared with the stream instead of fixed up front.  An
    authoritative size needs no cell: it is known before the first byte.
    """

    __slots__ = ("value",)

    def __init__(self, value: int | None = None) -> None:
        self.value = value


class DownloadMetadata:
    """The bytes one download hands over, and the size they are answerable for.

    Read-only to callers: ``declared_size`` is a property and the only writer is
    the stream itself, which records the length it really delivered.
    """

    __slots__ = ("chunks", "extension", "media_type", "quality", "expires_at",
                 "_declared_size", "_close_once")

    def __init__(
        self,
        chunks: AsyncIterable[bytes],
        extension: str | None = None,
        media_type: str | None = None,
        declared_size: int | DeclaredSize | None = None,
        _close_once: _CloseOnce | None = None,
        quality: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        self.chunks = chunks
        self.extension = extension
        self.media_type = media_type
        self.quality = quality
        # The instant this stream's URL stops being usable, copied from the
        # descriptor the source answered with.  A descriptor already past it is
        # refused rather than streamed, so a caller re-resolves instead.
        self.expires_at = expires_at
        self._declared_size = declared_size if isinstance(declared_size, DeclaredSize) else DeclaredSize(declared_size)
        self._close_once = _close_once

    @property
    def declared_size(self) -> int | None:
        """The length this download really is, once it has been delivered."""
        return self._declared_size.value

    def note_size(self, value: int) -> None:
        """Record the length the bytes turned out to be."""
        self._declared_size.value = value

    async def aclose(self) -> None:
        if self._close_once is not None:
            await self._close_once.close()

    async def close(self) -> None:
        await self.aclose()


@dataclass(frozen=True)
class ArtifactRecord:
    job_id: str
    candidate_id: str
    temporary_relative_path: str
    target_relative_path: str
    allocation_slot: int
    extension: str
    media_type: str
    declared_size: int | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    owner: str | None = None
    fence: int = 0
    lease_until_ms: int | None = None
    state: Literal[
        "prepared", "external_started", "stream_complete", "publishing", "physically_published",
        "published", "uncertain",
    ] = "prepared"


ArtifactTakeoverAction = Literal[
    "resume", "uncertain", "verify_publish", "verify_complete", "replay", "owner_conflict",
]


class ArtifactStore(Protocol):
    async def prepare_artifact(
        self,
        job_id: str,
        candidate: Candidate,
        *,
        media_root: str | Path,
        base_relative_path: str,
        extension: str,
        media_type: str,
        declared_size: int | None,
        owner: str,
        fence: int,
        ttl: int,
    ) -> ArtifactRecord: ...

    async def record_stream_complete(
        self,
        job_id: str,
        *,
        owner: str,
        fence: int,
        size_bytes: int,
        sha256: str,
        extension: str,
        media_type: str,
        declared_size: int | None,
        ttl: int,
    ) -> ArtifactRecord: ...

    async def mark_artifact_published(
        self,
        job_id: str,
        *,
        owner: str,
        fence: int,
        ttl: int,
    ) -> ArtifactRecord: ...

    async def takeover_artifact(
        self,
        job_id: str,
        *,
        new_owner: str,
        new_fence: int,
        now_ms: int,
        lease_ms: int,
    ) -> ArtifactTakeoverAction: ...

    async def claim_artifact_publish(
        self,
        job_id: str,
        *,
        owner: str,
        fence: int,
        ttl: int,
    ) -> ArtifactRecord: ...

    async def get_artifact(self, job_id: str) -> ArtifactRecord | None: ...


class DownloadSource(Protocol):
    async def download(self, candidate: Candidate, *, quality: str | None = None) -> DownloadMetadata: ...
    async def health(self) -> bool: ...


@dataclass(frozen=True)
class DownloadResult:
    relative_path: Path
    sha256: str
    size_bytes: int
    media_type: str
    extension: str
    language: Language
    # What the delivered bytes really measure, and the rate they really
    # carried.  Both stay ``None`` when the container could not be read, so a
    # caller that shows them never repeats a channel's claim as a measurement.
    duration_seconds: float | None = None
    bitrate_kbps: int | None = None
    quality: str | None = None
    quality_downgraded: bool = False
    # Which revision of the actual-quality verification produced `quality`.
    # Persisted with the download record so a later, stricter verifier can spot
    # a verdict it no longer stands behind and re-check it.
    quality_revision: int = QUALITY_REVISION
    # The tier the caller asked for and the tier the bytes turned out to be.
    # ``actual_quality`` repeats the source's own label while the verified
    # container can carry it and names that container otherwise, so a label the
    # bytes contradict never reaches the report; it is never read back out of
    # the request, so a source that quietly answers a lossless request with a
    # lossy stream is reported as the downgrade it is.
    requested_quality: str | None = None
    actual_quality: str | None = None


@dataclass(frozen=True)
class DownloadEvent:
    request_id: str
    candidate_id: str
    source_id: str
    source_version: str
    stage: str
    status: str
    error_code: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    relative_path: str | None = None
    healthy: bool | None = None
    # The tier the download asked for and the tier the bytes turned out to be,
    # reported side by side: a source that quietly serves a lossy stream for a
    # lossless request is a fact about the answer, not a guess about the request.
    requested_quality: str | None = None
    actual_quality: str | None = None


@dataclass(frozen=True)
class FallbackResult:
    download: DownloadResult | None = None
    refreshed: SearchResult | None = None
    failed_source_id: str | None = None
    download_error: str | None = None
    refresh_error: str | None = None
    healthy: bool | None = None


class MediaError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def emit_event(record: Callable[[DownloadEvent], None] | None, event: DownloadEvent) -> None:
    if record is None:
        return
    try:
        record(event)
    except Exception:
        pass
