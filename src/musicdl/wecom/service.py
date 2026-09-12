import hashlib
import logging
import time
from typing import Any, Protocol

from fastapi import Request

from .commands import parse_command
from .crypto import WeComCrypto, WeComCryptoError, verify_signature
from .models import WeComMessage
from .state import StateUnavailable
from .xml import XmlLimitError, parse_envelope, parse_message

LOG = logging.getLogger("musicdl.wecom")
MAX_BODY = 64 * 1024


class StateLike(Protocol):
    async def enqueue_message(self, corp_id: str, from_user: str, request_id: str, payload: dict[str, Any], ttl: int = 86400): ...
    async def lookup_message(self, corp_id: str, request_id: str) -> bool: ...
    async def ping(self) -> bool: ...


def _fresh(timestamp: str, now: float, skew: int) -> bool:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(now - value) <= skew


def _audit(event: str, reason: str, request_id: str = "") -> None:
    fingerprint = hashlib.sha256(request_id.encode()).hexdigest()[:16] if request_id else ""
    LOG.info("wecom_callback event=%s reason=%s request_fingerprint=%s", event, reason, fingerprint)


async def read_limited(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            raise ValueError("body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _request_id(msg: WeComMessage) -> str:
    if msg.msg_id:
        return msg.msg_id
    raw = "|".join((str(msg.agent_id or ""), msg.from_user, str(msg.create_time), msg.msg_type, msg.event or "", msg.content))
    return "event-" + hashlib.sha256(raw.encode()).hexdigest()


class WeComService:
    def __init__(self, settings, state: StateLike, clock=time.time, crypto=None):
        self.settings = settings
        self.state = state
        self.clock = clock
        self.crypto = crypto or WeComCrypto(settings.encoding_aes_key.get_secret_value(), settings.corp_id)

    def _query(self, request: Request):
        q = request.query_params
        signature, timestamp, nonce = q.get("msg_signature", ""), q.get("timestamp", ""), q.get("nonce", "")
        if len(signature) != 40 or any(c not in "0123456789abcdefABCDEF" for c in signature):
            raise ValueError("invalid_request")
        if not timestamp.isdecimal() or len(timestamp) > 16 or not nonce or len(nonce) > 128:
            raise ValueError("invalid_request")
        return signature, timestamp, nonce

    async def verify_get(self, request: Request) -> str:
        signature, timestamp, nonce = self._query(request)
        encrypted = request.query_params.get("echostr", "")
        if not signature or not timestamp or not nonce or not encrypted or not _fresh(timestamp, self.clock(), self.settings.clock_skew):
            raise ValueError("invalid_request")
        try:
            verify_signature(self.settings.token.get_secret_value(), timestamp, nonce, encrypted, signature)
            return self.crypto.decrypt(encrypted)
        except (WeComCryptoError, ValueError):
            raise ValueError("invalid_request") from None

    async def handle_post(self, request: Request) -> str:
        encoding = request.headers.get("content-encoding", "identity").lower()
        if encoding not in {"", "identity"}:
            raise ValueError("unsupported_encoding")
        signature, timestamp, nonce = self._query(request)
        if not signature or not timestamp or not nonce:
            raise ValueError("invalid_request")
        body = await read_limited(request)
        try:
            envelope = parse_envelope(body)
            encrypted = envelope.encrypt
            verify_signature(self.settings.token.get_secret_value(), timestamp, nonce, encrypted, signature)
            inner = self.crypto.decrypt(encrypted).encode()
            msg = parse_message(inner)
        except (WeComCryptoError, XmlLimitError, UnicodeError, ValueError):
            raise ValueError("invalid_request") from None
        request_id = _request_id(msg)
        if msg.agent_id != self.settings.agent_id:
            raise PermissionError("agent_denied")
        if msg.from_user not in self.settings.allowed_users:
            raise PermissionError("user_denied")
        if not _fresh(timestamp, self.clock(), self.settings.clock_skew):
            try:
                known = await self.state.lookup_message(self.settings.corp_id, request_id)
            except StateUnavailable:
                raise
            if known:
                _audit("ack", "stale_duplicate", request_id)
                return ""
            raise ValueError("stale_request")
        if msg.msg_type != "text":
            _audit("ack", "unsupported_type", request_id)
            return ""
        try:
            command = parse_command(msg.content)
        except ValueError:
            _audit("ack", "invalid_command", request_id)
            return ""
        payload = {"command": command.kind.value, "value": command.value, "msg_type": msg.msg_type}
        await self.state.enqueue_message(self.settings.corp_id, msg.from_user, request_id, payload, self.settings.dedup_ttl)
        _audit("enqueue", "accepted", request_id)
        return ""
