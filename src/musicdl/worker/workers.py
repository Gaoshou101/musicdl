from __future__ import annotations
import json
import inspect
import asyncio
from typing import Any, Callable

from musicdl.ai.models import AIRankResult
from musicdl.media.fallback import download_with_fallback
from musicdl.sources.search import SearchResult, search_sources
from musicdl.wecom.commands import CommandKind, parse_command
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
    inner = outer.get("payload", {})
    return {**outer, **(json.loads(inner) if isinstance(inner, str) else inner)}


class _StreamWorker:
    async def _ensure_group(self, stream, group):
        if not hasattr(self.redis, "xgroup_create"):
            raise RuntimeError("Redis consumer groups are required")
        try: await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc): raise

    async def _read(self, stream: str, group: str, *, count: int = 10):
        if not hasattr(self.redis, "xreadgroup"): raise RuntimeError("Redis xreadgroup is required")
        return await self.redis.xreadgroup(group, self.consumer, {stream: ">"}, count=count, block=1)

    async def _ack(self, stream, group, message_id):
        if hasattr(self.redis, "xack"):
            await self.redis.xack(stream, group, message_id)


class MessageWorker(_StreamWorker):
    def __init__(self, redis: Any, registry: Any, wecom: Any, *, state: RedisStateStore | None = None,
                 ai_ranker: Callable | None = None, group: str = "musicdl-workers", consumer: str = "message-worker",
                 max_results: int = 10, search_timeout: float = 10.0):
        self.redis, self.registry, self.wecom = redis, registry, wecom
        self.state = state or RedisStateStore(redis)
        self.ai_ranker, self.group, self.consumer = ai_ranker, group, consumer
        self.max_results, self.search_timeout = max_results, search_timeout
        self.stream = self.state.message_stream

    async def handle(self, envelope: dict[str, Any]) -> str | None:
        payload = envelope.get("payload", envelope)
        if isinstance(payload, str): payload = json.loads(payload)
        # enqueue_message stores routing metadata beside the user payload.
        payload = {**envelope, **payload}
        command = parse_command(payload.get("content", payload.get("text", "")))
        if command.kind is not CommandKind.SEARCH:
            return None
        result = await search_sources(self.registry, str(command.value), timeout=self.search_timeout)
        if self.ai_ranker:
            try:
                ranked = await _call(self.ai_ranker, result)
                result = ranked.search if isinstance(ranked, AIRankResult) else ranked
            except asyncio.CancelledError: raise
            except Exception: pass
        if not result.candidates:
            await _call(self.wecom.send_text, str(payload["from_user"]), "没有找到匹配结果。")
            return None
        context = SelectionContext(str(payload["corp_id"]), str(payload["from_user"]), str(payload["request_id"]), result.version,
                                   {i: c.item_id for i, c in enumerate(result.candidates[:100], 1)})
        token = await self.state.issue_selection(context)
        await bind_user_selection(self.redis, token, context, query=str(command.value), candidates={i: c.model_dump(mode="json") for i, c in enumerate(result.candidates[:100], 1)})
        text = format_results(result.candidates, max_items=self.max_results) or "没有找到匹配结果。"
        await _call(self.wecom.send_text, context.from_user, text + "\n\n回复序号下载。")
        return token

    async def run_once(self) -> int:
        handled = 0
        await self._ensure_group(self.stream, self.group)
        for _, messages in await self._read(self.stream, self.group):
            for message_id, fields in messages:
                try:
                    envelope = _envelope(fields)
                except (ValueError, KeyError, json.JSONDecodeError):
                    await self._ack(self.stream, self.group, message_id)
                    continue
                try:
                    await self.handle(envelope); handled += 1; await self._ack(self.stream, self.group, message_id)
                except Exception:
                    continue
        return handled


class JobWorker(_StreamWorker):
    def __init__(self, redis: Any, wecom: Any, sources: dict, media_root: str, *, state: RedisStateStore | None = None,
                 refresh: Callable | None = None, group: str = "musicdl-workers", consumer: str = "job-worker"):
        self.redis, self.wecom, self.sources, self.media_root = redis, wecom, sources, media_root
        self.state, self.refresh = state or RedisStateStore(redis), refresh
        self.group, self.consumer = group, consumer
        self.stream = self.state.job_stream
        self.selection_stream = self.state.message_stream
        self.selection_group = group + "-selection"

    async def handle_selection(self, token: str, context: SelectionContext, index: int):
        return await self.state.consume_selection(token, context, index)

    async def handle_user_selection(self, token: str, index: int):
        data = await _get_by_token(self.redis, token)
        if data is None:
            raise SelectionRejected()
        context = SelectionContext(data["corp_id"], data["from_user"], data["request_id"], data["version"], {int(k): v["item_id"] if isinstance(v, dict) else v for k, v in data["candidates"].items()})
        return await self.handle_selection(token, context, index)

    async def run_selection_once(self) -> int:
        await self._ensure_group(self.selection_stream, self.selection_group); count = 0
        for _, messages in await self._read(self.selection_stream, self.selection_group):
            for message_id, fields in messages:
                try:
                    envelope = _envelope(fields); text, user, corp = envelope.get("content", ""), envelope.get("from_user", ""), envelope.get("corp_id", "")
                    data = await get_user_selection(self.redis, corp, user)
                    if not data: raise SelectionRejected()
                    await self.handle_selection(data["token"], SelectionContext(data["corp_id"], data["from_user"], data["request_id"], data["version"], {int(k): v["item_id"] if isinstance(v, dict) else v for k,v in data["candidates"].items()}), int(str(text).strip()))
                    await self._ack(self.selection_stream, self.selection_group, message_id); count += 1
                except (ValueError, KeyError, json.JSONDecodeError, SelectionRejected): await self._ack(self.selection_stream, self.selection_group, message_id)
        return count

    async def handle_job(self, job: dict[str, Any]):
        if "candidate" not in job:
            route = await get_selection_for_request(self.redis, str(job["request_id"]))
            if not route: raise SelectionRejected()
            candidate = route["candidates"].get(str(job.get("index")), {})
            job = {**job, "candidate": candidate, "from_user": route["from_user"], "query": route.get("query", "")}
        candidate = job["candidate"]
        if isinstance(candidate, dict):
            from musicdl.sources.models import Candidate
            candidate = Candidate.model_validate(candidate)
        if self.refresh is None: raise RuntimeError("refresh callback is required")
        refresh = self.refresh
        result = await download_with_fallback(candidate, self.sources, self.media_root, request_id=str(job["request_id"]), query=str(job.get("query", candidate.title)), refresh=refresh)
        user = job.get("from_user") or job.get("user")
        if user:
            message = f"下载成功：{result.download.relative_path}" if result.download else f"下载失败，已重试：{result.download_error or result.refresh_error or 'unknown'}"
            await _call(self.wecom.send_text, user, message)
        return result

    async def run_once(self) -> int:
        await self._ensure_group(self.stream, self.group)
        handled = 0
        for _, messages in await self._read(self.stream, self.group):
            for message_id, fields in messages:
                try:
                    normalized = {k.decode() if isinstance(k, bytes) else k: (v.decode() if isinstance(v, bytes) else v) for k,v in fields.items()}
                    raw = normalized.get("payload")
                    job = json.loads(raw) if raw else normalized
                except (ValueError, KeyError, json.JSONDecodeError):
                    await self._ack(self.stream, self.group, message_id)
                    continue
                try:
                    await self.handle_job(job); handled += 1; await self._ack(self.stream, self.group, message_id)
                except Exception:
                    continue
        return handled
