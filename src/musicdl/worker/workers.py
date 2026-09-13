from __future__ import annotations
import json
import inspect
import asyncio
import hashlib
import math
import secrets
from collections.abc import Mapping
from typing import Any, Callable

from musicdl.ai.models import AIRankResult
from musicdl.media.fallback import download_with_fallback
from musicdl.sources.search import SearchResult, search_sources
from musicdl.wecom.commands import CommandKind, ParsedCommand, parse_command
from musicdl.wecom.results import format_results
from musicdl.wecom.state import RedisStateStore, SelectionContext, SelectionRejected
from .selection import bind_user_selection, get_user_selection, get_selection_for_user, get_selection_for_request, _get_by_token


async def _call(fn, *args, **kwargs):
    value = fn(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _field(fields: dict, name: str, default=None):
    fields = {((k.decode() if isinstance(k, bytes) else k)): v for k, v in fields.items()}
    value = fields.get(name, default)
    return value.decode() if isinstance(value, bytes) else value

def _envelope(fields):
    raw = _field(fields, "payload", "{}")
    outer = json.loads(raw)
    if not isinstance(outer, Mapping):
        raise ValueError("invalid stream envelope")
    inner = outer.get("payload", {})
    if isinstance(inner, str):
        inner = json.loads(inner)
    if not isinstance(inner, Mapping):
        raise ValueError("invalid stream envelope")
    return {**outer, **inner}


def _command(payload: dict[str, Any]) -> ParsedCommand:
    """Read the normalized service command, with an explicit legacy fallback."""
    if "command" not in payload:
        return parse_command(payload.get("content", payload.get("text", "")))
    try:
        kind = CommandKind(payload["command"])
    except (TypeError, ValueError):
        return ParsedCommand(CommandKind.UNSUPPORTED)
    value = payload.get("value")
    if kind is CommandKind.SEARCH:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
            raise ValueError("invalid search command")
        value = value.strip()
    return ParsedCommand(kind, value)


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

    async def _ensure_group(self, stream, group):
        if not hasattr(self.redis, "xgroup_create"):
            raise RuntimeError("Redis consumer groups are required")
        try: await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc): raise

    async def _read(self, stream: str, group: str, *, count: int = 10):
        if hasattr(self.redis, "xautoclaim"):
            claimed = await self.redis.xautoclaim(
                stream, group, self.consumer, self.pending_idle_ms, "0-0", count=count,
            )
            if isinstance(claimed, (list, tuple)) and len(claimed) >= 2 and claimed[1]:
                return [(stream, claimed[1])]
        if not hasattr(self.redis, "xreadgroup"): raise RuntimeError("Redis xreadgroup is required")
        return await self.redis.xreadgroup(group, self.consumer, {stream: ">"}, count=count, block=1)

    async def _ack(self, stream, group, message_id):
        if hasattr(self.redis, "xack"):
            await self.redis.xack(stream, group, message_id)

    def _retry_key(self, stream: str, message_id: Any) -> str:
        value = f"{stream}:{_field({'id': message_id}, 'id', '')}"
        return f"{self.namespace}:worker:retry:{hashlib.sha256(value.encode()).hexdigest()}"

    async def _clear_retry(self, stream: str, message_id: Any) -> None:
        if not hasattr(self.redis, "delete"):
            return
        await self.redis.delete(self._retry_key(stream, message_id))

    async def _ack_then_clear(self, stream: str, group: str, message_id: Any) -> None:
        await self._ack(stream, group, message_id)
        try:
            await self._clear_retry(stream, message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _record_failure(self, stream: str, group: str, message_id: Any, user: str = "") -> None:
        key = self._retry_key(stream, message_id)
        attempts = int(await self.redis.hincrby(key, "attempts", 1))
        await self.redis.expire(key, 604800)
        if attempts < self.max_attempts:
            return
        fields = {
            "source_stream": str(stream)[:128],
            "message_id": str(_field({"id": message_id}, "id", ""))[:64],
            "consumer": str(self.consumer)[:128],
            "reason": "business_failure",
            "attempts": str(attempts),
        }
        await self.redis.xadd(self.dead_letter_stream, fields, maxlen=1000, approximate=True)
        if user:
            try:
                await _call(self.wecom.send_text, user, "处理失败，请稍后重试。")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        await self._ack_then_clear(stream, group, message_id)

    async def _run_loop(self, operation: Callable[[], Any], poll_interval: float) -> None:
        while True:
            handled = await operation()
            if not handled:
                await asyncio.sleep(poll_interval)


class MessageWorker(_StreamWorker):
    def __init__(self, redis: Any, registry: Any, wecom: Any, *, state: RedisStateStore | None = None,
                 ai_ranker: Callable | None = None, group: str = "musicdl-workers", consumer: str | None = None,
                 max_results: int = 10, search_timeout: float = 10.0, selection_ttl: int = 600,
                 pending_idle_ms: int = 30001, max_attempts: int = 3):
        self.redis, self.registry, self.wecom = redis, registry, wecom
        self.state = state or RedisStateStore(redis)
        if not isinstance(search_timeout, (int, float)) or isinstance(search_timeout, bool) or not math.isfinite(search_timeout) or search_timeout <= 0:
            raise ValueError("invalid search timeout")
        self.ai_ranker, self.group = ai_ranker, group
        self.consumer = consumer if consumer is not None else f"message-{secrets.token_hex(12)}"
        self.max_results, self.search_timeout = max_results, search_timeout
        if not isinstance(selection_ttl, int) or isinstance(selection_ttl, bool) or not 60 <= selection_ttl <= 86400:
            raise ValueError("invalid selection ttl")
        self.selection_ttl = selection_ttl
        self._configure_delivery(self.state.namespace, pending_idle_ms, max_attempts)
        if self.pending_idle_ms <= self.search_timeout * 1000:
            raise ValueError("pending idle must exceed search timeout")
        self.stream = self.state.message_stream

    async def handle(self, envelope: dict[str, Any]) -> str | None:
        payload = envelope.get("payload", envelope)
        if isinstance(payload, str): payload = json.loads(payload)
        # enqueue_message stores routing metadata beside the user payload.
        payload = {**envelope, **payload}
        command = _command(payload)
        if command.kind is not CommandKind.SEARCH:
            return None
        result = await search_sources(self.registry, str(command.value), timeout=self.search_timeout)
        if self.ai_ranker:
            try:
                ranked = await _call(self.ai_ranker, result, str(command.value))
                result = ranked.search if isinstance(ranked, AIRankResult) else ranked
            except asyncio.CancelledError: raise
            except Exception: pass
        if not result.candidates:
            await _call(self.wecom.send_text, str(payload["from_user"]), "没有找到匹配结果。")
            return None
        context = SelectionContext(str(payload["corp_id"]), str(payload["from_user"]), str(payload["request_id"]), result.version,
                                   {i: c.item_id for i, c in enumerate(result.candidates[:100], 1)})
        token = await self.state.issue_selection(context, ttl=self.selection_ttl)
        await bind_user_selection(
            self.redis, token, context, query=str(command.value),
            candidates={i: c.model_dump(mode="json") for i, c in enumerate(result.candidates[:100], 1)},
            ttl=self.selection_ttl, namespace=self.namespace,
        )
        prompt = "\n\n回复序号下载。"
        text = format_results(result.candidates, max_items=self.max_results, max_bytes=2048 - len(prompt.encode("utf-8"))) or "没有找到匹配结果。"
        await _call(self.wecom.send_text, context.from_user, text + prompt)
        return token

    async def run_once(self) -> int:
        handled = 0
        await self._ensure_group(self.stream, self.group)
        for _, messages in await self._read(self.stream, self.group):
            for message_id, fields in messages:
                try:
                    envelope = _envelope(fields)
                except (ValueError, KeyError, json.JSONDecodeError):
                    try:
                        await self._ack_then_clear(self.stream, self.group, message_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    continue
                try:
                    await self.handle(envelope)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    try:
                        await self._record_failure(self.stream, self.group, message_id, str(envelope.get("from_user", "")))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    continue
                try:
                    await self._ack_then_clear(self.stream, self.group, message_id)
                    handled += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        return handled

    async def run_forever(self, *, poll_interval: float = 0.05) -> None:
        if not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool) or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("invalid poll interval")
        await self._run_loop(self.run_once, float(poll_interval))


class JobWorker(_StreamWorker):
    def __init__(self, redis: Any, wecom: Any, sources: dict, media_root: str, *, state: RedisStateStore | None = None,
                 refresh: Callable | None = None, group: str = "musicdl-workers", consumer: str | None = None,
                 job_ttl: int = 86400, pending_idle_ms: int = 30001, max_attempts: int = 3,
                 job_timeout: float = 10.0):
        self.redis, self.wecom, self.sources, self.media_root = redis, wecom, sources, media_root
        self.state, self.refresh = state or RedisStateStore(redis), refresh
        if not isinstance(job_ttl, int) or isinstance(job_ttl, bool) or not 60 <= job_ttl <= 604800:
            raise ValueError("invalid job ttl")
        self.job_ttl = job_ttl
        if not isinstance(job_timeout, (int, float)) or isinstance(job_timeout, bool) or not math.isfinite(job_timeout) or job_timeout <= 0:
            raise ValueError("invalid job timeout")
        self.job_timeout = float(job_timeout)
        self._configure_delivery(self.state.namespace, pending_idle_ms, max_attempts)
        self.group = group
        self.consumer = consumer if consumer is not None else f"job-{secrets.token_hex(12)}"
        if self.pending_idle_ms <= self.job_timeout * 1000:
            raise ValueError("pending idle must exceed job timeout")
        self.stream = self.state.job_stream
        self.selection_stream = self.state.message_stream
        self.selection_group = group + "-selection"

    async def handle_selection(self, token: str, context: SelectionContext, index: int):
        return await self.state.consume_selection(token, context, index, ttl=self.job_ttl)

    async def handle_user_selection(self, token: str, index: int):
        data = await _get_by_token(self.redis, token, namespace=self.namespace)
        if data is None:
            raise SelectionRejected()
        context = SelectionContext(data["corp_id"], data["from_user"], data["request_id"], data["version"], {int(k): v["item_id"] if isinstance(v, dict) else v for k, v in data["candidates"].items()})
        return await self.handle_selection(token, context, index)

    async def run_selection_once(self) -> int:
        await self._ensure_group(self.selection_stream, self.selection_group); count = 0
        for _, messages in await self._read(self.selection_stream, self.selection_group):
            for message_id, fields in messages:
                envelope: dict[str, Any] = {}
                try:
                    envelope = _envelope(fields); command = _command(envelope)
                    if command.kind is not CommandKind.SELECT: raise SelectionRejected()
                    index, user, corp = command.value, envelope.get("from_user", ""), envelope.get("corp_id", "")
                    data = await get_user_selection(self.redis, corp, user, namespace=self.namespace)
                    if not data: raise SelectionRejected()
                    await self.handle_selection(data["token"], SelectionContext(data["corp_id"], data["from_user"], data["request_id"], data["version"], {int(k): v["item_id"] if isinstance(v, dict) else v for k,v in data["candidates"].items()}), int(index))
                except asyncio.CancelledError:
                    raise
                except (ValueError, KeyError, json.JSONDecodeError, SelectionRejected):
                    try:
                        await self._ack_then_clear(self.selection_stream, self.selection_group, message_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    continue
                except Exception:
                    try:
                        user = str(envelope.get("from_user", ""))
                        await self._record_failure(self.selection_stream, self.selection_group, message_id, user)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    continue
                try:
                    await self._ack_then_clear(self.selection_stream, self.selection_group, message_id)
                    count += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        return count

    async def handle_job(self, job: dict[str, Any]):
        if "candidate" not in job:
            route = await get_selection_for_request(self.redis, str(job["request_id"]), namespace=self.namespace)
            if not route: raise SelectionRejected()
            candidate = route["candidates"].get(str(job.get("index")), {})
            job = {**job, "candidate": candidate, "from_user": route["from_user"], "query": route.get("query", "")}
        candidate = job["candidate"]
        if isinstance(candidate, dict):
            from musicdl.sources.models import Candidate
            candidate = Candidate.model_validate(candidate)
        if self.refresh is None: raise RuntimeError("refresh callback is required")
        refresh = self.refresh
        async with asyncio.timeout(self.job_timeout):
            result = await download_with_fallback(candidate, self.sources, self.media_root, request_id=str(job["request_id"]), query=str(job.get("query", candidate.title)), refresh=refresh)
        user = job.get("from_user") or job.get("user")
        if user:
            message = f"下载成功：{result.download.relative_path}" if result.download else f"下载失败，已重试：{result.download_error or result.refresh_error or 'unknown'}"
            await _call(self.wecom.send_text, user, message)
        return result

    async def _job_user(self, job: dict[str, Any]) -> str:
        direct = job.get("from_user") or job.get("user")
        if direct:
            return str(direct)
        request_id = job.get("request_id")
        if request_id:
            try:
                route = await get_selection_for_request(self.redis, str(request_id), namespace=self.namespace)
                if route and route.get("from_user"):
                    return str(route["from_user"])
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        return ""

    async def run_once(self) -> int:
        await self._ensure_group(self.stream, self.group)
        handled = 0
        for _, messages in await self._read(self.stream, self.group):
            for message_id, fields in messages:
                try:
                    normalized = {k.decode() if isinstance(k, bytes) else k: (v.decode() if isinstance(v, bytes) else v) for k,v in fields.items()}
                    raw = normalized.get("payload")
                    job = json.loads(raw) if raw else normalized
                    if not isinstance(job, dict):
                        raise ValueError("invalid job payload")
                except (ValueError, KeyError, json.JSONDecodeError):
                    try:
                        await self._ack_then_clear(self.stream, self.group, message_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    continue
                try:
                    await self.handle_job(job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    try:
                        await self._record_failure(self.stream, self.group, message_id, await self._job_user(job))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    continue
                try:
                    await self._ack_then_clear(self.stream, self.group, message_id)
                    handled += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        return handled

    async def run_forever(self, *, poll_interval: float = 0.05) -> None:
        if not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool) or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("invalid poll interval")
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._run_loop(self.run_selection_once, float(poll_interval)))
            tasks.create_task(self._run_loop(self.run_once, float(poll_interval)))
