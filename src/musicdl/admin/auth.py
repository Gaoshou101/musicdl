from __future__ import annotations

import base64
import asyncio
import concurrent.futures
import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .sessions import (REMEMBERED_SESSION_TTL_SECONDS, SESSION_TTL_SECONDS,
                       SessionCapacityError, SessionRegistry, token_digest)


class PasswordHasher:
    algorithm = "pbkdf2_sha256"

    def __init__(self, *, iterations: int = 310_000):
        self.iterations = iterations

    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, self.iterations)
        return f"{self.algorithm}${self.iterations}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"

    def verify(self, password: str, encoded: str) -> bool:
        try:
            algorithm, iterations, salt, expected = encoded.split("$")
            if algorithm != self.algorithm or not 100_000 <= int(iterations) <= 1_000_000:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), int(iterations))
            return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
        except (ValueError, TypeError):
            return False

    def accepts(self, encoded: str) -> bool:
        """Report whether an encoded hash has a usable shape, without a password."""
        try:
            algorithm, iterations, salt, digest = encoded.split("$")
            if algorithm != self.algorithm or not 100_000 <= int(iterations) <= 1_000_000:
                return False
            base64.urlsafe_b64decode(salt)
            base64.urlsafe_b64decode(digest)
        except (AttributeError, ValueError, TypeError):
            return False
        return True


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    user_id: str | None = None
    must_change: bool = False
    credential_version: int = 0


class AuthBusyError(RuntimeError):
    """The bounded password-work queue has no available slot."""


class StaleCredentialsError(RuntimeError):
    """Credentials changed while a password operation was running."""


_PASSWORD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="musicdl-admin-auth")
_PASSWORD_WORK_SLOTS = threading.BoundedSemaphore(4)


async def _run_password_work(function, *args):
    """Run one password operation with at most four accepted jobs per process."""
    if not _PASSWORD_WORK_SLOTS.acquire(blocking=False):
        raise AuthBusyError("administrator password work is at capacity")
    try:
        future = _PASSWORD_EXECUTOR.submit(function, *args)
    except BaseException:
        _PASSWORD_WORK_SLOTS.release()
        raise
    # Release only when the real worker has finished; cancellation of the
    # awaiting request must not cancel an executor-queued job or make a
    # still-running PBKDF2 job look free.
    future.add_done_callback(lambda _completed: _PASSWORD_WORK_SLOTS.release())
    wrapped = asyncio.wrap_future(future)
    try:
        # wrap_future propagates cancellation back to the concurrent future.
        # Shield it so canceling an HTTP request cannot remove a queued work
        # item while the bounded semaphore still represents its real capacity.
        return await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        # The executor job continues after its requester leaves. Consume a
        # possible late exception so no abandoned asyncio Future logs it as
        # never retrieved.
        wrapped.add_done_callback(_consume_password_result)
        raise


