"""Bounded durable metadata for browser sessions.

The browser token is deliberately never part of a snapshot.  It is random and
high entropy, so its SHA-256 digest is sufficient for lookup without keeping a
replayable credential in the administrator state file.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
from dataclasses import dataclass
from typing import Callable


SESSION_TTL_SECONDS = 12 * 60 * 60
REMEMBERED_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_SESSIONS = 2048
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class SessionCapacityError(RuntimeError):
    """The session table is full after expired records were removed."""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class SessionRecord:
    token_hash: str
    csrf_hash: str
    expires_at: float
    credential_version: int
    user_id: str
    must_change: bool
    remembered: bool

    def snapshot(self) -> dict:
        return {
            "token_hash": self.token_hash,
            "csrf_hash": self.csrf_hash,
            "expires_at": self.expires_at,
            "credential_version": self.credential_version,
            "user_id": self.user_id,
            "must_change": self.must_change,
            "remembered": self.remembered,
        }


class SessionRegistry:
    """An expiry-checked, capacity-bounded session index."""

    def __init__(self, *, capacity: int = MAX_SESSIONS,
                 clock: Callable[[], float] = time.time):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("invalid session capacity")
        self.capacity = capacity
        self.clock = clock
        self._records: dict[str, SessionRecord] = {}

    def _purge_expired(self, now: float) -> bool:
        expired = [key for key, record in self._records.items() if record.expires_at <= now]
        for key in expired:
            self._records.pop(key, None)
        return bool(expired)

    def create(self, token: str, csrf_hash: str, *, user_id: str, must_change: bool,
               credential_version: int, expires_at: float,
               remembered: bool) -> SessionRecord:
        now = self.clock()
        self._purge_expired(now)
        if len(self._records) >= self.capacity:
            raise SessionCapacityError("administrator session capacity reached")
        token_hash = token_digest(token)
        record = SessionRecord(token_hash, csrf_hash, expires_at, credential_version,
                               user_id, must_change, remembered)
        self._records[token_hash] = record
        return record

    def get(self, token: str | None, *, credential_version: int,
            user_id: str, must_change: bool) -> SessionRecord | None:
        if not isinstance(token, str) or not token:
            return None
        now = self.clock()
        self._purge_expired(now)
        record = self._records.get(token_digest(token))
        if record is None:
            return None
        if (record.expires_at <= now or record.credential_version != credential_version
                or record.user_id != user_id or record.must_change != must_change):
            self._records.pop(record.token_hash, None)
            return None
        return record

    def revoke(self, token: str | None) -> SessionRecord | None:
        if not isinstance(token, str) or not token:
            return None
        return self._records.pop(token_digest(token), None)

    def restore(self, payload, *, credential_version: int, user_id: str,
                credential_must_change: bool) -> None:
        self._records.clear()
        if not isinstance(payload, list):
            return
        now = self.clock()
        # A persisted file is local state, but still bound its work and reject
        # timestamps that could keep a corrupted session alive indefinitely.
        for item in payload[-self.capacity:]:
            if not isinstance(item, dict):
                continue
            token_hash = item.get("token_hash")
            csrf_hash = item.get("csrf_hash")
            expires_at = item.get("expires_at")
            version = item.get("credential_version")
            saved_user = item.get("user_id")
            must_change = item.get("must_change")
            remembered = item.get("remembered")
            if (not isinstance(token_hash, str) or not _DIGEST_RE.fullmatch(token_hash)
                    or not isinstance(csrf_hash, str) or not _DIGEST_RE.fullmatch(csrf_hash)
                    or isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                    or isinstance(version, bool) or not isinstance(version, int)
                    or version != credential_version or saved_user != user_id
                    or not isinstance(must_change, bool)
                    or must_change is not credential_must_change or not isinstance(remembered, bool)):
                continue
            try:
                expires_at = float(expires_at)
            except (OverflowError, TypeError, ValueError):
                continue
            ttl = REMEMBERED_SESSION_TTL_SECONDS if remembered else SESSION_TTL_SECONDS
            if not math.isfinite(expires_at) or expires_at <= now or expires_at > now + ttl + 300:
                continue
            self._records[token_hash] = SessionRecord(token_hash, csrf_hash, float(expires_at),
                                                      version, saved_user, must_change, remembered)

    def snapshot(self) -> list[dict]:
        self._purge_expired(self.clock())
        return [record.snapshot() for record in self._records.values()]

    def clear(self) -> None:
        self._records.clear()

    def restore_records(self, records: dict[str, SessionRecord]) -> None:
        """Restore a prior in-memory snapshot after an atomic save fails."""
        self._records = records

    def copy_records(self) -> dict[str, SessionRecord]:
        return {key: SessionRecord(**record.__dict__) for key, record in self._records.items()}

    @staticmethod
    def csrf_matches(expected_hash: str, candidate: str | None) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        return hmac.compare_digest(expected_hash, token_digest(candidate))
