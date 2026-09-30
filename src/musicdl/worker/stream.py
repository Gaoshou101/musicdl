"""Shared Redis stream delivery, recovery and retry mechanics."""
from __future__ import annotations
import asyncio
import hashlib
import inspect
import json
import logging
from typing import Any, Callable
from redis.exceptions import RedisError, ResponseError
from musicdl.config import WECOM_NOTICE_TIMEOUT_SECONDS

LOG = logging.getLogger('musicdl.worker.stream')
TERMINAL_FAILURE_TEXT = '处理失败，请稍后重试。'


async def _call(fn, *args, **kwargs):
    value = fn(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _field(fields: dict, name: str, default=None):
    fields = {((k.decode() if isinstance(k, bytes) else k)): v for k, v in fields.items()}
    value = fields.get(name, default)
    return value.decode() if isinstance(value, bytes) else value

class _StreamWorker:
    def _configure_delivery(self, namespace: str, pending_idle_ms: int, max_attempts: int) -> None:
        if not isinstance(pending_idle_ms, int) or isinstance(pending_idle_ms, bool) or not 1 <= pending_idle_ms <= 604800000:
            raise ValueError("invalid pending idle")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 100:
            raise ValueError("invalid max attempts")
        self.namespace = namespace
        self.pending_idle_ms = pending_idle_ms
        self.max_attempts = max_attempts
        self.dead_letter_stream = f"{namespace}:stream:dead-letter"
        self._groups: set[tuple[str, str]] = set()
        self._claim_cursors: dict[tuple[str, str], str | bytes] = {}

    async def _ensure_group(self, stream, group):
        if (stream, group) in self._groups:
            return
        if not hasattr(self.redis, "xgroup_create"):
            raise RuntimeError("Redis consumer groups are required")
        try: await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc): raise
        self._groups.add((stream, group))

    async def _read(self, stream: str, group: str, *, count: int = 1):
        # This consumer is serial: prefetch would start the pending clock for
        # work that cannot run yet and allow another consumer to reclaim it.
        try:
            return await self._read_available(stream, group, count=count)
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                self._groups.discard((stream, group))
                self._claim_cursors.pop((stream, group), None)
            raise

    async def _read_available(self, stream: str, group: str, *, count: int):
        if hasattr(self.redis, "xautoclaim"):
            claimed = await self.redis.xautoclaim(
                stream, group, self.consumer, self.pending_idle_ms,
                self._claim_cursors.get((stream, group), "0-0"), count=count,
            )
            if isinstance(claimed, (list, tuple)) and len(claimed) >= 2:
                self._claim_cursors[(stream, group)] = claimed[0]
                if claimed[1]:
                    return [(stream, claimed[1])]
        if not hasattr(self.redis, "xreadgroup"): raise RuntimeError("Redis xreadgroup is required")
        return await self.redis.xreadgroup(group, self.consumer, {stream: ">"}, count=count, block=1)

    async def _ack(self, stream, group, message_id):
        if hasattr(self.redis, "xack"):
            await self.redis.xack(stream, group, message_id)

    def _retry_key(self, stream: str, group: str, message_id: Any) -> str:
        value = json.dumps([stream, group, _field({'id': message_id}, 'id', '')], separators=(",", ":"))
        return f"{self.namespace}:worker:retry:{hashlib.sha256(value.encode()).hexdigest()}"

    async def _clear_retry(self, stream: str, group: str, message_id: Any) -> None:
        if not hasattr(self.redis, "delete"):
            return
        await self.redis.delete(self._retry_key(stream, group, message_id))

    async def _ack_then_clear(self, stream: str, group: str, message_id: Any) -> None:
        await self._ack(stream, group, message_id)
        try:
            await self._clear_retry(stream, group, message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _record_failure(self, stream: str, group: str, message_id: Any, user: str = "") -> None:
        key = self._retry_key(stream, group, message_id)
        attempts = int(await self.redis.hincrby(key, "attempts", 1))
        await self.redis.expire(key, self.retry_window_seconds)
        if attempts < self.max_attempts:
            return
        fields = {
            "source_stream": str(stream)[:128],
            "consumer_group": str(group)[:128],
            "message_id": str(_field({"id": message_id}, "id", ""))[:64],
            "consumer": str(self.consumer)[:128],
            "reason": "business_failure",
            "attempts": str(attempts),
        }
        await self.redis.xadd(self.dead_letter_stream, fields, maxlen=1000, approximate=True)
        await self._notify_terminal(stream, message_id, user)
        await self._ack_then_clear(stream, group, message_id)

    async def _notify_terminal(self, stream: str, message_id: Any, user: str) -> None:
        """Deliver the bounded dead-letter notice for one terminal message."""
        if not user:
            return
        try:
            async with asyncio.timeout(WECOM_NOTICE_TIMEOUT_SECONDS):
                await _call(self.wecom.send_text, user, TERMINAL_FAILURE_TEXT)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _run_loop(self, operation: Callable[[], Any], poll_interval: float) -> None:
        backoff = poll_interval
        while True:
            try:
                handled = await operation()
            except (RedisError, OSError) as exc:
                if isinstance(exc, ResponseError) and "NOGROUP" not in str(exc):
                    raise
                LOG.warning("stream temporarily unavailable; retrying in %.2fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(max(backoff * 2, 0.1), 5.0)
                continue
            backoff = poll_interval
            if not handled:
                await asyncio.sleep(poll_interval)
