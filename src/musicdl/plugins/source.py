"""Phase 2 MusicSource adapter for an enabled stored plugin."""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from musicdl.contracts.plugin import PluginRequest
from musicdl.media.models import DownloadMetadata
from musicdl.sources.models import Candidate, normalize_text
from .client import PluginClient
from .store import StoredPlugin

if TYPE_CHECKING:
    from musicdl.media.transport import SecureMediaTransport

_MAX_TIMEOUT_MS = 30_000


def _remaining_ms(deadline: float) -> int:
    """Budget left in whole milliseconds; a non-positive value fails closed downstream."""
    return math.ceil((deadline - time.monotonic()) * 1000)


class PluginSource:
    def __init__(self, stored: StoredPlugin, client: PluginClient,
                 transport: SecureMediaTransport | None = None, *,
                 resolve_stream_timeout_ms: int | None = None,
                 health_timeout_ms: int | None = None):
        for value in (resolve_stream_timeout_ms, health_timeout_ms):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_TIMEOUT_MS:
                raise ValueError("invalid_timeout")
        self.stored = stored
        self.client = client
        self.transport = transport
        self.resolve_stream_timeout_ms = resolve_stream_timeout_ms
        self.health_timeout_ms = health_timeout_ms

    async def search(self, query: str) -> tuple[Candidate, ...]:
        query = normalize_text(query)
        if not query or len(query) > 500:
            raise ValueError("invalid_query")
        if not self.stored.enabled:
            return ()
        request = PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search",
                                payload={"query": query})
        response = await self.client.invoke(self.stored, request)
        if not response.ok:
            raise RuntimeError("plugin_search_failed")
        if not isinstance(response.result, list) or len(response.result) > 100:
            raise RuntimeError("candidate_limit")
        result: list[Candidate] = []
        try:
            for item in response.result:
                candidate = item if isinstance(item, Candidate) else Candidate.model_validate(item)
                if (candidate.source_id != self.stored.manifest.plugin_id or
                        candidate.source_version != self.stored.manifest.version):
                    raise RuntimeError("candidate_identity")
                result.append(candidate)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("candidate_invalid") from exc
        return tuple(result)

    def _download_budget_ms(self) -> int:
        if self.resolve_stream_timeout_ms is not None:
            return self.resolve_stream_timeout_ms
        return max(1, min(_MAX_TIMEOUT_MS, math.ceil(float(getattr(self.client, "timeout", 30.0)) * 1000)))

    async def download(self, candidate: Candidate) -> DownloadMetadata:
        """Resolve one selected candidate, then stream it through the main-owned transport."""
        if self.transport is None:
            raise RuntimeError("download_failed")
        deadline = time.monotonic() + self._download_budget_ms() / 1000
        media = await self.client.resolve(self.stored, candidate, timeout_ms=_remaining_ms(deadline))
        if media.candidate_id != candidate.item_id:
            raise RuntimeError("candidate_mismatch")
        return await self.transport.open(media, allowed_hosts=self.stored.manifest.allowed_hosts,
                                         timeout_ms=_remaining_ms(deadline))

    async def health(self) -> bool:
        result = await self.client.health(self.stored, timeout_ms=self.health_timeout_ms)
        if not isinstance(result, bool):
            raise RuntimeError("plugin_health_failed")
        return result
