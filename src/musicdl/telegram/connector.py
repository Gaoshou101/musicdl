import asyncio
import inspect
import os
import re
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

    def __init__(self, session_root: str | os.PathLike, api_id: int, api_hash: str, client_factory: Callable):
        self.root = Path(session_root).expanduser().resolve()
        self.api_id = api_id
        self._api_hash = api_hash
        self.factory = client_factory
        self._clients: dict[str, TelegramClientProtocol] = {}
        self._bot_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._harden(self.root)

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
                return TelegramResult(TelegramStatus.READY)
            await client.send_code_request(phone)
            return TelegramResult(TelegramStatus.CODE_REQUIRED)
        except Exception as exc:
            return self._failure(exc)

    async def complete_code(self, profile: str, phone: str, code: str) -> TelegramResult:
        try:
            client = await self._client(profile)
            await client.sign_in(phone=phone, code=code)
            if not await client.is_user_authorized():
                return TelegramResult(TelegramStatus.INVALID_SESSION)
            return TelegramResult(TelegramStatus.READY)
        except Exception as exc:
            return self._failure(exc)

    async def complete_password(self, profile: str, password: str) -> TelegramResult:
        try:
            client = await self._client(profile)
            await client.sign_in(password=password)
            if not await client.is_user_authorized():
                return TelegramResult(TelegramStatus.INVALID_SESSION)
            return TelegramResult(TelegramStatus.READY)
        except Exception as exc:
            return self._failure(exc)

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
