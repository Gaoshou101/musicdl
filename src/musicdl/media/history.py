"""A bounded download journal; Redis remains the authority for WeCom effects.

The context-local recorder follows the existing request/effect, never starts
work of its own, and cannot turn an already published file into a failed download.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath


_LOG = logging.getLogger("musicdl.history")
_ACTIVE: ContextVar[DownloadJournal | None] = ContextVar("download_history", default=None)
_SPANS: ContextVar[tuple] = ContextVar("download_history_spans", default=())
_EXTRA_CODES = frozenset({"download_cancelled", "service_restarted", "download_uncertain",
    "download_deferred", "source_unavailable", "refresh_failed", "health_failed",
    "refresh_included_failed_source", "cleanup_failed", "resolve_failed", "history_unavailable", "duration_unverified"})
_STAGES = frozenset({"download", "resolve", "transfer", "refresh", "health", "duration",
                     "cleanup", "channel_switch", "quality_downgraded", "task"})
_STATUSES = frozenset({"started", "success", "failed", "cancelled", "selected", "observed",
                       "unverified", "interrupted", "unavailable"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value, limit=256):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?i)https?://\S+", "[redacted]", value)
    value = re.sub(r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)=)\S+", "[redacted]", value)
    return "".join(c for c in value if c.isprintable())[:limit]


def _code(value):
    from .models import _DOWNLOAD_CODES
    return value if isinstance(value, str) and value in _DOWNLOAD_CODES | _EXTRA_CODES else "download_failed"


def _path(value):
    text = str(value).replace("\\", "/") if value is not None else ""
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or ":" in text or len(text) > 2048:
        return None
    return text


def download_report(request_id, source_id, fallback_from, result):
    """One shared, explicit artifact projection; no source URLs or local paths."""
    return {"request_id": request_id, "source_id": source_id, "fallback_from": fallback_from,
            "relative_path": _path(result.relative_path), "sha256": getattr(result, "sha256", None),
            "size_bytes": result.size_bytes, "media_type": result.media_type,
            "extension": result.extension, "language": getattr(result.language, "value", result.language)
            if hasattr(result, "language") else None,
            "requested_quality": getattr(result, "requested_quality", None),
            "actual_quality": getattr(result, "actual_quality", None)}


def _safe_result(result):
    if result is None:
        return None
    safe = {key: _text(result.get(key), 256) or None for key in
            ("request_id", "source_id", "fallback_from", "media_type", "extension", "language",
             "requested_quality", "actual_quality")}
    safe["relative_path"] = _path(result.get("relative_path"))
    digest = result.get("sha256")
    safe["sha256"] = digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None
    size = result.get("size_bytes")
    safe["size_bytes"] = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
    return safe


class HistoryUnavailable(RuntimeError):
    pass


class HistoryCapacity(HistoryUnavailable):
    pass


class DownloadHistoryStore:
    def __init__(self, path: str | Path | None = None, *, limit=1000, event_limit=200, running_limit=128):
        self.path = Path(path) if path is not None else None
        self.limit, self.event_limit, self.running_limit = limit, event_limit, running_limit
        self._connection = None
        self._lock = threading.RLock()
        self._warnings: set[str] = set()
        self._recovery_pending = False

    def _db(self):
        if self._connection is None:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path) if self.path else ":memory:", timeout=1,
                                         check_same_thread=False)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS download_events (
                    task_id TEXT NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(task_id, seq));
                CREATE INDEX IF NOT EXISTS download_order ON downloads(created_at DESC, id DESC);
            """)
            self._connection = connection
        return self._connection

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                connection = self._db()
                with connection:
                    recovering = self._recovery_pending
                    if recovering:
                        self._recover(connection)
                    yield connection
                if recovering:
                    self._recovery_pending = False
            except (sqlite3.Error, OSError) as exc:
                raise HistoryUnavailable("history_unavailable") from exc

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _prune(self, db):
        db.execute("DELETE FROM downloads WHERE id IN (SELECT id FROM downloads WHERE status != 'running' "
                   "ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?)", (self.limit,))
        self._warnings.intersection_update(row[0] for row in db.execute("SELECT id FROM downloads"))

    def recover(self):
        """Called once at application startup, never during runtime replacement."""
        self._recovery_pending = True
        # On a temporary startup I/O failure the next store operation retries
        # recovery before admitting work or presenting stale running records.
        with self._transaction():
            pass

    def _recover(self, db):
        now = _now()
        for task_id, body in db.execute("SELECT id, body FROM downloads WHERE status='running'").fetchall():
            data = json.loads(body)
            data.update(status="interrupted", updated_at=now, finished_at=now,
                        error_code="service_restarted", elapsed_ms=None)
            db.execute("UPDATE downloads SET status=?,body=? WHERE id=?",
                       (data["status"], json.dumps(data, ensure_ascii=False), task_id))
        self._prune(db)

    def begin(self, task_id, candidate, *, origin, query, quality=None):
        with self._transaction() as db:
            row = db.execute("SELECT body FROM downloads WHERE id=?", (task_id,)).fetchone()
            existing = json.loads(row[0]) if row else None
            # A duplicate Redis delivery cannot interrupt an owner or rewrite a
            # completed download. Artifact validation/replay remains in Redis.
            if existing and existing["status"] in {"running", "succeeded"}:
                return False
            count = db.execute("SELECT count(*) FROM downloads WHERE status='running'").fetchone()[0]
            if count >= self.running_limit:
                raise HistoryCapacity("history_capacity")
            now = _now()
            data = {"id": task_id, "origin": origin, "title": _text(candidate.title),
                    "artist": _text(candidate.artist), "query": _text(query, 500),
                    "source_id": _text(candidate.source_id), "status": "running",
                    "created_at": existing["created_at"] if existing else now,
                    "updated_at": now, "finished_at": None, "elapsed_ms": None,
                    "error_code": None, "requested_quality": _text(quality, 64) or None,
                    "actual_quality": None, "result": None,
                    "events_truncated": existing.get("events_truncated", False) if existing else False,
                    "history_warning": None}
            db.execute("INSERT INTO downloads VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "status=excluded.status,body=excluded.body", (task_id, data["created_at"],
                       "running", json.dumps(data, ensure_ascii=False)))
            self._prune(db)
            return True

    def append(self, task_id, event):
        data = asdict(event) if is_dataclass(event) else dict(event)
        stage = data.get("stage")
        if stage not in _STAGES:
            return
        safe = {"stage": stage, "status": data.get("status") if data.get("status") in _STATUSES else "observed",
                "created_at": _now(), "elapsed_ms": None}
        elapsed = data.get("elapsed_ms")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0:
            safe["elapsed_ms"] = round(elapsed, 3)
        for key in ("source_id", "source_version", "candidate_id", "requested_quality", "actual_quality",
                    "from_source_id", "to_source_id"):
            safe[key] = _text(data.get(key), 256) or None
        safe["error_code"] = _code(data["error_code"]) if data.get("error_code") else None
        reason = data.get("reason")
        safe["reason"] = (reason if reason in {"quality_downgrade", "content_failure"} else
                          reason.split(":", 1)[0] + ":" + _code(reason.split(":", 1)[1])
                          if isinstance(reason, str) and reason.startswith(("download_error:", "content_error:")) else None)
        safe["relative_path"] = _path(data.get("relative_path"))
        size = data.get("size_bytes")
        safe["size_bytes"] = size if isinstance(size, int) and size >= 0 else None
        with self._transaction() as db:
            row = db.execute("SELECT body FROM downloads WHERE id=?", (task_id,)).fetchone()
            if not row:
                return
            task = json.loads(row[0])
            seq = db.execute("SELECT coalesce(max(seq),0)+1 FROM download_events WHERE task_id=?", (task_id,)).fetchone()[0]
            safe["seq"] = seq
            db.execute("INSERT INTO download_events VALUES(?,?,?)", (task_id, seq, json.dumps(safe, ensure_ascii=False)))
            db.execute("DELETE FROM download_events WHERE task_id=? AND seq<=?", (task_id, seq-self.event_limit))
            task.update(updated_at=safe["created_at"], events_truncated=task["events_truncated"] or seq > self.event_limit)
            db.execute("UPDATE downloads SET body=? WHERE id=?", (json.dumps(task, ensure_ascii=False), task_id))

    def finish(self, task_id, *, status, elapsed_ms, result=None, error_code=None, warning=False):
        with self._transaction() as db:
            row = db.execute("SELECT body FROM downloads WHERE id=?", (task_id,)).fetchone()
            if not row:
                return
            data = json.loads(row[0])
            now = _now()
            result = _safe_result(result)
            data.update(status=status, updated_at=now, finished_at=now, elapsed_ms=round(elapsed_ms, 3),
                        result=result, error_code=_code(error_code) if error_code else None,
                        history_warning="history_unavailable" if warning else None)
            if result:
                data.update(source_id=result["source_id"], requested_quality=result["requested_quality"],
                            actual_quality=result["actual_quality"])
            db.execute("UPDATE downloads SET status=?,body=? WHERE id=?", (status, json.dumps(data, ensure_ascii=False), task_id))
            self._prune(db)

    def warn(self, task_id):
        if len(self._warnings) < self.limit + self.running_limit:
            self._warnings.add(task_id)
        _LOG.warning("download history write failed: history_unavailable")

    def _read(self, body):
        data = json.loads(body)
        if data["id"] in self._warnings:
            data["history_warning"] = "history_unavailable"
        return data

    def list(self, *, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid history pagination")
        with self._transaction() as db:
            rows = db.execute("SELECT body FROM downloads ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = db.execute("SELECT count(*) FROM downloads").fetchone()[0]
            return {"items": [self._read(row[0]) for row in rows], "total": total, "offset": offset, "limit": limit}

    def get(self, task_id):
        with self._transaction() as db:
            row = db.execute("SELECT body FROM downloads WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            data = self._read(row[0])
            data["events"] = [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM download_events WHERE task_id=? ORDER BY seq", (task_id,))]
            return data


class DownloadJournal:
    def __init__(self, store, task_id, candidate, *, origin, query, quality=None):
        self.store, self.task_id = store, task_id
        self.started = time.monotonic()
        self.warning = False
        self.enabled = store.begin(task_id, candidate, origin=origin, query=query, quality=quality)

    @contextmanager
    def activate(self):
        token = _ACTIVE.set(self if self.enabled else None)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    def record(self, event):
        if not self.enabled:
            return
        try:
            self.store.append(self.task_id, event)
        except HistoryUnavailable:
            self.warning = True
            self.store.warn(self.task_id)

    def finish(self, status, *, result=None, error_code=None):
        if self.enabled:
            try:
                self.store.finish(self.task_id, status=status, result=result, error_code=error_code,
                                  elapsed_ms=(time.monotonic()-self.started)*1000, warning=self.warning)
            except HistoryUnavailable:
                self.warning = True
                self.store.warn(self.task_id)
        return self.warning


def record_history_event(event):
    journal = _ACTIVE.get()
    if journal is not None:
        journal.record(event)


def trace_download_stage(stage):
    """Measure real awaited spans without changing the legacy event stream."""
    def decorate(function):
        @wraps(function)
        async def traced(*args, **kwargs):
            journal = _ACTIVE.get()
            if journal is None:
                return await function(*args, **kwargs)
            position = 1 if stage == "resolve" else 0
            candidate = args[position] if len(args) > position else kwargs["candidate"]
            key = (stage, candidate.source_id, candidate.item_id)
            spans = _SPANS.get()
            # A guarded WeCom resolver calls source_download again. It is one
            # resolver attempt, not two independent channel attempts.
            if key in spans:
                return await function(*args, **kwargs)
            fields = {"stage": stage, "source_id": candidate.source_id, "candidate_id": candidate.item_id,
                      "source_version": candidate.source_version, "requested_quality": kwargs.get("quality")}
            journal.record({**fields, "status": "started"})
            start = time.monotonic()
            token = _SPANS.set((*spans, key))
            try:
                result = await function(*args, **kwargs)
            except BaseException as exc:
                import asyncio
                journal.record({**fields, "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                                "error_code": "download_cancelled" if isinstance(exc, asyncio.CancelledError)
                                else "media_timeout" if isinstance(exc, TimeoutError)
                                else _code(getattr(exc, "code", None)), "elapsed_ms": (time.monotonic()-start)*1000})
                raise
            finally:
                _SPANS.reset(token)
            journal.record({**fields, "status": "success", "elapsed_ms": (time.monotonic()-start)*1000})
            return result
        return traced
    return decorate
