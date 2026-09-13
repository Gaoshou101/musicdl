"""Phase 2 MusicSource adapter for an enabled stored plugin."""
from __future__ import annotations

from uuid import uuid4

from musicdl.contracts.plugin import PluginRequest
from musicdl.sources.models import Candidate, normalize_text
from .client import PluginClient
from .store import StoredPlugin


class PluginSource:
    def __init__(self, stored: StoredPlugin, client: PluginClient):
        self.stored = stored
        self.client = client

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
