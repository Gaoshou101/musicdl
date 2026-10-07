"""Bounded, non-destructive source diagnostics for the administrator panel.

The check exercises a source's own search adapter and, when that source exposes
a metadata-only resolver, asks for FLAC and one lossy fallback. It never opens
the returned URL or downloads media. Results are kept in a separate small JSON
file so diagnostic writes cannot overwrite administrator credentials.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from musicdl.media.admission import AdmissionRejected, DownloadAdmission
from musicdl.sources.lossless_probe import PROBE_QUALITY, verdict_of
from musicdl.sources.models import Candidate


_LOG = logging.getLogger("musicdl.admin.diagnostics")
DEFAULT_DIAGNOSTIC_QUERY = "周杰伦 晴天"
FALLBACK_DIAGNOSTIC_QUERIES = ("晴天 周杰伦", "稻香 周杰伦")
DIAGNOSTIC_TIMEOUT_SECONDS = 45.0
DIAGNOSTIC_COOLDOWN_SECONDS = 60.0
DIAGNOSTIC_WORKERS = 2
DIAGNOSTIC_PENDING_LIMIT = 32
DIAGNOSTIC_RESULT_LIMIT = 256
DIAGNOSTIC_JOB_HISTORY_LIMIT = 128
DIAGNOSTIC_STORE_MAX_BYTES = 4 * 1024 * 1024
_STORE_VERSION = 1
_HEX_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")

_STATUS_MESSAGES = {
    "ok": "搜索和 FLAC 解析均有响应；下载尚未验证。",
    "unsupported_flac": "搜索和低音质解析有响应，但未能提供 FLAC；下载尚未验证。",
    "empty_search": "搜索没有返回结果；这不能说明音源不可用。",
    "search_failed": "搜索阶段失败。",
    "resolve_failed": "搜索有结果，但解析阶段失败。",
    "resolve_unknown": "搜索和解析有响应，但返回的元数据不足以确认音质。",
    "resolve_unavailable": "搜索有结果；此音源没有可安全执行的元数据解析接口。",
    "search_only": "搜索有结果；此音源未配置解析能力。",
    "timeout": "检查超过时间限制。",
    "auth_required": "上游要求重新授权。",
    "rate_limited": "上游正在限流，请稍后重试。",
    "busy": "下载和检查容量已满，请稍后重试。",
    "cancelled": "检查已取消。",
    "source_unavailable": "当前运行时没有这个音源。",
}
_FINAL_STATUSES = frozenset(_STATUS_MESSAGES)
_AUTH_CODES = frozenset({
    "auth_required", "authentication_required", "unauthorized", "unauthorised",
    "not_authenticated", "invalid_token", "token_expired",
})
_RATE_CODES = frozenset({
    "rate_limit", "rate_limited", "too_many_requests", "429",
})
_TIMEOUT_CODES = frozenset({"timeout", "timed_out", "runner_timeout", "search_timeout"})
_LOSSY_QUALITY = "320k"
_DIAGNOSTIC_USER = "musicdl-source-diagnostic"


def normalize_query(value: object) -> str:
    """Validate a caller query without reflecting exception details."""
    if value is None or value == "":
        return DEFAULT_DIAGNOSTIC_QUERY
    if not isinstance(value, str):
        raise ValueError("invalid_query")
    query = " ".join(value.split())
    if not query:
        return DEFAULT_DIAGNOSTIC_QUERY
    if len(query) > 500:
        raise ValueError("invalid_query")
    return query


def source_fingerprint(runtime: Any, source_id: str) -> str | None:
    """Return the installed script hash when the runtime exposes one."""
    resolvers = getattr(runtime, "resolvers", {})
    resolver = resolvers.get(source_id) if isinstance(resolvers, dict) else None
    stored = getattr(resolver, "stored", None)
    manifest = getattr(stored, "manifest", None)
    digest = getattr(manifest, "sha256", None)
    if isinstance(digest, str) and len(digest) == 64:
        return digest.lower()
    return None


def _safe_code(error: BaseException) -> str:
    """Read only an exact allowlisted code attribute."""
    value = getattr(error, "code", None)
    if not isinstance(value, str):
        return ""
    return value.strip().casefold().replace("-", "_")[:64]


def classify_exception(error: BaseException, *, stage: str) -> str:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if status_code in {401, 403}:
            return "auth_required"
        if status_code == 429:
            return "rate_limited"
    code = _safe_code(error)
    if code in _AUTH_CODES:
        return "auth_required"
    if code in _RATE_CODES:
        return "rate_limited"
    if code in _TIMEOUT_CODES or code.endswith("_timeout"):
        return "timeout"
    if not code:
        message = str(error).strip().casefold().replace("-", "_")
        if message in _AUTH_CODES:
            return "auth_required"
        if message in _RATE_CODES:
            return "rate_limited"
        if message in _TIMEOUT_CODES or message.endswith("_timeout"):
            return "timeout"
    elif code in _AUTH_CODES or code in _RATE_CODES:
        return "search_failed" if stage == "search" else "resolve_failed"
    return "search_failed" if stage == "search" else "resolve_failed"


def _message(status: str) -> str:
    return _STATUS_MESSAGES.get(status, _STATUS_MESSAGES["resolve_failed"])


def _finite_timestamp(value: int | float) -> bool:
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _validated_record(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source_id = value.get("source_id")
    version = value.get("source_version")
    fingerprint = value.get("fingerprint")
    status = value.get("status")
    tested_at = value.get("tested_at")
    if (not isinstance(source_id, str) or not source_id or len(source_id) > 64
            or not isinstance(version, str) or not version or len(version) > 64
            or (fingerprint is not None and (not isinstance(fingerprint, str)
                                             or not _HEX_DIGEST.fullmatch(fingerprint)))
            or not isinstance(status, str) or status not in _FINAL_STATUSES
            or isinstance(tested_at, bool) or not isinstance(tested_at, (int, float))
            or not _finite_timestamp(tested_at)):
        return None
    query = value.get("query", DEFAULT_DIAGNOSTIC_QUERY)
    actual_query = value.get("actual_query", query)
    if not isinstance(query, str) or len(query) > 500:
        return None
    if not isinstance(actual_query, str) or len(actual_query) > 500:
        return None
    queries = value.get("queries_tried", [actual_query])
    if (not isinstance(queries, list) or len(queries) > 3
            or any(not isinstance(item, str) or len(item) > 500 for item in queries)):
        return None
    metrics: dict[str, int | None] = {}
    for key in ("search_ms", "resolve_ms"):
        number = value.get(key)
        if number is not None and (isinstance(number, bool) or not isinstance(number, int)
                                   or not 0 <= number <= 45_000):
            return None
        metrics[key] = number
    extension = value.get("extension")
    quality = value.get("quality")
    if extension is not None and (not isinstance(extension, str) or len(extension) > 16):
        return None
    if quality is not None and (not isinstance(quality, str) or len(quality) > 16):
        return None
    return {
        "source_id": source_id,
        "source_version": version,
        "fingerprint": fingerprint,
        "status": status,
        "message": _message(status),
        "query": query,
        "actual_query": actual_query,
        "queries_tried": list(queries),
        "tested_at": float(tested_at),
        "search_ms": metrics["search_ms"],
        "resolve_ms": metrics["resolve_ms"],
        "extension": extension,
        "quality": quality,
        "stage": (value.get("stage") if isinstance(value.get("stage"), str)
                   and value.get("stage") in {"queue", "admission", "search", "resolve"}
                   else "resolve"),
        "download_verified": False,
    }


class DiagnosticResultStore:
    """Atomic, bounded result snapshots separate from the admin credential file."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path else None
        self._results: OrderedDict[tuple[str, str, str | None], dict[str, Any]] = OrderedDict()
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            if self.path.stat().st_size > DIAGNOSTIC_STORE_MAX_BYTES:
                _LOG.warning("source diagnostic result file exceeds its size limit")
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LOG.warning("source diagnostic result file could not be read")
            return
        if not isinstance(payload, dict) or payload.get("version") != _STORE_VERSION:
            _LOG.warning("source diagnostic result file has an unsupported format")
            return
        records = payload.get("results")
        if not isinstance(records, list):
            return
        for item in records[-DIAGNOSTIC_RESULT_LIMIT:]:
            record = _validated_record(item)
            if record is not None:
                key = (record["source_id"], record["source_version"], record["fingerprint"])
                self._results[key] = record

    def record(self, result: dict[str, Any]) -> bool:
        validated = _validated_record(result)
        if validated is None:
            return False
        key = (validated["source_id"], validated["source_version"], validated["fingerprint"])
        self._results.pop(key, None)
        self._results[key] = validated
        while len(self._results) > DIAGNOSTIC_RESULT_LIMIT:
            self._results.popitem(last=False)
        return self._save()

    def latest(self, source_id: str, version: str, fingerprint: str | None) -> dict[str, Any] | None:
        result = self._results.get((source_id, version, fingerprint))
        return dict(result) if result else None

    def snapshot(self, current: dict[str, tuple[str, str | None]]) -> list[dict[str, Any]]:
        rows = []
        for result in self._results.values():
            active = current.get(result["source_id"])
            row = dict(result)
            row["stale"] = active != (result["source_version"], result["fingerprint"])
            rows.append(row)
        return rows

    def _save(self) -> bool:
        if self.path is None:
            return True
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"version": _STORE_VERSION,
                                         "results": list(self._results.values())},
                                        ensure_ascii=False, sort_keys=True))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            return True
        except OSError:
            _LOG.warning("source diagnostic result file could not be saved")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False


