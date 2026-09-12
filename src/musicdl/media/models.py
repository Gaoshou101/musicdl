from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from collections.abc import Callable

from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult

MAX_MEDIA_BYTES = 500 * 1024 * 1024
_DOWNLOAD_CODES = frozenset({"invalid_max_bytes", "invalid_chunk", "file_too_large", "download_failed", "empty_download", "size_mismatch", "unsupported_extension", "signature_mismatch", "extension_mismatch", "mime_mismatch", "path_escape"})
Language = Literal["华语", "欧美", "日韩", "未知"]


@dataclass(frozen=True)
class DownloadMetadata:
    chunks: AsyncIterable[bytes]
    extension: str | None = None
    media_type: str | None = None
    declared_size: int | None = None


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
