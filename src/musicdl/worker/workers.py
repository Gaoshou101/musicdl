from __future__ import annotations
import json
import inspect
import asyncio
import hashlib
import math
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from musicdl.ai.models import AIRankResult
from musicdl.media.fallback import download_with_fallback
from musicdl.media.models import ArtifactRecord, FallbackResult, MediaError
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult, search_sources
from musicdl.wecom.commands import CommandKind, ParsedCommand, parse_command
from musicdl.wecom.results import format_results
from musicdl.media.validation import validated_destination
from musicdl.wecom.state import EffectLease, RedisStateStore, SelectionContext, SelectionRejected
from .selection import bind_user_selection, get_user_selection, get_selection_for_user, get_selection_for_request, _get_by_token


REDIS_OVERHEAD_SECONDS = 1.0
WECOM_NOTICE_TIMEOUT_SECONDS = 10.0
TERMINAL_FAILURE_TEXT = "处理失败，请稍后重试。"
EFFECT_REPLAY_LIMIT = 100
_MEDIA_TYPES = {"mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4", "ogg": "audio/ogg"}
_NOTICE_CODES = {
    "success_notice": "success_notice_uncertain",
    "selection_prompt": "prompt_uncertain",
    "terminal_failure_notice": "terminal_failure_notice_uncertain",
}


class JobDeferred(RuntimeError):
    """A live lease owns this job effect; leave the message pending without retry or XACK."""


class EffectUncertain(RuntimeError):
    """A durable effect reached a terminal uncertain stage and must not be replayed."""

    def __init__(self, code: str = "effect_uncertain"):
        self.code = code
        super().__init__(code)


def _effect_code(record: Any, default: str) -> str:
    result = getattr(record, "result", None) or {}
    code = result.get("code") if isinstance(result, dict) else None
    return code if isinstance(code, str) and 0 < len(code) <= 64 else default