@dataclass
class _Job:
    job_id: str
    source_id: str
    source_version: str
    fingerprint: str | None
    query: str
    user_id: str
    runtime: Any
    entry: Any
    submitted_at: float
    deadline: float
    status: str = "queued"
    stage: str = "queue"
    result: dict[str, Any] | None = None
    task: asyncio.Task | None = None
    batch_id: str | None = None

    @property
    def key(self) -> tuple[str, str, str | None]:
        return self.source_id, self.source_version, self.fingerprint


@asynccontextmanager
async def _runtime_borrow(runtime):
    borrow = getattr(runtime, "borrow", None)
    if callable(borrow):
        async with borrow():
            yield runtime
    else:
        yield runtime


async def _cancel_and_drain(tasks: tuple[asyncio.Task, ...] | list[asyncio.Task]) -> None:
    """Drain cancelled work to completion before allowing runtime cleanup."""
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    joined = asyncio.gather(*pending, return_exceptions=True)
    interrupted = False
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            interrupted = True
    await joined
    if interrupted:
        raise asyncio.CancelledError


class SourceDiagnosticManager:
    """Own bounded diagnostic jobs across runtime reloads."""

    def __init__(self, store: DiagnosticResultStore, admission: DownloadAdmission, *,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 cooldown_seconds: float = DIAGNOSTIC_COOLDOWN_SECONDS,
                 deadline_seconds: float = DIAGNOSTIC_TIMEOUT_SECONDS):
        self.store = store
        self.admission = admission
        self.admission_user = _DIAGNOSTIC_USER
        self.clock = clock
        self.monotonic = monotonic
        self.cooldown_seconds = cooldown_seconds
        self.deadline_seconds = deadline_seconds
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=DIAGNOSTIC_PENDING_LIMIT)
        self._lock = asyncio.Lock()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._by_key: dict[tuple[str, str, str | None], str] = {}
        self._cooldown: dict[tuple[str, str, str | None], float] = {}
        self._workers: list[asyncio.Task] = []
        self._closed = False
        self._retired_runtimes: set[int] = set()

    @staticmethod
    def _entry(runtime, source_id: str):
        registry = getattr(runtime, "plugin_registry", None) or getattr(runtime, "registry", None)
        return registry.get(source_id) if registry is not None else None

    def _start_workers(self) -> None:
        if self._workers or self._closed:
            return
        self._workers = [asyncio.create_task(self._worker(), name=f"source-diagnostics-{index}")
                         for index in range(DIAGNOSTIC_WORKERS)]

    def _prune_job_history(self) -> None:
        while len(self._jobs) >= DIAGNOSTIC_JOB_HISTORY_LIMIT:
            removable = next((job_id for job_id, job in self._jobs.items()
                              if job.status in {"complete", "cancelled", "not_started"}), None)
            if removable is None:
                return
            self._jobs.pop(removable, None)

    def _cooldown_result(self, source_id: str, version: str,
                         fingerprint: str | None) -> dict[str, Any] | None:
        result = self.store.latest(source_id, version, fingerprint)
        completed_at = self._cooldown.get((source_id, version, fingerprint))
        if result is None or result["status"] in {"busy", "cancelled"}:
            return None
        if completed_at is None:
            # Monotonic time has a process-local origin. Restore the remaining
            # cooldown from the persisted completion timestamp after restart.
            elapsed = max(0.0, self.clock() - result["tested_at"])
        else:
            elapsed = max(0.0, self.monotonic() - completed_at)
        remaining = self.cooldown_seconds - elapsed
        if remaining <= 0:
            return None
        return {"result": result, "retry_after": max(1, math.ceil(remaining)),
                "actual_query": result["actual_query"]}

    async def submit(self, runtime: Any, source_id: str, *, user_id: str,
                     query: object = None, batch_id: str | None = None) -> dict[str, Any]:
        cleaned_query = normalize_query(query)
        entry = self._entry(runtime, source_id)
        if entry is None:
            return {"mode": "not_started", "source_id": source_id,
                    "status": "source_unavailable", "job_id": None}
        version = getattr(entry, "version", None)
        if not isinstance(version, str) or not version:
            return {"mode": "not_started", "source_id": source_id,
                    "status": "source_unavailable", "job_id": None}
        fingerprint = source_fingerprint(runtime, source_id)
        key = (source_id, version, fingerprint)
        async with self._lock:
            if self._closed or id(runtime) in self._retired_runtimes:
                return {"mode": "not_started", "source_id": source_id,
                        "status": "runtime_reloading" if not self._closed else "busy",
                        "job_id": None}
            active_id = self._by_key.get(key)
            if active_id is not None:
                active = self._jobs.get(active_id)
                if active is not None and active.status in {"queued", "running"}:
                    return {"mode": "deduplicated", "source_id": source_id,
                            "source_version": version, "job_id": active_id,
                            "status": active.status, "actual_query": active.query}
                self._by_key.pop(key, None)
            cooldown = self._cooldown_result(source_id, version, fingerprint)
            if cooldown is not None:
                return {"mode": "cooldown", "source_id": source_id,
                        "source_version": version, "job_id": None,
                        "status": cooldown["result"]["status"], **cooldown}
            if self._queue.full():
                return {"mode": "not_started", "source_id": source_id,
                        "source_version": version, "job_id": None, "status": "queue_full"}
            if len(self._jobs) >= DIAGNOSTIC_JOB_HISTORY_LIMIT and all(
                    job.status in {"queued", "running"} for job in self._jobs.values()):
                return {"mode": "not_started", "source_id": source_id,
                        "source_version": version, "job_id": None, "status": "queue_full"}

            job = _Job(secrets.token_urlsafe(18), source_id, version, fingerprint,
                       cleaned_query, user_id, runtime, entry, self.clock(),
                       self.monotonic() + self.deadline_seconds,
                       batch_id=batch_id)
            self._prune_job_history()
            self._jobs[job.job_id] = job
            self._by_key[key] = job.job_id
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                self._jobs.pop(job.job_id, None)
                self._by_key.pop(key, None)
                return {"mode": "not_started", "source_id": source_id,
                        "source_version": version, "job_id": None, "status": "queue_full"}
            self._start_workers()
            return {"mode": "scheduled", "source_id": source_id,
                    "source_version": version, "job_id": job.job_id,
                    "status": "queued", "actual_query": cleaned_query}

    async def submit_many(self, runtime: Any, entries: Iterable[Any], *, user_id: str,
                          query: object = None) -> dict[str, Any]:
        cleaned_query = normalize_query(query)
        batch_id = secrets.token_urlsafe(12)
        items = []
        for entry in entries:
            source_id = getattr(entry, "source_id", None)
            if not isinstance(source_id, str) or not source_id:
                continue
            item = await self.submit(runtime, source_id, user_id=user_id,
                                     query=cleaned_query, batch_id=batch_id)
            items.append(item)
        for item in items:
            if item["mode"] == "scheduled":
                job = self._jobs.get(item["job_id"])
                if job is not None:
                    item["batch_id"] = batch_id
            elif item["mode"] == "deduplicated":
                job = self._jobs.get(item.get("job_id"))
                if job is not None:
                    job.batch_id = batch_id
                    item["batch_id"] = batch_id
        return {
            "batch_id": batch_id,
            "requested": len(items),
            "scheduled": sum(item["mode"] == "scheduled" for item in items),
            "deduplicated": sum(item["mode"] == "deduplicated" for item in items),
            "cooldown": sum(item["mode"] == "cooldown" for item in items),
            "not_started": sum(item["mode"] == "not_started" for item in items),
            "items": items,
        }

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.status != "queued":
                    continue
                if job.deadline <= self.monotonic():
                    job.status = "complete"
                    job.stage = "queue"
                    job.result = self._result(job, "timeout", stage="queue")
                    self.store.record(job.result)
                    self._cooldown[job.key] = self.monotonic()
                    if self._by_key.get(job.key) == job.job_id:
                        self._by_key.pop(job.key, None)
                    continue
                job.status = "running"
                job.stage = "admission"
                job.task = asyncio.create_task(self._run(job), name=f"diagnostic-{job.job_id}")
                try:
                    await job.task
                except asyncio.CancelledError:
                    if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                        raise
            finally:
                self._queue.task_done()

    async def _run(self, job: _Job) -> None:
        result = None
        try:
            async with _runtime_borrow(job.runtime):
                remaining = job.deadline - self.monotonic()
                if remaining <= 0:
                    result = self._result(job, "timeout", stage="queue")
                else:
                    try:
                        async with asyncio.timeout(remaining):
                            async with self.admission.acquire(source_id=job.source_id,
                                                              user_id=self.admission_user,
                                                              timeout=0):
                                result = await self._diagnose(job)
                    except AdmissionRejected:
                        result = self._result(job, "busy", stage="admission")
                    except TimeoutError:
                        result = self._result(job, "timeout", stage=job.stage)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.result = self._result(job, "cancelled", stage=job.stage)
            raise
        except Exception:
            _LOG.warning("source diagnostic failed at stage %s", job.stage)
            result = self._result(job, "search_failed" if job.stage == "search" else "resolve_failed",
                                  stage=job.stage)
        finally:
            if result is not None:
                job.result = result
                if result["status"] != "cancelled":
                    job.status = "complete"
                    self.store.record(result)
                    if result["status"] != "busy":
                        self._cooldown[job.key] = self.monotonic()
            async with self._lock:
                if self._by_key.get(job.key) == job.job_id:
                    self._by_key.pop(job.key, None)

    async def _diagnose(self, job: _Job) -> dict[str, Any]:
        source = getattr(job.entry, "source", None)
        search = source if callable(source) and not callable(getattr(source, "search", None)) else getattr(source, "search", None)
        if not callable(search):
            return self._result(job, "search_failed", stage="search")
        queries = ([job.query] + [fallback for fallback in FALLBACK_DIAGNOSTIC_QUERIES
                                  if fallback != job.query][:2])
        queries_tried: list[str] = []
        search_ms = 0
        candidate = None
        actual_query = job.query
        last_search_status = "empty_search"
        for query in queries:
            if self.monotonic() >= job.deadline:
                result = self._result(job, "timeout", stage="search", query=actual_query,
                                      queries_tried=queries_tried, search_ms=search_ms)
                return result
            job.stage = "search"
            actual_query = query
            queries_tried.append(query)
            started = self.monotonic()
            try:
                found = search(query)
                if inspect.isawaitable(found):
                    found = await found
                first = next(iter(found or ()), None)
                if first is not None:
                    candidate = first if isinstance(first, Candidate) else Candidate.model_validate(first)
                    if (candidate.source_id != job.source_id
                            or candidate.source_version != job.source_version):
                        raise ValueError("candidate_identity")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                search_ms += max(0, round((self.monotonic() - started) * 1000))
                status = classify_exception(error, stage="search")
                if status in {"auth_required", "rate_limited", "timeout"}:
                    return self._result(job, status, stage="search", query=actual_query,
                                        queries_tried=queries_tried, search_ms=search_ms)
                if len(queries_tried) < len(queries):
                    last_search_status = status
                    continue
                return self._result(job, status, stage="search", query=actual_query,
                                    queries_tried=queries_tried, search_ms=search_ms)
            search_ms += max(0, round((self.monotonic() - started) * 1000))
            if candidate is not None:
                break
        if candidate is None:
            return self._result(job, "empty_search" if last_search_status == "empty_search" else last_search_status,
                                stage="search", query=actual_query,
                                queries_tried=queries_tried, search_ms=search_ms)

        resolver = getattr(job.runtime, "resolvers", {}).get(job.source_id)
        client = getattr(resolver, "client", None)
        stored = getattr(resolver, "stored", None)
        resolve = getattr(client, "resolve", None)
        if callable(resolve) and stored is not None:
            return await self._resolve(job, resolve, stored, candidate, actual_query,
                                       queries_tried, search_ms)
        # Some sources can only be validated by actually downloading bytes (for
        # example Telegram bots). A diagnostic must not silently do that work.
        status = "search_only" if resolver is None else "resolve_unavailable"
        return self._result(job, status, stage="resolve", query=actual_query,
                            queries_tried=queries_tried, search_ms=search_ms)

    async def _resolve(self, job: _Job, resolve, stored, candidate: Candidate,
                       query: str, queries_tried: list[str], search_ms: int) -> dict[str, Any]:
        resolve_ms = 0
        job.stage = "resolve"
        started = self.monotonic()
        try:
            async with asyncio.timeout(max(0.001, job.deadline - self.monotonic())):
                flac = await resolve(stored, candidate,
                                     timeout_ms=self._remaining_ms(job), quality=PROBE_QUALITY)
            resolve_ms += max(0, round((self.monotonic() - started) * 1000))
            extension = getattr(flac, "extension", None)
            quality = getattr(flac, "actual_quality", None) or getattr(flac, "quality", None)
            verdict, _ = verdict_of(flac)
            if verdict == "lossless":
                return self._result(job, "ok", stage="resolve", query=query,
                                    queries_tried=queries_tried, search_ms=search_ms,
                                    resolve_ms=resolve_ms, extension=extension, quality=quality)
            # Keep an explicit lossy answer distinct from contradictory or
            # incomplete metadata, following the lossless probe's semantics.
            status = "unsupported_flac" if verdict == "lossy" else "resolve_unknown"
            return self._result(job, status, stage="resolve", query=query,
                                queries_tried=queries_tried, search_ms=search_ms,
                                resolve_ms=resolve_ms, extension=extension, quality=quality)
        except asyncio.CancelledError:
            raise
        except Exception as flac_error:
            resolve_ms += max(0, round((self.monotonic() - started) * 1000))
            status = classify_exception(flac_error, stage="resolve")
            if status in {"timeout", "auth_required", "rate_limited"}:
                return self._result(job, status, stage="resolve", query=query,
                                    queries_tried=queries_tried, search_ms=search_ms,
                                    resolve_ms=resolve_ms)

        # A source that rejects FLAC may still resolve a lossy track. Keep this
        # to one bounded fallback request and report the distinction explicitly.
        if self.monotonic() >= job.deadline:
            return self._result(job, "timeout", stage="resolve", query=query,
                                queries_tried=queries_tried, search_ms=search_ms,
                                resolve_ms=resolve_ms)
        started = self.monotonic()
        try:
            async with asyncio.timeout(max(0.001, job.deadline - self.monotonic())):
                fallback = await resolve(stored, candidate,
                                         timeout_ms=self._remaining_ms(job), quality=_LOSSY_QUALITY)
            resolve_ms += max(0, round((self.monotonic() - started) * 1000))
            return self._result(job, "unsupported_flac", stage="resolve", query=query,
                                queries_tried=queries_tried, search_ms=search_ms,
                                resolve_ms=resolve_ms,
                                extension=getattr(fallback, "extension", None),
                                quality=getattr(fallback, "actual_quality", None)
                                or getattr(fallback, "quality", None))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            resolve_ms += max(0, round((self.monotonic() - started) * 1000))
            status = classify_exception(error, stage="resolve")
            return self._result(job, status, stage="resolve", query=query,
                                queries_tried=queries_tried, search_ms=search_ms,
                                resolve_ms=resolve_ms)

    def _remaining_ms(self, job: _Job) -> int:
        remaining = max(0.001, job.deadline - self.monotonic())
        return max(1, min(30_000, math.ceil(remaining * 1000)))

    def _result(self, job: _Job, status: str, *, stage: str,
                query: str | None = None, queries_tried: list[str] | None = None,
                search_ms: int | None = None, resolve_ms: int | None = None,
                extension: Any = None, quality: Any = None) -> dict[str, Any]:
        result = {
            "source_id": job.source_id,
            "source_version": job.source_version,
            "fingerprint": job.fingerprint,
            "status": status if status in _FINAL_STATUSES else "resolve_failed",
            "stage": stage,
            "message": _message(status),
            "query": job.query,
            "actual_query": query or job.query,
            "queries_tried": list(queries_tried or []),
            "tested_at": self.clock(),
            "search_ms": search_ms,
            "resolve_ms": resolve_ms,
            "extension": extension if isinstance(extension, str) and len(extension) <= 16 else None,
            "quality": quality if isinstance(quality, str) and len(quality) <= 16 else None,
            "download_verified": False,
        }
        return result

    def snapshot(self, runtime: Any) -> dict[str, Any]:
        registry = getattr(runtime, "plugin_registry", None) or getattr(runtime, "registry", None)
        current: dict[str, tuple[str, str | None]] = {}
        if registry is not None:
            for entry in registry.enabled():
                current[entry.source_id] = (entry.version, source_fingerprint(runtime, entry.source_id))
        jobs = [self._public_job(job) for job in self._jobs.values()
                if job.status in {"queued", "running"}]
        return {"results": self.store.snapshot(current), "jobs": jobs,
                "active_limit": DIAGNOSTIC_WORKERS,
                "pending_limit": DIAGNOSTIC_PENDING_LIMIT,
                "active": sum(job.status == "running" for job in self._jobs.values()),
                "pending": self._queue.qsize()}

    @staticmethod
    def _public_job(job: _Job) -> dict[str, Any]:
        public = {"job_id": job.job_id, "source_id": job.source_id,
                "source_version": job.source_version, "fingerprint": job.fingerprint,
                "status": job.status, "stage": job.stage, "query": job.query,
                "submitted_at": job.submitted_at,
                "deadline_at": job.submitted_at + DIAGNOSTIC_TIMEOUT_SECONDS,
                "batch_id": job.batch_id}
        if job.result is not None:
            public["result"] = dict(job.result)
        return public

    def job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return self._public_job(job) if job is not None else None

    def owns_job(self, job_id: str, user_id: str) -> bool:
        job = self._jobs.get(job_id)
        return job is not None and secrets.compare_digest(job.user_id, user_id)

    async def _cancel_batch(self, jobs: list[_Job]) -> None:
        running = []
        async with self._lock:
            for job in jobs:
                if job.status == "queued":
                    job.status = "cancelled"
                    job.result = self._result(job, "cancelled", stage="queue")
                    self._discard_queued(job)
                    if self._by_key.get(job.key) == job.job_id:
                        self._by_key.pop(job.key, None)
                elif job.status == "running" and job.task is not None:
                    running.append(job.task)
        for task in running:
            task.cancel()
        if running:
            await _cancel_and_drain(running)
    async def cancel(self, job_id: str) -> dict[str, Any]:
        running_task = None
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"cancelled": False, "status": "not_found"}
            if job.status == "queued":
                job.status = "cancelled"
                job.result = self._result(job, "cancelled", stage="queue")
                self._discard_queued(job)
                self._by_key.pop(job.key, None)
            elif job.status == "running" and job.task is not None:
                running_task = job.task
            else:
                return {"cancelled": False, "status": job.status}
        if running_task is not None:
            running_task.cancel()
            await _cancel_and_drain((running_task,))
        return {"cancelled": True, "status": "cancelled"}

    async def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        jobs = [job for job in self._jobs.values() if job.batch_id == batch_id
                and job.status in {"queued", "running"}]
        await self._cancel_batch(jobs)
        return {"cancelled": len(jobs), "batch_id": batch_id}

    async def cancel_runtime(self, runtime: Any) -> None:
        running = []
        async with self._lock:
            self._retired_runtimes.add(id(runtime))
            for job in self._jobs.values():
                if job.runtime is not runtime:
                    continue
                if job.status == "queued":
                    job.status = "cancelled"
                    job.result = self._result(job, "cancelled", stage="queue")
                    self._discard_queued(job)
                    self._by_key.pop(job.key, None)
                elif job.status == "running" and job.task is not None:
                    running.append(job.task)
        for task in running:
            task.cancel()
        if running:
            await _cancel_and_drain(running)

    def release_runtime(self, runtime: Any) -> None:
        """Allow a runtime to be adopted again after a cancelled reload."""
        self._retired_runtimes.discard(id(runtime))

    async def close(self) -> None:
        running = []
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            for job in self._jobs.values():
                if job.status == "queued":
                    job.status = "cancelled"
                    job.result = self._result(job, "cancelled", stage="queue")
                    self._discard_queued(job)
                    self._by_key.pop(job.key, None)
                elif job.status == "running" and job.task is not None:
                    running.append(job.task)
        for task in running:
            task.cancel()
        interrupted = False
        if running:
            try:
                await _cancel_and_drain(running)
            except asyncio.CancelledError:
                interrupted = True
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            try:
                await _cancel_and_drain(self._workers)
            except asyncio.CancelledError:
                interrupted = True
        self._workers.clear()
        if interrupted:
            raise asyncio.CancelledError

    def _discard_queued(self, target: _Job) -> bool:
        """Remove a cancelled item now so it immediately returns queue capacity."""
        retained: list[_Job] = []
        removed = False
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued is target:
                removed = True
            else:
                retained.append(queued)
            self._queue.task_done()
        for queued in retained:
            self._queue.put_nowait(queued)
        return removed

