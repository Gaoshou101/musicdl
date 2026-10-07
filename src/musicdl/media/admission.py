"""Bounded, process-owned admission for media downloads.

Create one :class:`DownloadAdmission` for the service process and inject it into
every panel and worker runtime. The queue has no background task: cancellation
removes a pending waiter, and a lease is returned only after the caller owns an
active slot. The caller holds the lease across resolution, streaming, fallback,
and publication.
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any


class AdmissionRejected(RuntimeError):
    """No bounded admission slot could be obtained for this request."""

    def __init__(self, message: str, snapshot: dict[str, int]):
        super().__init__(message)
        self.snapshot = snapshot


class AdmissionQueueFull(AdmissionRejected):
    """The active slots and all bounded pending places are occupied."""


class AdmissionTimeout(AdmissionRejected):
    """The caller's bounded wait expired before a slot became available."""


ADMISSION_BLOCKED_CODE = "download_admission_blocked"
ADMISSION_TIMEOUT_CODE = "download_admission_timeout"
ADMISSION_FAILURE_CODES = frozenset({ADMISSION_BLOCKED_CODE, ADMISSION_TIMEOUT_CODE})


@dataclass(eq=False, slots=True)
class _Waiter:
    future: asyncio.Future["AdmissionPermit"]
    source_id: str | None
    user_id: str | None
    operation_deadline: float | None = None
    transition_timeout: float | None = None
    permit: "AdmissionPermit | None" = None


@dataclass(eq=False, slots=True)
class _SourceWaiter:
    future: asyncio.Future[None]
    permit: "AdmissionPermit"
    source_id: str


class AdmissionPermit:
    """An owned active slot; releasing it is safe to repeat."""

    __slots__ = ("_owner", "source_id", "user_id", "_released", "_operation_deadline",
                 "_transition_timeout", "_source_wait_interrupted")

    def __init__(self, owner: "DownloadAdmission", source_id: str | None, user_id: str | None,
                 operation_deadline: float | None = None, transition_timeout: float | None = None):
        self._owner = owner
        self.source_id = source_id
        self.user_id = user_id
        self._released = False
        self._operation_deadline = operation_deadline
        self._transition_timeout = transition_timeout
        self._source_wait_interrupted = False

    async def release(self) -> None:
        await self._owner._release(self)

    async def switch_source(self, source_id: str) -> None:
        """Reserve the actual resolver's source slot, retaining global/user slots."""
        source = _identifier(source_id, "source_id")
        if source is None:
            raise ValueError("invalid source_id")
        await self._owner._switch_source(self, source)

    def snapshot(self) -> dict[str, int]:
        """Return aggregate admission counts without exposing identifiers."""
        return self._owner.snapshot()

    def take_source_wait_interruption(self) -> AdmissionTimeout | None:
        """Whether a surrounding source-stage timeout interrupted a transition wait."""
        interrupted = self._source_wait_interrupted
        self._source_wait_interrupted = False
        if not interrupted:
            return None
        return AdmissionTimeout("download source transition timed out", self._owner.snapshot())

    def take_source_wait_interruption_status(self) -> str | None:
        """Whether interruption was the shared deadline or an enclosing stage budget."""
        timed_out = self._source_wait_interrupted
        self._source_wait_interrupted = False
        if not timed_out:
            return None
        deadline = self._operation_deadline
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            return "global_deadline"
        return "admission_wait"

    async def __aenter__(self) -> "AdmissionPermit":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.release()


class _AcquireContext:
    __slots__ = ("_owner", "_source_id", "_user_id", "_timeout",
                 "_operation_deadline", "_transition_timeout", "_permit")

    def __init__(self, owner: "DownloadAdmission", source_id: str | None,
                 user_id: str | None, timeout: float | None, operation_deadline: float | None,
                 transition_timeout: float | None):
        self._owner, self._source_id, self._user_id = owner, source_id, user_id
        self._timeout, self._operation_deadline = timeout, operation_deadline
        self._transition_timeout, self._permit = transition_timeout, None

    async def __aenter__(self) -> AdmissionPermit:
        self._permit = await self._owner._acquire(
            source_id=self._source_id, user_id=self._user_id, timeout=self._timeout,
            operation_deadline=self._operation_deadline, transition_timeout=self._transition_timeout,
        )
        return self._permit

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._permit is not None:
            await self._permit.release()
            self._permit = None


