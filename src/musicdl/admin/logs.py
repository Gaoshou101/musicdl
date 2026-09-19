"""A bounded window onto this process's own log records.

The panel already shows the two things it did itself -- the download events and
the audit trail -- and neither of them answers "what did the service print". An
operator debugging a panel download needs the lines the code wrote while it was
failing, and those go through a log handler, not through a store the portal
owns. This is that store: one handler, attached to the loggers the running
process writes through, keeping the most recent records in memory.

It is deliberately not the container's stdout. What lands in Docker's log
driver arrives too late to read from a page, and it is not this process's to
serve. What is captured here is what this process logged while it ran, which is
the half an operator can act on from the panel.

Two things are stripped before anything is kept: credentials, which sources
embed in the URLs they are handed, and the query string of every HTTP URL,
because those URLs carry signed keys. This window is an egress path, so it uses
its own redaction rather than the key-based one in ``musicdl.secrets``, which
only knows about the fields a settings object happens to name.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime, timezone

_MASK = "[REDACTED_SECRET]"

# The names this handler is attached to directly. ``uvicorn`` and its children
# are configured with ``propagate = False``, so a handler on the root logger
# never sees them; ``uvicorn.access`` is configured that way on its own.
_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")

# The levels the panel may filter by, as the numeric thresholds they name.
LEVELS: dict[str, int] = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
                          "error": logging.ERROR, "critical": logging.CRITICAL}

_URL_TAIL = re.compile(r"(https?://[^\s\"'<>|\\]+?)[?#][^\s\"'<>|\\]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}")
_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|proxy-authorization|api[-_]?key|access[-_]?token|refresh[-_]?token"
    r"|key|token|secret|password|passwd|sign|signature)\b\s*[:=]\s*[^\s,;\"'&]+")

_FORMATTER = logging.Formatter()


def _redact(text: str) -> str:
    """Remove what a log line can leak: URL query strings and credentials.

    The URL rewrite runs first, because a signed query string is the commonest
    way a credential reaches a log line in the first place, and the assignment
    pattern would otherwise leave the parameter name behind with its value
    already gone.
    """
    text = _URL_TAIL.sub(lambda match: match.group(1), text)
    text = _BEARER.sub(lambda match: f"{match.group(0).split()[0]} {_MASK}", text)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={_MASK}", text)


class LogBuffer(logging.Handler):
    """The most recent log records of this process, bounded and redacted."""

    def __init__(self, capacity: int = 500) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 100_000:
            raise ValueError("invalid capacity")
        super().__init__(level=logging.INFO)
        self.capacity = capacity
        # (id, numeric level, entry) triples, so the filter and the cursor do
        # not have to re-parse a level name back into a number.
        self._entries: deque[tuple[int, int, dict]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 0
        self._dropped = 0
        self._attached: list[tuple[logging.Logger, bool]] = []

    # -- capture ---------------------------------------------------------
    def install(self) -> None:
        """Attach to the loggers this process writes through.

        Idempotent: a second call is a no-op, so a lifespan that runs twice
        does not record every line twice.
        """
        if self._attached:
            return
        root = logging.getLogger()
        root.addHandler(self)
        self._attached.append((root, root.propagate))
        for name in _LOGGER_NAMES:
            logger = logging.getLogger(name)
            # ``uvicorn.error`` propagates into ``uvicorn``, and both are
            # attached here, so the child is pinned where it is rather than
            # letting one record arrive twice.
            self._attached.append((logger, logger.propagate))
            logger.addHandler(self)
            logger.propagate = False

    def uninstall(self) -> None:
        """Detach exactly what ``install`` attached and restore propagation."""
        for logger, propagate in self._attached:
            logger.removeHandler(self)
            logger.propagate = propagate
        self._attached.clear()

    def append(self, record: logging.LogRecord) -> None:
        """Keep one record, dropping the oldest once the window is full."""
        entry = self._entry(record)
        levelno = record.levelno if isinstance(record.levelno, int) else 0
        with self._lock:
            self._next_id += 1
            if len(self._entries) >= self.capacity:
                self._dropped += 1
            self._entries.append((self._next_id, levelno, entry))

    def emit(self, record: logging.LogRecord) -> None:
        """The ``logging`` half of ``append``; it never raises at the caller."""
        try:
            self.append(record)
        except Exception:
            pass

    def _entry(self, record: logging.LogRecord) -> dict:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        traceback = None
        if record.exc_info:
            try:
                traceback = _FORMATTER.formatException(record.exc_info)
            except Exception:
                traceback = None
        return {"time": _timestamp(record), "level": str(record.levelname).lower(),
                "logger": str(record.name), "message": _redact(message),
                "traceback": None if traceback is None else _redact(traceback)}

    # -- reading ---------------------------------------------------------
    def page(self, *, limit: int = 200, after: int = 0, level: str | None = None) -> dict:
        """One page of retained records, oldest first.

        ``after`` is the cursor the page before this one ended on: ``0`` asks
        for the newest records, anything else for the first ones newer than it,
        which is what a window that polls without reprinting itself needs.
        ``dropped`` is cumulative -- how many records have rolled out of the
        window since the process started -- because a reader that is behind
        cannot tell from ids alone whether it missed any.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("invalid limit")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("invalid pagination")
        if level is not None and level not in LEVELS:
            raise ValueError("invalid level")
        threshold = 0 if level is None else LEVELS[level]
        with self._lock:
            retained = [(entry_id, entry) for entry_id, levelno, entry in self._entries
                        if levelno >= threshold]
            total = len(retained)
            if after > 0:
                page = [(entry_id, entry) for entry_id, entry in retained if entry_id > after][:limit]
            else:
                page = retained[-limit:]
            return {"items": [{"id": entry_id, **entry} for entry_id, entry in page],
                    "total": total, "last_id": page[-1][0] if page else after,
                    "dropped": self._dropped}


def _timestamp(record: logging.LogRecord) -> str:
    moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
