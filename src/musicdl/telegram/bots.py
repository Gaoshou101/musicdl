"""Dependency-injected Telegram music bot source adapters."""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from musicdl.sources.models import Candidate, normalize_text


MAX_BOT_TIMEOUT = 120.0
MAX_BOT_RESULTS = 100
_BOT_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")


def validate_bot_username(value: Any) -> str:
    """Return a valid Bot username; the leading ``@`` is not accepted."""
    if not isinstance(value, str) or not _BOT_USERNAME.fullmatch(value):
        raise ValueError("invalid_bot_username")
    return value


def validate_command_template(value: Any) -> str:
    """Return a command template carrying exactly one ``{query}`` placeholder."""
    if (not isinstance(value, str) or value.count("{query}") != 1
            or re.sub(r"\{query\}", "", value).find("{") >= 0
            or "}" in re.sub(r"\{query\}", "", value)):
        raise ValueError("invalid_command_template")
    return value


@dataclass(frozen=True)
class TelegramMediaRecord:
    """Normalized media payload returned by a Telegram bot gateway."""

    item_id: str
    title: str
    artist: str
    album: str | None = None
    duration: int | None = None
    bitrate: int | None = None
    format: str | None = None
    size: int | None = None

    @classmethod
    def from_value(cls, value: Any) -> "TelegramMediaRecord":
        if isinstance(value, cls):
            record = value
        elif isinstance(value, Mapping):
            required = ("item_id", "title", "artist")
            if any(key not in value for key in required):
                raise ValueError("invalid_bot_response")
            try:
                record = cls(**{key: value.get(key) for key in cls.__dataclass_fields__})
            except (TypeError, ValueError):
                raise ValueError("invalid_bot_response") from None
        else:
            raise ValueError("invalid_bot_response")
        try:
            values = {key: getattr(record, key) for key in cls.__dataclass_fields__}
            for key in ("item_id", "title", "artist"):
                if not isinstance(values[key], str) or not normalize_text(values[key]):
                    raise ValueError
            for key in ("album", "format"):
                if values[key] is not None and not isinstance(values[key], str):
                    raise ValueError
            for key in ("duration", "bitrate", "size"):
                if values[key] is not None and (not isinstance(values[key], int) or isinstance(values[key], bool)):
                    raise ValueError
            return cls(
                item_id=normalize_text(values["item_id"]),
                title=normalize_text(values["title"]),
                artist=normalize_text(values["artist"]),
                album=normalize_text(values["album"]) if values["album"] is not None else None,
                duration=values["duration"], bitrate=values["bitrate"],
                format=normalize_text(values["format"]) if values["format"] is not None else None,
                size=values["size"],
            )
        except (AttributeError, TypeError, ValueError):
            raise ValueError("invalid_bot_response") from None


BotRequester = Callable[[str, str, float], Awaitable[Sequence[TelegramMediaRecord] | Sequence[Mapping[str, Any]]]]


class _TelegramBot:
    def __init__(self, bot_username: str, requester: BotRequester, *, source_id: str, source_version: str,
                 command_template: str, timeout: float = 10.0, max_results: int = 100):
        validate_bot_username(bot_username)
        if not callable(requester):
            raise ValueError("invalid_bot_requester")
        validate_command_template(command_template)
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or not 0 < timeout <= MAX_BOT_TIMEOUT):
            raise ValueError("invalid_timeout")
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= MAX_BOT_RESULTS:
            raise ValueError("invalid_max_results")
        try:
            if not isinstance(source_id, str) or not normalize_text(source_id) or len(normalize_text(source_id)) > 64:
                raise ValueError
            if not isinstance(source_version, str) or not normalize_text(source_version) or len(normalize_text(source_version)) > 64:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("invalid_source") from None
        self.bot_username, self.requester = bot_username, requester
        self.source_id, self.source_version = normalize_text(source_id), normalize_text(source_version)
        self.command_template, self.timeout, self.max_results = command_template, float(timeout), max_results

    async def search(self, query: str) -> list[Candidate]:
        if not isinstance(query, str):
            raise ValueError("invalid_query")
        query = normalize_text(query)
        if not query or len(query) > 256:
            raise ValueError("invalid_query")
        command = self.command_template.replace("{query}", query)
        try:
            response = self.requester(self.bot_username, command, self.timeout)
            if not inspect.isawaitable(response):
                raise ValueError
            response = await response
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc) == "invalid_bot_response":
                raise
            raise ValueError("bot_request_failed") from None
        if isinstance(response, (str, bytes, bytearray)) or not isinstance(response, Sequence):
            raise ValueError("invalid_bot_response")
        if len(response) > self.max_results:
            raise ValueError("too_many_bot_results")
        records = [TelegramMediaRecord.from_value(item) for item in response]
        try:
            return [Candidate(source_id=self.source_id, source_version=self.source_version, **record.__dict__) for record in records]
        except Exception:
            raise ValueError("invalid_bot_response") from None


class PublicTelegramBot(_TelegramBot):
    def __init__(self, bot_username: str, requester: BotRequester, *, source_id: str, source_version: str,
                 timeout: float = 10.0, max_results: int = 100):
        super().__init__(bot_username, requester, source_id=source_id, source_version=source_version,
                         command_template="/search {query}", timeout=timeout, max_results=max_results)


class CustomTelegramBot(_TelegramBot):
    def __init__(self, bot_username: str, requester: BotRequester, *, command_template: str,
                 source_id: str, source_version: str, timeout: float = 10.0, max_results: int = 100):
        super().__init__(bot_username, requester, source_id=source_id, source_version=source_version,
                         command_template=command_template, timeout=timeout, max_results=max_results)


# Concise aliases for callers that prefer adapter terminology.
PublicBotAdapter = PublicTelegramBot
CustomBotAdapter = CustomTelegramBot