class DownloadAdmission:
    """A bounded active and waiting budget shared by all download entry points.

    The instance is event-loop-owned. It is intended to be constructed once per
    service process and reused across panel and worker runtime reloads.
    """

    def __init__(self, *, active_limit: int = 4, pending_limit: int = 16,
                 per_source_limit: int = 2, per_user_limit: int = 2):
        self.active_limit = _positive_int(active_limit, "active_limit", maximum=64)
        self.pending_limit = _nonnegative_int(pending_limit, "pending_limit", maximum=10_000)
        self.per_source_limit = _positive_int(per_source_limit, "per_source_limit", maximum=self.active_limit)
        self.per_user_limit = _positive_int(per_user_limit, "per_user_limit", maximum=self.active_limit)
        self._active = 0
        self._source_active: Counter[str] = Counter()
        self._user_active: Counter[str] = Counter()
        self._waiters: deque[_Waiter] = deque()
        self._source_waiters: deque[_SourceWaiter] = deque()
        self._lock = asyncio.Lock()

    def acquire(self, *, source_id: str | None = None, user_id: str | None = None,
                timeout: float | None = None,
                operation_deadline: float | None = None,
                transition_timeout: float | None = None) -> _AcquireContext:
        """Return an async context that acquires and releases one slot.

        ``timeout`` bounds only time in the queue; the owner should independently
        bound the complete download. ``source_id`` and ``user_id`` apply fairness
        limits while the permit is held. Empty identifiers are rejected rather
        than turned into shared anonymous buckets.
        """
        source = _identifier(source_id, "source_id")
        user = _identifier(user_id, "user_id")
        wait = _timeout(timeout)
        deadline = _deadline(operation_deadline)
        transition_wait = _timeout(transition_timeout)
        return _AcquireContext(self, source, user, wait, deadline, transition_wait)

    def snapshot(self) -> dict[str, int]:
        """Return queue counts without exposing source or user identifiers."""
        return {
            "active": self._active,
            "pending": len(self._waiters),
            "source_pending": len(self._source_waiters),
            "active_limit": self.active_limit,
            "pending_limit": self.pending_limit,
            "source_active": len(self._source_active),
            "user_active": len(self._user_active),
            "per_source_limit": self.per_source_limit,
            "per_user_limit": self.per_user_limit,
        }

    async def _acquire(self, *, source_id: str | None, user_id: str | None,
                       timeout: float | None, operation_deadline: float | None,
                       transition_timeout: float | None) -> AdmissionPermit:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        async with self._lock:
            if not self._waiters and self._can_start(source_id, user_id):
                return self._reserve(source_id, user_id, operation_deadline, transition_timeout)
            if len(self._waiters) >= self.pending_limit:
                raise AdmissionQueueFull("download admission queue is full", self.snapshot())
            waiter = _Waiter(loop.create_future(), source_id, user_id,
                             operation_deadline=operation_deadline,
                             transition_timeout=transition_timeout)
            self._waiters.append(waiter)
            self._promote()

        try:
            if deadline is None:
                return await asyncio.shield(waiter.future)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AdmissionTimeout("download admission wait timed out", self.snapshot())
            try:
                return await asyncio.wait_for(asyncio.shield(waiter.future), remaining)
            except TimeoutError:
                raise AdmissionTimeout("download admission wait timed out", self.snapshot()) from None
        except BaseException:
            async with self._lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                if not waiter.future.done():
                    waiter.future.cancel()
                if waiter.permit is not None:
                    self._release_locked(waiter.permit)
                    waiter.permit = None
                self._promote_all()
            raise

    async def _release(self, permit: AdmissionPermit) -> None:
        async with self._lock:
            self._remove_source_waiters_locked(permit)
            self._release_locked(permit)
            self._promote_all()

    async def _switch_source(self, permit: AdmissionPermit, source_id: str) -> None:
        loop = asyncio.get_running_loop()
        waiter: _SourceWaiter | None = None
        async with self._lock:
            if permit._owner is not self or permit._released:
                raise AdmissionRejected("download admission permit is no longer active", self.snapshot())
            if permit.source_id == source_id:
                return
            self._release_source_locked(permit)
            self._promote_all()
            if self._source_active[source_id] < self.per_source_limit:
                self._reserve_source_locked(permit, source_id)
                return
            if (len(self._source_waiters) >= self.active_limit
                    or any(item.permit is permit for item in self._source_waiters)):
                raise AdmissionQueueFull("download source transition queue is full", self.snapshot())
            waiter = _SourceWaiter(loop.create_future(), permit, source_id)
            self._source_waiters.append(waiter)
            self._promote_source_waiters()

        try:
            deadlines = [value for value in (
                permit._operation_deadline,
                None if permit._transition_timeout is None else loop.time() + permit._transition_timeout,
            ) if value is not None]
            deadline = min(deadlines) if deadlines else None
            if deadline is None:
                await asyncio.shield(waiter.future)
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise AdmissionTimeout("download source transition timed out", self.snapshot())
                try:
                    await asyncio.wait_for(asyncio.shield(waiter.future), remaining)
                except TimeoutError:
                    if (permit._operation_deadline is not None
                            and permit._operation_deadline <= loop.time()):
                        permit._source_wait_interrupted = True
                    raise AdmissionTimeout("download source transition timed out", self.snapshot()) from None
        except BaseException as exc:
            async with self._lock:
                try:
                    self._source_waiters.remove(waiter)
                except ValueError:
                    pass
                if isinstance(exc, (asyncio.CancelledError, AdmissionTimeout)) and not waiter.future.done():
                    permit._source_wait_interrupted = True
                if waiter.future.done() and permit.source_id == source_id:
                    self._release_source_locked(permit)
                if not waiter.future.done():
                    waiter.future.cancel()
                self._promote_all()
            raise
        async with self._lock:
            if permit._released or permit.source_id != source_id:
                raise AdmissionRejected("download admission permit is no longer active", self.snapshot())

    def _release_locked(self, permit: AdmissionPermit) -> None:
        if permit._owner is not self or permit._released:
            return
        permit._released = True
        self._active -= 1
        self._release_source_locked(permit)
        if permit.user_id is not None:
            _decrement(self._user_active, permit.user_id)

    def _can_start(self, source_id: str | None, user_id: str | None) -> bool:
        return (
            self._active < self.active_limit
            and (source_id is None or self._source_active[source_id] < self.per_source_limit)
            and (user_id is None or self._user_active[user_id] < self.per_user_limit)
        )

    def _reserve(self, source_id: str | None, user_id: str | None,
                 operation_deadline: float | None = None,
                 transition_timeout: float | None = None) -> AdmissionPermit:
        self._active += 1
        if source_id is not None:
            self._source_active[source_id] += 1
        if user_id is not None:
            self._user_active[user_id] += 1
        return AdmissionPermit(self, source_id, user_id, operation_deadline, transition_timeout)

    def _promote_all(self) -> None:
        """Keep transitions ahead of new source reservations when capacity moves."""
        self._promote_source_waiters()
        self._promote()
        self._promote_source_waiters()

    def _release_source_locked(self, permit: AdmissionPermit) -> None:
        source_id = permit.source_id
        if source_id is not None:
            permit.source_id = None
            _decrement(self._source_active, source_id)

    def _reserve_source_locked(self, permit: AdmissionPermit, source_id: str) -> None:
        permit.source_id = source_id
        self._source_active[source_id] += 1

    def _remove_source_waiters_locked(self, permit: AdmissionPermit) -> None:
        for waiter in tuple(self._source_waiters):
            if waiter.permit is permit:
                self._source_waiters.remove(waiter)
                if not waiter.future.done():
                    waiter.future.cancel()

    def _promote_source_waiters(self) -> None:
        """Give freed per-source places to eligible queued transitions first."""
        while True:
            eligible = next((waiter for waiter in self._source_waiters
                             if waiter.permit._released is False
                             and waiter.permit.source_id is None
                             and self._source_active[waiter.source_id] < self.per_source_limit), None)
            if eligible is None:
                return
            self._source_waiters.remove(eligible)
            self._reserve_source_locked(eligible.permit, eligible.source_id)
            if not eligible.future.done():
                eligible.future.set_result(None)

    def _promote(self) -> None:
        """Grant available slots to the earliest eligible queued requests."""
        while self._active < self.active_limit:
            eligible = next((waiter for waiter in self._waiters
                             if self._can_start(waiter.source_id, waiter.user_id)), None)
            if eligible is None:
                return
            self._waiters.remove(eligible)
            if eligible.future.cancelled():
                continue
            eligible.permit = self._reserve(eligible.source_id, eligible.user_id,
                                            eligible.operation_deadline, eligible.transition_timeout)
            eligible.future.set_result(eligible.permit)


def _positive_int(value: int, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def _nonnegative_int(value: int, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def _identifier(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"invalid {name}")
    return value


def _timeout(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid admission timeout")
    return float(value)


def _deadline(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid operation_deadline")
    return float(value)


def _decrement(counter: Counter[str], key: str) -> None:
    counter[key] -= 1
    if counter[key] <= 0:
        del counter[key]