class _EffectGuard:
    """Claim, fence, and durably complete one job effect around an external call."""

    def __init__(self, state: Any, job_id: str, effect: str, owner: str, *, deadline: float,
                 ttl: int, overhead: float, pending_idle_ms: int, clock: Callable[[], float]):
        self.state, self.job_id, self.effect, self.owner = state, job_id, effect, owner
        self.deadline, self.ttl, self.overhead = deadline, ttl, overhead
        self.pending_idle_ms, self.clock = pending_idle_ms, clock

    def lease_ms(self) -> int:
        """Derive the lease from the one handler deadline plus a bounded Redis allowance."""
        remaining = max(0.0, self.deadline - self.clock())
        return max(1000, min(self.pending_idle_ms - 1000, math.ceil((remaining + self.overhead) * 1000)))

    async def claim(self):
        """Return ``(lease, None)`` when this worker may run the external call."""
        record = await self.state.begin_job_effect(self.job_id, self.effect, self.owner,
                                                   lease_ms=self.lease_ms(), ttl=self.ttl)
        if isinstance(record, EffectLease):
            return record, None
        if record.status == "busy":
            raise JobDeferred()
        return None, record

    async def external(self, lease: EffectLease) -> None:
        """Renew the owner/fence lease and mark the external stage immediately before the call."""
        await self.state.renew_job_effect(self.job_id, self.effect, self.owner, lease.fence,
                                          lease_ms=self.lease_ms())
        await self.state.begin_external_effect(self.job_id, self.effect, self.owner, lease.fence, ttl=self.ttl)

    async def complete(self, lease: EffectLease, result: dict[str, Any]) -> None:
        await self.state.complete_job_effect(self.job_id, self.effect, self.owner, lease.fence, result, ttl=self.ttl)

    async def complete_quietly(self, lease: EffectLease, result: dict[str, Any]) -> None:
        try:
            await self.complete(lease, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def uncertain(self, lease: EffectLease, code: str) -> None:
        await self.state.mark_job_effect_uncertain(self.job_id, self.effect, self.owner, lease.fence, code,
                                                   ttl=self.ttl)

    async def uncertain_quietly(self, lease: EffectLease, code: str) -> None:
        try:
            await self.uncertain(lease, code)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _call(fn, *args, **kwargs):
    value = fn(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _positive_seconds(value, name: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name)
    return float(value)


def _retry_window(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 604800:
        raise ValueError("invalid retry window")
    return value


def _context_from_route(data: Mapping[str, Any]) -> SelectionContext:
    """Rebuild the frozen selection context stored beside a token."""
    candidates = {}
    for key, value in (data.get("candidates") or {}).items():
        candidates[int(key)] = value if isinstance(value, Candidate) else Candidate.model_validate(value)
    generation = data.get("generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise SelectionRejected()
    return SelectionContext(str(data["corp_id"]), str(data["from_user"]), str(data["request_id"]),
                            str(data["version"]), candidates, query=str(data.get("query", "")),
                            selection_generation=generation)


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
        await self.redis.expire(key, self.retry_window_seconds)
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
        await self._notify_terminal(stream, message_id, user)
        await self._ack_then_clear(stream, group, message_id)

    async def _notify_terminal(self, stream: str, message_id: Any, user: str) -> None:
        """Deliver the bounded dead-letter notice for one terminal message."""
        if not user:
            return
        try:
            await _call(self.wecom.send_text, user, TERMINAL_FAILURE_TEXT)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _run_loop(self, operation: Callable[[], Any], poll_interval: float) -> None:
        while True:
            handled = await operation()
            if not handled:
                await asyncio.sleep(poll_interval)


class MessageWorker(_StreamWorker):
    def __init__(self, redis: Any, registry: Any, wecom: Any, *, state: RedisStateStore | None = None,
                 ai_ranker: Callable | None = None, group: str = "musicdl-workers", consumer: str | None = None,
                 max_results: int = 10, search_timeout: float = 10.0, selection_ttl: int = 600,
                 pending_idle_ms: int = 30001, max_attempts: int = 3, retry_window_seconds: int = 86400):
        self.redis, self.registry, self.wecom = redis, registry, wecom
        self.state = state or RedisStateStore(redis)
        self.retry_window_seconds = _retry_window(retry_window_seconds)
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
        snapshot = {i: c for i, c in enumerate(result.candidates[:100], 1)}
        context = SelectionContext(str(payload["corp_id"]), str(payload["from_user"]), str(payload["request_id"]),
                                   result.version, snapshot, query=str(command.value), selection_generation=0)
        token = await self.state.issue_selection(context, ttl=self.selection_ttl)
        await bind_user_selection(self.redis, token, context, ttl=self.selection_ttl, namespace=self.namespace)
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
                 job_timeout: float = 10.0, resolve_stream_timeout: float | None = None,
                 refresh_timeout: float | None = None, health_timeout: float = 10.0,
                 retry_window_seconds: int = 86400, selection_ttl: int = 600, max_results: int = 10,
                 redis_overhead_seconds: float = REDIS_OVERHEAD_SECONDS,
                 wecom_notice_timeout: float = WECOM_NOTICE_TIMEOUT_SECONDS):
        self.redis, self.wecom, self.sources, self.media_root = redis, wecom, sources, media_root
        self.state, self.refresh = state or RedisStateStore(redis), refresh
        if not isinstance(selection_ttl, int) or isinstance(selection_ttl, bool) or not 60 <= selection_ttl <= 86400:
            raise ValueError("invalid selection ttl")
        self.selection_ttl = selection_ttl
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 100:
            raise ValueError("invalid max results")
        self.max_results = max_results
        self.redis_overhead_seconds = _positive_seconds(redis_overhead_seconds, "invalid redis overhead")
        self.wecom_notice_timeout = _positive_seconds(wecom_notice_timeout, "invalid wecom notice timeout")
        self.resolve_stream_timeout = _positive_seconds(resolve_stream_timeout, "invalid resolve stream timeout")
        self.refresh_timeout = _positive_seconds(refresh_timeout, "invalid refresh timeout")
        self.health_timeout = _positive_seconds(health_timeout, "invalid health timeout")
        self.retry_window_seconds = _retry_window(retry_window_seconds)
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
        return await self.handle_selection(token, _context_from_route(data), index)

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
                    await self.handle_selection(data["token"], _context_from_route(data), int(index))
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

    async def handle_job(self, job: dict[str, Any], *, job_id: str):
        if not isinstance(job_id, str) or not job_id or len(job_id) > 256:
            raise ValueError("invalid job id")
        payload = dict(job)
        raw = payload.get("candidate")
        if raw is None:
            # Legacy payload: freeze one route snapshot now and never look the route up again.
            route = await get_selection_for_request(self.redis, str(payload["request_id"]), namespace=self.namespace)
            if not route: raise SelectionRejected()
            raw = (route.get("candidates") or {}).get(str(payload.get("index")), {})
            payload = {**payload, "from_user": route["from_user"], "query": route.get("query", ""),
                       "corp_id": payload.get("corp_id") or route.get("corp_id", ""),
                       "generation": payload.get("generation") or route.get("generation", 0)}
        if isinstance(raw, str):
            raw = json.loads(raw)
        candidate = raw if isinstance(raw, Candidate) else Candidate.model_validate(raw)
        if self.refresh is None: raise RuntimeError("refresh callback is required")
        deadline = self._deadline()
        owner = f"{self.consumer}:{secrets.token_hex(16)}"
        async with asyncio.timeout_at(deadline):
            result = await self._run_download_effect(job_id, payload, candidate, owner, deadline)
            user = str(payload.get("from_user") or payload.get("user") or "")
            if result.download is not None:
                await self._notify(job_id, "success_notice", owner, deadline, user,
                                   f"下载成功：{result.download.relative_path}")
                return result
            await self._finish_failure(job_id, payload, candidate, owner, deadline, result, user)
            return result

    def _clock(self) -> float:
        return asyncio.get_running_loop().time()

    def _deadline(self) -> float:
        """Start one handler deadline that covers every Redis, media, and WeCom step."""
        return self._clock() + self.job_timeout

    def _guard(self, job_id: str, effect: str, owner: str, deadline: float) -> _EffectGuard:
        return _EffectGuard(self.state, job_id, effect, owner, deadline=deadline, ttl=self.job_ttl,
                            overhead=self.redis_overhead_seconds, pending_idle_ms=self.pending_idle_ms,
                            clock=self._clock)

    def _lease_ms(self, deadline: float) -> int:
        return self._guard("", "download", "", deadline).lease_ms()

    async def _reserve(self, job_id: str, candidate: Candidate, owner: str, lease: EffectLease,
                       deadline: float) -> ArtifactRecord | None:
        """Persist the deterministic artifact reservation before any source call.

        A candidate without a usable extension has no deterministic target, so it keeps the
        unreserved download path instead of inventing a reservation.
        """
        root = Path(self.media_root).resolve(strict=False)
        extension = str(candidate.format or "").lstrip(".").lower()
        if not extension:
            return None
        try:
            base = validated_destination(root, None, candidate.artist, candidate.title, extension)
        except MediaError:
            return None
        base_relative = base.resolve(strict=False).relative_to(root).as_posix()
        record = await self.state.prepare_artifact(
            job_id, candidate, media_root=root, base_relative_path=base_relative, extension=extension,
            media_type=_MEDIA_TYPES.get(extension, f"audio/{extension}"), declared_size=None,
            owner=owner, fence=lease.fence, ttl=self.job_ttl)
        if record.fence > lease.fence:
            raise EffectUncertain("artifact_uncertain")
        if record.fence < lease.fence:
            action = await self.state.takeover_artifact(
                job_id, new_owner=owner, new_fence=lease.fence,
                now_ms=await self.state.redis_now_ms(), lease_ms=self._lease_ms(deadline))
            if action == "owner_conflict":
                raise JobDeferred()
            if action == "uncertain":
                raise EffectUncertain("artifact_uncertain")
            record = await self.state.get_artifact(job_id) or record
        return record

    async def _run_download_effect(self, job_id: str, payload: dict[str, Any], candidate: Candidate,
                                   owner: str, deadline: float):
        guard = self._guard(job_id, "download", owner, deadline)
        lease, record = await guard.claim()
        if lease is None:
            if record.status == "uncertain":
                raise EffectUncertain(_effect_code(record, "artifact_uncertain"))
            if not (record.result or {}).get("ok", False):
                return FallbackResult(download=None,
                                      download_error=_effect_code(record, "download_failed"))
            return await self._replay_download(job_id, payload, candidate, owner, deadline)
        reservation = await self._reserve(job_id, candidate, owner, lease, deadline)
        reserved = {} if reservation is None else {
            "reservation": reservation, "artifact_store": self.state, "owner": owner, "fence": lease.fence}
        await guard.external(lease)
        try:
            result = await download_with_fallback(
                candidate, self._guarded_sources(job_id, owner, deadline), self.media_root,
                request_id=str(payload["request_id"]), query=str(payload.get("query") or candidate.title),
                refresh=self._guarded_refresh(job_id, owner, deadline),
                resolve_stream_timeout=self.resolve_stream_timeout, refresh_timeout=self.refresh_timeout,
                health_timeout=self.health_timeout, **reserved)
        except asyncio.CancelledError:
            await guard.uncertain_quietly(lease, "download_cancelled")
            raise
        except (TimeoutError, MediaError) as exc:
            code = exc.code if isinstance(exc, MediaError) else "media_timeout"
            await guard.complete_quietly(lease, {"ok": False, "code": code})
            return FallbackResult(download=None, download_error=code)
        except JobDeferred:
            raise
        except EffectUncertain:
            raise
        except Exception:
            await guard.uncertain_quietly(lease, "download_uncertain")
            raise
        if result.download is None:
            code = result.download_error or "download_failed"
            await guard.complete_quietly(lease, {"ok": False, "code": code})
            if code == "artifact_uncertain":
                raise EffectUncertain(code)
            return result
        await guard.complete(lease, {"ok": True})
        return result

    async def _replay_download(self, job_id: str, payload: dict[str, Any], candidate: Candidate,
                               owner: str, deadline: float):
        """Replay a completed download from its durable artifact record without a source call."""
        record = await self.state.get_artifact(job_id)
        if record is None or record.state == "uncertain":
            raise EffectUncertain("artifact_uncertain")
        result = await download_with_fallback(
            candidate, self._guarded_sources(job_id, owner, deadline), self.media_root,
            request_id=str(payload["request_id"]), query=str(payload.get("query") or candidate.title),
            refresh=self._guarded_refresh(job_id, owner, deadline),
            resolve_stream_timeout=self.resolve_stream_timeout, refresh_timeout=self.refresh_timeout,
            health_timeout=self.health_timeout, reservation=record, artifact_store=self.state,
            owner=owner, fence=record.fence)
        if result.download is None:
            raise EffectUncertain(result.download_error or "artifact_uncertain")
        return result

    def _guarded_sources(self, job_id: str, owner: str, deadline: float) -> dict:
        """Wrap every source so the shared health call is a separate fenced effect."""
        return {source_id: _GuardedSource(source, self, job_id, owner, deadline)
                for source_id, source in self.sources.items()}

    def _guarded_refresh(self, job_id: str, owner: str, deadline: float):
        """Wrap the search refresh callback in its own fenced effect."""
        refresh = self.refresh

        async def call(query: str, excluded: frozenset[str]):
            guard = self._guard(job_id, "refresh", owner, deadline)
            lease, record = await guard.claim()
            if lease is None:
                # A completed refresh cannot republish its candidate snapshot, so it is terminal.
                raise EffectUncertain(_effect_code(record, "refresh_uncertain"))
            await guard.external(lease)
            try:
                value = await refresh(query, excluded)
            except asyncio.CancelledError:
                await guard.uncertain_quietly(lease, "refresh_uncertain")
                raise
            except Exception:
                await guard.complete_quietly(lease, {"ok": False, "code": "refresh_failed"})
                raise
            await guard.complete(lease, {"ok": True, "version": str(getattr(value, "version", ""))[:64],
                                         "candidates": len(getattr(value, "candidates", None) or [])})
            return value

        return call

    async def _finish_failure(self, job_id: str, payload: dict[str, Any], candidate: Candidate, owner: str,
                              deadline: float, result, user: str) -> None:
        """Rebind refreshed candidates under a new generation, or report the terminal failure."""
        refreshed = result.refreshed
        corp_id = str(payload.get("corp_id") or "")
        if refreshed is None or not user or not corp_id:
            await self._notify(job_id, "terminal_failure_notice", owner, deadline, user, self._failure_text(result))
            return
        token = await self._rebind(job_id, payload, refreshed, owner, deadline, user, corp_id)
        if token is None:
            return
        prompt = "\n\n回复序号下载。"
        text = format_results(refreshed.candidates, max_items=self.max_results,
                              max_bytes=2048 - len(prompt.encode("utf-8")))
        await self._notify(job_id, "selection_prompt", owner, deadline, user, (text or "没有找到匹配结果。") + prompt)

    async def _rebind(self, job_id: str, payload: dict[str, Any], refreshed, owner: str, deadline: float,
                      user: str, corp_id: str) -> str | None:
        """Persist one refreshed selection generation and return its token."""
        guard = self._guard(job_id, "rebind", owner, deadline)
        lease, record = await guard.claim()
        if lease is None:
            if record.status == "uncertain":
                await self._notify(job_id, "terminal_failure_notice", owner, deadline, user,
                                   TERMINAL_FAILURE_TEXT)
                return None
            token = (record.result or {}).get("token")
            return token if isinstance(token, str) and token else None
        context = SelectionContext(
            corp_id=corp_id, from_user=user, request_id=str(payload["request_id"]),
            candidate_set_version=str(getattr(refreshed, "version", "")),
            candidates={index: item for index, item in enumerate(refreshed.candidates[:EFFECT_REPLAY_LIMIT], 1)},
            query=str(payload.get("query") or ""),
            selection_generation=int(payload.get("generation") or 0) + 1)
        await guard.external(lease)
        token = await self.state.issue_selection(context, ttl=self.selection_ttl)
        await bind_user_selection(self.redis, token, context, ttl=self.selection_ttl, namespace=self.namespace)
        await guard.complete(lease, {"ok": True, "token": token,
                                     "generation": context.selection_generation})
        return token

    @staticmethod
    def _failure_text(result) -> str:
        return f"下载失败，已重试：{result.download_error or result.refresh_error or 'unknown'}"

    async def _notify(self, job_id: str, effect: str, owner: str, deadline: float, user: str, text: str) -> None:
        """Send one bounded WeCom notice under its own fence; never resend an uncertain send."""
        if not user:
            return
        guard = self._guard(job_id, effect, owner, deadline)
        lease, record = await guard.claim()
        if lease is None:
            return
        await guard.external(lease)
        budget = min(self.wecom_notice_timeout, max(0.0, deadline - self._clock()))
        try:
            async with asyncio.timeout(budget):
                await _call(self.wecom.send_text, user, text)
        except asyncio.CancelledError:
            await guard.uncertain_quietly(lease, _NOTICE_CODES.get(effect, f"{effect}_uncertain"))
            raise
        except Exception:
            await guard.uncertain_quietly(lease, _NOTICE_CODES.get(effect, f"{effect}_uncertain"))
            return
        await guard.complete(lease, {"delivered": True})

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
                    await self.handle_job(job, job_id=str(_field({"id": message_id}, "id", "")))
                except JobDeferred:
                    # A live lease owns this job: leave the message pending with no retry and no XACK.
                    continue
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

    async def _notify_terminal(self, stream: str, message_id: Any, user: str) -> None:
        """Claim the dead-letter notice as its own fenced effect before it is sent."""
        if not user:
            return
        deadline = self._deadline()
        owner = f"{self.consumer}:{secrets.token_hex(16)}"
        try:
            async with asyncio.timeout_at(deadline):
                await self._notify(str(_field({"id": message_id}, "id", "")), "terminal_failure_notice",
                                   owner, deadline, user, TERMINAL_FAILURE_TEXT)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


class _GuardedSource:
    """Source proxy that fences the shared health call behind its own durable effect."""

    def __init__(self, source: Any, worker: JobWorker, job_id: str, owner: str, deadline: float):
        self.source, self.worker = source, worker
        self.job_id, self.owner, self.deadline = job_id, owner, deadline

    async def download(self, candidate: Candidate):
        return await self.source.download(candidate)

    async def health(self):
        guard = self.worker._guard(self.job_id, "health", self.owner, self.deadline)
        lease, record = await guard.claim()
        if lease is None:
            if record.status == "uncertain":
                return None
            return bool((record.result or {}).get("healthy", True))
        await guard.external(lease)
        try:
            healthy = await self.source.health()
        except asyncio.CancelledError:
            await guard.uncertain_quietly(lease, "health_uncertain")
            raise
        except Exception:
            await guard.complete_quietly(lease, {"ok": False, "code": "health_failed"})
            raise
        await guard.complete(lease, {"ok": True, "healthy": bool(healthy)})
        return healthy