def _consume_password_result(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


class AdminAuth:
    CSRF_DOMAIN = b"musicdl/admin/csrf/v1"

    def __init__(self, *, hasher: PasswordHasher | None = None, on_change=None,
                 session_capacity: int = 2048, session_clock=time.time):
        self.hasher = hasher or PasswordHasher()
        self._on_change = on_change
        self._user_id = "admin"
        self._password_hash = self.hasher.hash("password")
        self._must_change = True
        self._credential_version = 1
        self._sessions = SessionRegistry(capacity=session_capacity, clock=session_clock)
        self._bound_csrf: dict[str, str] = {}
        self._lock = threading.RLock()

    def authenticate(self, username: str, password: str) -> AuthResult:
        user_id, encoded, must_change, version = self._credential_snapshot()
        ok = username == user_id and self.hasher.verify(password, encoded)
        return self._finish_auth(ok, username, user_id, must_change, version)

    async def authenticate_async(self, username: str, password: str) -> AuthResult:
        """Verify PBKDF2 off-loop with a bounded running-plus-queued workload."""
        user_id, encoded, must_change, version = self._credential_snapshot()
        ok = await _run_password_work(
            lambda: username == user_id and self.hasher.verify(password, encoded))
        return self._finish_auth(ok, username, user_id, must_change, version)

    def change_credentials(self, user_id: str, username: str, password: str) -> None:
        self._validate_new_credentials(user_id, username, password)
        _, _, _, version = self._credential_snapshot()
        encoded = self.hasher.hash(password)
        self._commit_credentials(user_id, username, encoded, expected_version=version)

    async def change_credentials_async(self, user_id: str, username: str,
                                       password: str, *, expected_version: int) -> None:
        """Hash a replacement off-loop and refuse a stale verification result."""
        self._validate_new_credentials(user_id, username, password)
        encoded = await _run_password_work(self.hasher.hash, password)
        self._commit_credentials(user_id, username, encoded, expected_version=expected_version)

    @staticmethod
    def _validate_new_credentials(user_id: str, username: str, password: str) -> None:
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("invalid credentials")
        if (not isinstance(user_id, str) or not username.strip() or len(password) < 8
                or password == "password"):
            raise ValueError("invalid credentials")

    def _credential_snapshot(self) -> tuple[str, str, bool, int]:
        with self._lock:
            return (self._user_id, self._password_hash, self._must_change,
                    self._credential_version)

    def _finish_auth(self, ok: bool, username: str, user_id: str,
                     must_change: bool, version: int) -> AuthResult:
        with self._lock:
            if (not ok or version != self._credential_version
                    or user_id != self._user_id or username != self._user_id):
                return AuthResult(False)
            return AuthResult(True, self._user_id, self._must_change,
                              self._credential_version)

    def _commit_credentials(self, user_id: str, username: str, encoded: str,
                            *, expected_version: int | None) -> None:
        with self._lock:
            if user_id != self._user_id:
                raise ValueError("invalid credentials")
            if expected_version is not None and expected_version != self._credential_version:
                raise StaleCredentialsError("credentials changed during verification")
            previous = (self._user_id, self._password_hash, self._must_change,
                        self._credential_version, self._sessions.copy_records(),
                        self._bound_csrf.copy())
            self._user_id = username.strip()
            self._password_hash = encoded
            self._must_change = False
            self._credential_version += 1
            self._sessions.clear()
            self._bound_csrf.clear()
            try:
                self._notify()
            except BaseException:
                (self._user_id, self._password_hash, self._must_change,
                 self._credential_version, records, bound_csrf) = previous
                self._sessions.restore_records(records)
                self._bound_csrf = bound_csrf
                raise

    def restore(self, payload) -> None:
        """Adopt persisted credentials, ignoring anything malformed."""
        if not isinstance(payload, dict):
            return
        user_id, encoded, must_change = payload.get("user_id"), payload.get("password_hash"), payload.get("must_change")
        if not isinstance(user_id, str) or not user_id.strip():
            return
        if not isinstance(encoded, str) or not self.hasher.accepts(encoded):
            return
        if not isinstance(must_change, bool):
            return
        version = payload.get("credential_version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            version = 1
        with self._lock:
            self._user_id, self._password_hash, self._must_change = user_id.strip(), encoded, must_change
            self._credential_version = version
            self._sessions.clear()
            self._bound_csrf.clear()

    def restore_sessions(self, payload) -> None:
        with self._lock:
            self._sessions.restore(payload, credential_version=self._credential_version,
                                   user_id=self._user_id,
                                   credential_must_change=self._must_change)
            self._bound_csrf.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {"user_id": self._user_id, "password_hash": self._password_hash,
                    "must_change": self._must_change,
                    "credential_version": self._credential_version}

    def sessions_snapshot(self) -> list[dict]:
        with self._lock:
            return self._sessions.snapshot()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def issue_session(self, *, remember: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        csrf = self.derive_csrf(token)
        with self._lock:
            now = self._sessions.clock()
            ttl = REMEMBERED_SESSION_TTL_SECONDS if remember else SESSION_TTL_SECONDS
            self._sessions.create(
                token, token_digest(csrf), user_id=self._user_id,
                must_change=self._must_change, credential_version=self._credential_version,
                expires_at=now + ttl, remembered=remember)
            active_hashes = self._sessions._records.keys()
            self._bound_csrf = {key: value for key, value in self._bound_csrf.items()
                                if key in active_hashes}
            try:
                self._notify()
            except BaseException:
                self._sessions.revoke(token)
                raise
        return token

    def bind_csrf(self, session: str, token: str) -> None:
        with self._lock:
            record = self._sessions.get(session, credential_version=self._credential_version,
                                        user_id=self._user_id,
                                        must_change=self._must_change)
            if record is None:
                return
            previous_hash, previous_token = record.csrf_hash, self._bound_csrf.get(record.token_hash)
            record.csrf_hash = token_digest(token)
            self._bound_csrf[record.token_hash] = token
            try:
                self._notify()
            except BaseException:
                record.csrf_hash = previous_hash
                if previous_token is None:
                    self._bound_csrf.pop(record.token_hash, None)
                else:
                    self._bound_csrf[record.token_hash] = previous_token
                raise

    def valid_csrf(self, session: str | None, token: str | None) -> bool:
        if not isinstance(session, str) or not isinstance(token, str):
            return False
        with self._lock:
            record = self._sessions.get(session, credential_version=self._credential_version,
                                        user_id=self._user_id,
                                        must_change=self._must_change)
            return bool(record and SessionRegistry.csrf_matches(record.csrf_hash, token))

    def revoke_session(self, token: str) -> None:
        with self._lock:
            record = self._sessions.revoke(token)
            if record is None:
                return
            bound = self._bound_csrf.pop(record.token_hash, None)
            try:
                self._notify()
            except BaseException:
                self._sessions.restore_records({**self._sessions.copy_records(), record.token_hash: record})
                if bound is not None:
                    self._bound_csrf[record.token_hash] = bound
                raise

    def session_user(self, token: str | None) -> str | None:
        with self._lock:
            record = self._sessions.get(token, credential_version=self._credential_version,
                                        user_id=self._user_id,
                                        must_change=self._must_change)
            return record.user_id if record else None

    def session_must_change(self, token: str | None) -> bool:
        with self._lock:
            record = self._sessions.get(token, credential_version=self._credential_version,
                                        user_id=self._user_id,
                                        must_change=self._must_change)
            return bool(record and record.must_change)

    def session_csrf(self, token: str | None, candidate: str | None = None) -> str | None:
        """The CSRF token bound to a session, for a server-rendered form.

        The normal token is a domain-separated HMAC of the browser token. This
        lets a restarted process recover the same value without storing either
        replayable token in its state file.
        """
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            record = self._sessions.get(token, credential_version=self._credential_version,
                                        user_id=self._user_id,
                                        must_change=self._must_change)
            if record is None:
                return None
            bound = self._bound_csrf.get(record.token_hash)
            if bound is not None:
                return bound
            derived = self.derive_csrf(token)
            return derived if SessionRegistry.csrf_matches(record.csrf_hash, derived) else None

    @classmethod
    def derive_csrf(cls, session_token: str) -> str:
        digest = hmac.new(session_token.encode("utf-8"), cls.CSRF_DOMAIN, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def session_remembered(self, token: str | None) -> bool:
        with self._lock:
            record = self._sessions.get(token, credential_version=self._credential_version,
                                        user_id=self._user_id,
                                        must_change=self._must_change)
            return bool(record and record.remembered)

    def session_expires_at(self, token: str | None) -> float | None:
        with self._lock:
            record = self._sessions.get(token, credential_version=self._credential_version,
                                        user_id=self._user_id,
                                        must_change=self._must_change)
            return record.expires_at if record else None

    @property
    def password_hash(self) -> str:
        return self._password_hash


class RateLimiter:
    def __init__(self, *, limit: int = 5, window_seconds: float = 60, clock=time.monotonic, max_keys: int = 4096):
        if isinstance(max_keys, bool) or not isinstance(max_keys, int) or max_keys < 1:
            raise ValueError("invalid rate limiter capacity")
        self.limit, self.window_seconds, self.clock = limit, window_seconds, clock
        self.max_keys = max_keys
        self._attempts: OrderedDict[str, list[float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = self.clock()
        # Last accepted attempts are ordered oldest first. Inactive clients
        # expire without scanning every key on each login or evicting live limits.
        while self._attempts:
            oldest = next(iter(self._attempts))
            stamps = self._attempts[oldest]
            if stamps and now - stamps[-1] < self.window_seconds:
                break
            self._attempts.popitem(last=False)
        if key not in self._attempts and len(self._attempts) >= self.max_keys:
            return False
        values = [stamp for stamp in self._attempts.get(key, []) if now - stamp < self.window_seconds]
        if len(values) >= self.limit:
            self._attempts[key] = values
            return False
        values.append(now)
        self._attempts[key] = values
        self._attempts.move_to_end(key)
        return True
