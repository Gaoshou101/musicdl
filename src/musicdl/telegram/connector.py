import asyncio
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import TelegramResult, TelegramStatus


_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class TelegramClientProtocol(Protocol):
    async def connect(self) -> None: ...
    async def is_user_authorized(self) -> bool: ...
    async def send_code_request(self, phone: str) -> Any: ...
    async def sign_in(self, **kwargs: Any) -> Any: ...


def telethon_client_factory(api_id: int, api_hash: str, proxy: Any = None) -> Callable[[Path], TelegramClientProtocol]:
    """Build a lazy Telethon factory; importing Telethon is deferred until use."""
    def factory(session_path: Path) -> TelegramClientProtocol:
        from telethon import TelegramClient
        kwargs = {"flood_sleep_threshold": 0}
        if proxy is not None:
            kwargs["proxy"] = proxy
        return TelegramClient(session_path, api_id, api_hash, **kwargs)
    return factory


class TelegramConnector:
    """Small orchestration boundary around an injectable Telegram client."""

    # How long a half-finished login stays usable. Telegram's own code lives
    # for a few minutes and the hash is useless without it, so this only has to
    # outlast one operator's typing, not be a credential of its own.
    PENDING_TTL_SECONDS = 900

    def __init__(self, session_root: str | os.PathLike, api_id: int, api_hash: str, client_factory: Callable):
        self.root = Path(session_root).expanduser().resolve()
        self.api_id = api_id
        self._api_hash = api_hash
        self.factory = client_factory
        self._clients: dict[str, TelegramClientProtocol] = {}
        self._bot_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._pending_memory: dict[str, Any] | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        self._harden(self.root)

    # -- the half-finished login ----------------------------------------

    def pending_path(self, profile: str) -> Path:
        base = self.session_path(profile)
        return base.with_name(base.name + ".login.json")

    def _remember_login(self, profile: str, phone: str, code_hash: str | None) -> None:
        """Write down what the second step of a first login needs.

        Telethon keeps the phone_code_hash on the client that asked for the
        code, and every reload of the surrounding runtime builds a new one --
        the panel would ask the operator for a second code for no reason. It
        goes beside the session it belongs to instead, in a file only this
        account on this host can read.
        """
        payload = {"phone": phone, "phone_code_hash": code_hash or "", "sent_at": time.time()}
        path = self.pending_path(profile)
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
        except OSError:  # pragma: no cover - a volume that cannot be written
            self._pending_memory = payload

    def pending_login(self, profile: str) -> dict[str, Any]:
        """The login step waiting to be finished, if there is one."""
        payload: Any = self._pending_memory
        try:
            payload = json.loads(self.pending_path(profile).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(payload, dict):
            return {}
        sent_at = payload.get("sent_at")
        if isinstance(sent_at, (int, float)) and time.time() - sent_at > self.PENDING_TTL_SECONDS:
            return {}
        return payload

    def _forget_login(self, profile: str) -> None:
        self._pending_memory = None
        try:
            self.pending_path(profile).unlink()
        except OSError:
            pass

    @staticmethod
    def _harden(path: Path) -> None:
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass

    def _harden_session(self, profile: str) -> None:
        base = self.session_path(profile)
        for path in (base.with_name(base.name + ".session"), base.with_name(base.name + ".session-wal"), base.with_name(base.name + ".session-shm")):
            if path.exists():
                self._harden(path)

    def session_path(self, profile: str) -> Path:
        if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
            raise ValueError("invalid Telegram profile")
        path = (self.root / profile).resolve()
        if path.parent != self.root:
            raise ValueError("Telegram session path escapes configured root")
        return path

    async def _client(self, profile: str):
        path = self.session_path(profile)
        client = self._clients.get(profile)
        if client is None:
            client = self.factory(path)
            if inspect.isawaitable(client):
                client = await client
            self._clients[profile] = client
        await client.connect()
        self._harden_session(profile)
        return client

    @staticmethod
    def _failure(exc: Exception) -> TelegramResult:
        name = type(exc).__name__
        if name in {"FloodWaitError", "FloodWait"}:
            return TelegramResult(TelegramStatus.RATE_LIMITED, retry_after=int(getattr(exc, "seconds", 0)))
        if name in {"UnauthorizedError", "AuthKeyUnregisteredError", "SessionRevokedError"}:
            return TelegramResult(TelegramStatus.INVALID_SESSION)
        if name in {"SessionPasswordNeededError"}:
            return TelegramResult(TelegramStatus.PASSWORD_REQUIRED)
        return TelegramResult(TelegramStatus.ERROR, error="telegram operation failed")

    async def begin_login(self, profile: str, phone: str) -> TelegramResult:
        try:
            client = await self._client(profile)
            if await client.is_user_authorized():
                self._forget_login(profile)
                return TelegramResult(TelegramStatus.READY)
            sent = await client.send_code_request(phone)
            code_hash = getattr(sent, "phone_code_hash", None)
            self._remember_login(profile, phone,
                                 code_hash if isinstance(code_hash, str) else None)
            return TelegramResult(TelegramStatus.CODE_REQUIRED)
        except Exception as exc:
            return self._failure(exc)

    async def complete_code(self, profile: str, phone: str | None = None,
                            code: str = "") -> TelegramResult:
        """Finish a login with the code Telegram sent.

        The phone and the code hash come from ``begin_login`` when the caller
        does not repeat them, and are passed to Telethon explicitly: a session
        that was authorised on another client instance still signs in.
        """
        try:
            client = await self._client(profile)
            pending = self.pending_login(profile)
            number = phone or pending.get("phone")
            if not number:
                return TelegramResult(TelegramStatus.ERROR,
                                      error="telegram login was not started")
            kwargs: dict[str, Any] = {"phone": number, "code": code}
            if pending.get("phone_code_hash"):
                kwargs["phone_code_hash"] = pending["phone_code_hash"]
            try:
                await client.sign_in(**kwargs)
            except Exception as exc:
                if type(exc).__name__ == "SessionPasswordNeededError":
                    return TelegramResult(TelegramStatus.PASSWORD_REQUIRED)
                raise
            if not await client.is_user_authorized():
                return TelegramResult(TelegramStatus.INVALID_SESSION)
            self._forget_login(profile)
            return TelegramResult(TelegramStatus.READY)
        except Exception as exc:
            return self._failure(exc)

    async def complete_password(self, profile: str, password: str) -> TelegramResult:
        try:
            client = await self._client(profile)
            await client.sign_in(password=password)
            if not await client.is_user_authorized():
                return TelegramResult(TelegramStatus.INVALID_SESSION)
            self._forget_login(profile)
            return TelegramResult(TelegramStatus.READY)
        except Exception as exc:
            return self._failure(exc)

    def pending_phone(self, profile: str) -> str | None:
        """The number a half-finished login is waiting on, if there is one."""
        phone = self.pending_login(profile).get("phone")
        return phone if isinstance(phone, str) and phone else None

    async def logout(self, profile: str) -> TelegramResult:
        """Drop the stored session and leave the connector signed out.

        An operator who signed in with the wrong account needs a way back that
        does not mean deleting a file inside the container; the session is
        removed locally, and the next login writes a fresh one.
        """
        path = self.session_path(profile)
        self._forget_login(profile)
        client = self._clients.pop(profile, None)
        if client is not None:
            closer = getattr(client, "disconnect", None)
            if callable(closer):
                result = closer()
                if inspect.isawaitable(result):
                    await result
        for suffix in (".session", ".session-wal", ".session-shm", ".session-journal"):
            candidate = path.with_name(path.name + suffix)
            try:
                if candidate.exists():
                    candidate.unlink()
            except OSError:
                return TelegramResult(TelegramStatus.ERROR,
                                      error="telegram session file could not be removed")
        return TelegramResult(TelegramStatus.INVALID_SESSION)

    def bot_requester(self, profile: str, decoder: Callable) -> Callable:
        """Build a safe async bridge for requesting one response from a bot."""
        if not callable(decoder):
            raise TypeError("decoder must be callable")

        async def request(bot_username: str, command: str, timeout: float):
            client = await self._client(profile)
            if not await client.is_user_authorized():
                raise RuntimeError("telegram client is not authorized")
            lock_key = (profile, bot_username.lower())
            if lock_key not in self._bot_locks:
                self._bot_locks[lock_key] = asyncio.Lock()
            async with self._bot_locks[lock_key]:
                async with client.conversation(bot_username, timeout=timeout) as conversation:
                    await conversation.send_message(command)
                    response = await conversation.get_response()
            decoded = decoder(response)
            if inspect.isawaitable(decoded):
                decoded = await decoded
            return decoded

        return request

    async def restore(self, profile: str) -> TelegramResult:
        try:
            client = await self._client(profile)
            if await client.is_user_authorized():
                return TelegramResult(TelegramStatus.READY)
            return TelegramResult(TelegramStatus.INVALID_SESSION)
        except Exception as exc:
            return self._failure(exc)

    async def disconnect(self) -> None:
        """Cleanly disconnect all active clients and clear memory references."""
        for client in list(self._clients.values()):
            if hasattr(client, "disconnect") and callable(client.disconnect):
                res = client.disconnect()
                if inspect.isawaitable(res):
                    await res
        self._clients.clear()
        self._bot_locks.clear()
