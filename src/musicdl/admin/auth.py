from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


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


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    user_id: str | None = None
    must_change: bool = False


class AdminAuth:
    def __init__(self, *, hasher: PasswordHasher | None = None):
        self.hasher = hasher or PasswordHasher()
        self._user_id = "admin"
        self._password_hash = self.hasher.hash("password")
        self._must_change = True
        self._sessions: dict[str, tuple[str, bool]] = {}
        self._csrf: dict[str, str] = {}

    def authenticate(self, username: str, password: str) -> AuthResult:
        if username != self._user_id or not self.hasher.verify(password, self._password_hash):
            return AuthResult(False)
        return AuthResult(True, self._user_id, self._must_change)

    def change_credentials(self, user_id: str, username: str, password: str) -> None:
        if user_id != self._user_id or len(username.strip()) < 1 or len(password) < 8 or password == "password":
            raise ValueError("invalid credentials")
        self._user_id, self._password_hash, self._must_change = username.strip(), self.hasher.hash(password), False
        self._sessions.clear(); self._csrf.clear()

    def issue_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (self._user_id, self._must_change)
        return token

    def bind_csrf(self, session: str, token: str) -> None:
        if session in self._sessions: self._csrf[session] = token

    def valid_csrf(self, session: str | None, token: str | None) -> bool:
        return bool(session and token and session in self._sessions and hmac.compare_digest(self._csrf.get(session, ""), token))

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)

    def session_user(self, token: str | None) -> str | None:
        value = self._sessions.get(token or "")
        return value[0] if value else None

    def session_must_change(self, token: str | None) -> bool:
        value = self._sessions.get(token or "")
        return bool(value and value[1])

    @property
    def password_hash(self) -> str:
        return self._password_hash


class RateLimiter:
    def __init__(self, *, limit: int = 5, window_seconds: float = 60, clock=time.monotonic):
        self.limit, self.window_seconds, self.clock = limit, window_seconds, clock
        self._attempts: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        values = [stamp for stamp in self._attempts.get(key, []) if now - stamp < self.window_seconds]
        if len(values) >= self.limit:
            self._attempts[key] = values
            return False
        values.append(now)
        self._attempts[key] = values
        return True
