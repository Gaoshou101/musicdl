"""A Telegram music Bot as one whole source: search and download.

``bots.py`` builds an adapter around a request/response bridge, which covers a
Bot that answers a search command by sending the file.  ``music_v1bot`` does
not: it answers with a numbered list and only sends the audio once the
operator presses a button, so a download has to replay the same search and
press the same button in the same conversation.  That needs the client itself,
which is what ``TelegramBotFlow`` holds.

Both contracts are served here.  Which one a Bot speaks is decided by the reply
it gives -- a listing or a file -- rather than by a setting an operator would
have to know about before the first search either way.
"""

from __future__ import annotations

import math

from musicdl.media.models import DownloadMetadata
from musicdl.sources.models import Candidate, normalize_text

from .bots import MAX_BOT_TIMEOUT, MAX_BOT_RESULTS, validate_bot_username, validate_command_template
from .decoder import (LISTING_ITEM_PREFIX, decode_bot_reply, decode_listing_item,
                      extension_of)

#: How much of a query is kept inside a listing entry's ``item_id``.  A
#: candidate's id may be 256 characters, and the whole point of keeping the
#: query is to send the identical command again, so the search itself is capped
#: at the same length rather than recording something it never sent.
MAX_RECORDED_QUERY = 200
#: A Telegram Bot streams a lossless file at whatever the host's link to
#: Telegram's data centre allows -- measured 2026-09-21 at 0.5 MiB/s for a
#: 53 MiB FLAC, so a minute and a half for one song.  The panel's own budget is
#: sized for a CDN that answers in seconds, and this is what such a channel
#: asks for instead.
DEFAULT_STREAM_BUDGET = 900.0

#: The only failures a caller is told about.  Anything else -- Telethon's own
#: exceptions carry entity names and request details -- becomes one code, so a
#: search that fails cannot leak the account or the query into a panel.
_SAFE_ERRORS = frozenset({"invalid_bot_response", "bot_selection_missing",
                          "too_many_bot_results", "invalid_query"})


def _safe_error(error: BaseException) -> ValueError:
    """The failure code for one exception, without its message."""
    code = str(error)
    return ValueError(code if code in _SAFE_ERRORS else "bot_request_failed")


class TelegramBotSource:
    """One enabled Bot definition, as both a search channel and a resolver."""

    def __init__(self, bot_username: str, flow, *, source_id: str, source_version: str,
                 command_template: str = "/search {query}", timeout: float = 10.0,
                 max_results: int = MAX_BOT_RESULTS,
                 stream_budget_seconds: float = DEFAULT_STREAM_BUDGET):
        validate_bot_username(bot_username)
        validate_command_template(command_template)
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or not 0 < timeout <= MAX_BOT_TIMEOUT):
            raise ValueError("invalid_timeout")
        if (not isinstance(max_results, int) or isinstance(max_results, bool)
                or not 1 <= max_results <= MAX_BOT_RESULTS):
            raise ValueError("invalid_max_results")
        if (not isinstance(stream_budget_seconds, (int, float))
                or isinstance(stream_budget_seconds, bool)
                or not math.isfinite(stream_budget_seconds) or stream_budget_seconds <= 0):
            raise ValueError("invalid_timeout")
        for name, value in (("source_id", source_id), ("source_version", source_version)):
            cleaned = normalize_text(value) if isinstance(value, str) else ""
            if not cleaned or len(cleaned) > 64:
                raise ValueError(f"invalid_{name}")
        self.bot_username = bot_username
        self.flow = flow
        self.source_id = normalize_text(source_id)
        self.source_version = normalize_text(source_version)
        self.command_template = command_template
        self.timeout = float(timeout)
        self.max_results = max_results
        self.stream_budget_seconds = float(stream_budget_seconds)

    def _command(self, query: str) -> str:
        """The command for one query, capped like the id that records it."""
        return self.command_template.replace("{query}", query[:MAX_RECORDED_QUERY])

    def _query(self, value: str) -> str:
        query = normalize_text(value)
        if not query or len(query) > 500:
            raise ValueError("invalid_query")
        return query[:MAX_RECORDED_QUERY]

    async def search(self, query: str) -> list[Candidate]:
        """Ask the Bot what it has for one query."""
        if not isinstance(query, str):
            raise ValueError("invalid_query")
        query = self._query(query)
        try:
            message = await self.flow.ask(self.bot_username, self._command(query), self.timeout)
        except Exception as error:  # noqa: BLE001 - every failure becomes one code
            raise _safe_error(error) from None
        records = decode_bot_reply(message, query=query, max_records=self.max_results)
        try:
            return [Candidate(source_id=self.source_id, source_version=self.source_version,
                              **record.__dict__) for record in records]
        except Exception:
            raise ValueError("invalid_bot_response") from None

    async def download(self, candidate: Candidate) -> DownloadMetadata:
        """Replay the search behind a listing entry and take the file it yields."""
        if not isinstance(candidate, Candidate) or candidate.source_id != self.source_id:
            raise RuntimeError("candidate_identity")
        label, recorded = None, None
        if isinstance(candidate.item_id, str) and candidate.item_id.startswith(LISTING_ITEM_PREFIX):
            label, recorded = decode_listing_item(candidate.item_id)
        query = self._query(recorded or candidate.title)
        try:
            message = await self.flow.select(self.bot_username, self._command(query),
                                             self.timeout, label)
        except Exception as error:  # noqa: BLE001 - every failure becomes one code
            safe = _safe_error(error)
            raise RuntimeError(str(safe)) from None
        document = (getattr(message, "document", None) or getattr(message, "audio", None)
                    or getattr(message, "voice", None))
        declared = getattr(document, "size", None)
        declared = declared if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0 else None
        return DownloadMetadata(chunks=self.flow.stream(message),
                                extension=extension_of(message),
                                media_type=None,
                                declared_size=declared)

    async def health(self) -> bool:
        """Whether the account behind this Bot could talk to Telegram right now."""
        try:
            return bool(await self.flow.ready())
        except Exception:
            return False
