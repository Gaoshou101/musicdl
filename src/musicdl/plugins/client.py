"""Main-side client for the bounded plugin runner protocol."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import stat
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from musicdl.contracts.plugin import (
    MAX_HTTP_ACTIONS, MAX_PAYLOAD_BYTES, MAX_SOURCE_BYTES, HttpAction, HttpObservation,
    PluginInvocation, PluginRequest, PluginResponse, PluginStep, ResolvedMedia,
)
from musicdl.sources.models import Candidate
from .broker import ActionDenied, HttpsActionBroker
from .store import StoredPlugin


class PluginClientError(RuntimeError):
    """Stable internal client failure which must not be remapped."""


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
    def _normalize_timeout_ms(timeout_ms: int | None, default_timeout: float) -> int:
        if timeout_ms is None:
            return max(1, min(30_000, math.ceil(default_timeout * 1000)))
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 30_000:
            raise ValueError("invalid_timeout")
        return timeout_ms

    async def resolve(
        self,
        stored: StoredPlugin,
        candidate: Candidate,
        *,
        timeout_ms: int | None = None,
    ) -> ResolvedMedia:
        request_timeout_ms = self._normalize_timeout_ms(timeout_ms, self.timeout)
        try:
            request = PluginRequest(
                protocol="musicdl.plugin/v1",
                request_id=uuid4(),
                operation="resolve",
                timeout_ms=request_timeout_ms,
                payload={"candidate": candidate.public_representation},
            )
            response = await self.invoke(stored, request)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
            if str(exc) in {"runner_invalid_json", "runner_response_too_large", "runner_response_mismatch"}:
                raise RuntimeError("plugin_resolve_invalid") from None
            raise RuntimeError("plugin_resolve_failed") from None

        if not response.ok:
            raise RuntimeError("plugin_resolve_failed")
        try:
            resolved = ResolvedMedia.model_validate(response.result)
        except (ValidationError, TypeError, ValueError):
            raise RuntimeError("plugin_resolve_invalid") from None
        if resolved.candidate_id != candidate.item_id:
            raise RuntimeError("candidate_mismatch")
        return resolved

    async def health(
        self,
        stored: StoredPlugin,
        *,
        timeout_ms: int | None = None,
    ) -> bool:
        request_timeout_ms = self._normalize_timeout_ms(timeout_ms, self.timeout)
        try:
            request = PluginRequest(
                protocol="musicdl.plugin/v1",
                request_id=uuid4(),
                operation="health",
                timeout_ms=request_timeout_ms,
            )
            response = await self.invoke(stored, request)
            if not response.ok or not isinstance(response.result, bool):
                raise ValueError("invalid_health_response")
            return response.result
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("plugin_health_failed") from None

    @staticmethod
    def _source(stored: StoredPlugin) -> str:
        fd = None
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(os.fspath(stored.path), flags)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise RuntimeError("source_invalid")
            chunks: list[bytes] = []
            total = 0
            while total <= MAX_SOURCE_BYTES:
                chunk = os.read(fd, min(16 * 1024, MAX_SOURCE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise RuntimeError("source_invalid")
            data = b"".join(chunks)
            if len(data) != info.st_size:
                raise RuntimeError("source_invalid")
            if hashlib.sha256(data).hexdigest() != stored.manifest.sha256:
                raise RuntimeError("source_digest_mismatch")
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("source_invalid") from exc
        except RuntimeError:
            raise
        except (OSError, ValueError) as exc:
            raise RuntimeError("source_unavailable") from exc
        finally:
            if fd is not None:
                os.close(fd)

    async def invoke(self, stored: StoredPlugin, request: PluginRequest) -> PluginResponse:
        if not stored.enabled:
            raise RuntimeError("plugin_disabled")
        if request.operation not in stored.manifest.operations:
            raise RuntimeError("operation_not_declared")
        deadline = time.monotonic() + min(self.timeout, request.timeout_ms / 1000)
        actions: list[HttpAction] = []
        observations: list[HttpObservation] = []
        seen: set[str] = set()
        async def loop() -> PluginResponse:
            while True:
                # Re-read immutable content immediately before every send.
                current_source = await asyncio.to_thread(self._source, stored)
                invocation = PluginInvocation(manifest=stored.manifest, source=current_source,
                                              request=request, actions=tuple(actions), observations=tuple(observations))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("runner_timeout")
                try:
                    async with self.http.stream("POST", self.service_url,
                            content=invocation.model_dump_json().encode(),
                            timeout=remaining, follow_redirects=False) as response:
                        if response.status_code < 200 or response.status_code >= 300:
                            raise RuntimeError("runner_http_error")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_PAYLOAD_BYTES:
                                raise PluginClientError("runner_response_too_large")
                except PluginClientError:
                    raise
                except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                    raise RuntimeError("runner_timeout") from exc
                except httpx.HTTPError as exc:
                    raise RuntimeError("runner_http_error") from exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise RuntimeError("runner_http_error") from exc
                try:
                    step = PluginStep.model_validate_json(bytes(body))
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
                    observation = await asyncio.to_thread(
                        self.broker.fetch, action, stored.manifest.allowed_hosts, timeout=remaining)
                except ActionDenied as exc:
                    raise RuntimeError("runner_timeout" if exc.code == "timeout" else "action_denied") from exc
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
