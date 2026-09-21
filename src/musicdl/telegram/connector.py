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


def _media_of(message: Any) -> Any | None:
    """The file one Bot reply carries, if it carries one."""
    return (getattr(message, "document", None) or getattr(message, "audio", None)
            or getattr(message, "voice", None))


def button_index(message: Any, label: str) -> int:
    """Where the inline button carrying ``label`` sits, counted across rows.

    A Bot numbers its entries per listing, so the label is what identifies the
    entry the operator picked; the position is only how Telethon reaches it.
    """
    wanted = "" if label is None else label.strip()
    rows = getattr(getattr(message, "reply_markup", None), "rows", None) or ()
    position = 0
    for row in rows:
        for button in getattr(row, "buttons", None) or ():
            text = getattr(button, "text", None)
            if isinstance(text, str) and text.strip() == wanted and wanted:
                return position
            position += 1
    raise ValueError("bot_selection_missing")


class TelegramBotFlow:
    """A Bot conversation as two moves: ask one question, fetch one file.

    A Bot that answers with a numbered list needs both moves in the same
    conversation -- the listing is the search result and the button press is
    the download -- and the file that comes back is streamed by the same
    client.  Which move a Bot needs is decided by the adapter above this; the
    flow only knows how to send a command, press a button, and read bytes.
    """

    #: A Bot that announces itself ("正在上传...") before the file has to be
    #: read past, and a chatty one must not hold a worker open forever.
    MAX_REPLIES = 4
    #: 512 KiB per request measured ~0.5 MiB/s from the deployment host to
    #: Telegram's DC on 2026-09-21; the bound only keeps a caller from asking
    #: for a chunk size Telethon will not honour.
    DEFAULT_REQUEST_SIZE = 512 * 1024

    def __init__(self, connector: "TelegramConnector", profile: str):
        self._connector = connector
        self.profile = profile

    async def _client(self):
        client = await self._connector._client(self.profile)
        if not await client.is_user_authorized():
            raise RuntimeError("telegram client is not authorized")
        return client

    async def ready(self) -> bool:
        """Whether this flow could talk to Telegram right now, without asking."""
        try:
            return await self._client() is not None
        except Exception:  # noqa: BLE001 - a probe reports, it does not raise
            return False

    async def ask(self, bot_username: str, command: str, timeout: float) -> Any:
        """Send one command and return the reply it produced."""
        client = await self._client()
        async with self._connector.bot_lock(self.profile, bot_username):
            async with client.conversation(bot_username, timeout=timeout) as conversation:
                await conversation.send_message(command)
                return await conversation.get_response()

    async def select(self, bot_username: str, command: str, timeout: float,
                     label: str | None = None) -> Any:
        """Replay one search and return the file reply it leads to.

        ``label`` is the listing button to press.  Without one the Bot answers
        with the file itself, which is the older contract; either way the reply
        that carries a file is what comes back.
        """
        client = await self._client()
        async with self._connector.bot_lock(self.profile, bot_username):
            async with client.conversation(bot_username, timeout=timeout) as conversation:
                await conversation.send_message(command)
                reply = await conversation.get_response()
                if label is not None:
                    await reply.click(button_index(reply, label))
                    reply = None
                for _ in range(self.MAX_REPLIES):
                    if reply is None:
                        reply = await conversation.get_response(timeout=timeout)
                    if _media_of(reply) is not None:
                        return reply
                    reply = None
        raise ValueError("invalid_bot_response")

    async def stream(self, message: Any, *, request_size: int | None = None):
        """Yield the bytes of one file reply, in the order Telegram serves them."""
        if isinstance(request_size, bool) or (request_size is not None
                                              and (not isinstance(request_size, int)
                                                   or not 1024 <= request_size <= 1024 * 1024)):
            raise ValueError("invalid_chunk_size")
        document = _media_of(message)
        if document is None:
            raise ValueError("invalid_bot_response")
        client = await self._client()
        async for chunk in client.iter_download(
                document, request_size=request_size or self.DEFAULT_REQUEST_SIZE):
            if isinstance(chunk, bytearray):
                chunk = bytes(chunk)
            if isinstance(chunk, bytes) and chunk:
                yield chunk


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
            response = await self.bot_flow(profile).ask(bot_username, command, timeout)
            decoded = decoder(response)
            if inspect.isawaitable(decoded):
                decoded = await decoded
            return decoded

        return request

    def bot_lock(self, profile: str, bot_username: str) -> asyncio.Lock:
        """The lock for one Bot in one profile.

        Two searches landing in the same chat at once would answer each other's
        requests -- the Bot replies in order, not in pairs -- so every
        conversation with a Bot is serialised here, and the requester and the
        flow above share the one lock.
        """
        key = (profile, str(bot_username).lower())
        lock = self._bot_locks.get(key)
        if lock is None:
            lock = self._bot_locks[key] = asyncio.Lock()
        return lock

    def bot_flow(self, profile: str) -> TelegramBotFlow:
        """The conversation, button, and file primitives for one profile."""
        return TelegramBotFlow(self, profile)

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
