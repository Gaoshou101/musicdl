"""Main-side client for the bounded plugin runner protocol."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from musicdl.contracts.plugin import (
    MAX_HTTP_ACTIONS, HttpAction, HttpObservation, PluginInvocation, PluginRequest,
    PluginResponse, PluginStep,
)
from .broker import ActionDenied, HttpsActionBroker
from .store import StoredPlugin


class PluginClient:
    def __init__(self, service_url: str, *, broker: HttpsActionBroker | None = None,
                 http_client: httpx.AsyncClient | None = None, timeout: float = 30.0):
        self.service_url = service_url.rstrip("/") + "/v1/execute"
        self.broker = broker
        self._owned_client = http_client is None
        self.http = http_client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        if not (0 < timeout <= 30):
            raise ValueError("invalid_timeout")
        self.timeout = timeout
        # Injected clients must make the transport policy observable; never mutate one.
        if getattr(self.http, "_trust_env", getattr(self.http, "trust_env", None)) is not False:
            raise ValueError("http_client_must_disable_environment")
        if getattr(self.http, "follow_redirects", None) is not False:
            raise ValueError("http_client_must_disable_redirects")

    async def aclose(self) -> None:
        if self._owned_client:
            await self.http.aclose()

    @staticmethod
    def _source(stored: StoredPlugin) -> str:
        try:
            data = Path(stored.path).read_bytes()
        except (OSError, ValueError) as exc:
            raise RuntimeError("source_unavailable") from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != stored.manifest.sha256:
            raise RuntimeError("source_digest_mismatch")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("source_invalid") from exc

    async def invoke(self, stored: StoredPlugin, request: PluginRequest) -> PluginResponse:
        if not stored.enabled:
            raise RuntimeError("plugin_disabled")
        if request.operation not in stored.manifest.operations:
            raise RuntimeError("operation_not_declared")
        source = self._source(stored)
        deadline = time.monotonic() + min(self.timeout, request.timeout_ms / 1000)
        actions: list[HttpAction] = []
        observations: list[HttpObservation] = []
        seen: set[str] = set()
        async def loop() -> PluginResponse:
            while True:
                # Re-read immutable content immediately before every send.
                current_source = self._source(stored)
                invocation = PluginInvocation(manifest=stored.manifest, source=current_source,
                                              request=request, actions=tuple(actions), observations=tuple(observations))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("runner_timeout")
                try:
                    response = await self.http.post(self.service_url,
                        content=invocation.model_dump_json().encode(),
                        timeout=remaining, follow_redirects=False)
                except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                    raise RuntimeError("runner_timeout") from exc
                except httpx.HTTPError as exc:
                    raise RuntimeError("runner_http_error") from exc
                if response.status_code < 200 or response.status_code >= 300:
                    raise RuntimeError("runner_http_error")
                try:
                    step = PluginStep.model_validate(response.json())
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("runner_invalid_json") from exc
                if step.response is not None:
                    final = step.response
                    if final.request_id != request.request_id or final.operation != request.operation:
                        raise RuntimeError("runner_response_mismatch")
                    return final
                action = step.action
                assert action is not None
                if len(actions) >= MAX_HTTP_ACTIONS:
                    raise RuntimeError("action_limit")
                if action.action_id in seen:
                    raise RuntimeError("action_repeated")
                seen.add(action.action_id)
                actions.append(action)
                if self.broker is None:
                    raise RuntimeError("action_denied")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("runner_timeout")
                try:
                    observation = await asyncio.wait_for(
                        asyncio.to_thread(self.broker.fetch, action, stored.manifest.allowed_hosts), remaining)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("runner_timeout") from exc
                except ActionDenied as exc:
                    raise RuntimeError("action_denied") from exc
                except Exception as exc:
                    raise RuntimeError("action_failed") from exc
                if observation.action_id != action.action_id:
                    raise RuntimeError("observation_mismatch")
                observations.append(observation)
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                return await loop()
        except TimeoutError as exc:
            raise RuntimeError("runner_timeout") from exc
