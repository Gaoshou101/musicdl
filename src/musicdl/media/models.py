from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult

MAX_MEDIA_BYTES = 500 * 1024 * 1024
_DOWNLOAD_CODES = frozenset({
    "invalid_max_bytes", "invalid_chunk", "file_too_large", "download_failed", "empty_download",
    "size_mismatch", "unsupported_extension", "signature_mismatch", "extension_mismatch",
    "mime_mismatch", "path_escape", "artifact_uncertain", "media_url_denied", "media_host_denied",
    "media_dns_failed", "media_address_denied", "media_connect_failed", "media_tls_failed",
    "media_timeout", "media_redirect_denied", "media_response_invalid",
})
Language = Literal["华语", "欧美", "日韩", "未知"]


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


@dataclass(frozen=True)
class DownloadMetadata:
    chunks: AsyncIterable[bytes]
    extension: str | None = None
    media_type: str | None = None
    declared_size: int | None = None
    _close_once: _CloseOnce | None = field(default=None, repr=False, compare=False)

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
    async def download(self, candidate: Candidate) -> DownloadMetadata: ...
    async def health(self) -> bool: ...


@dataclass(frozen=True)
class DownloadResult:
    relative_path: Path
    sha256: str
    size_bytes: int
    media_type: str
    extension: str
    language: Language


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
