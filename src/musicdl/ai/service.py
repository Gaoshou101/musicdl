from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from musicdl.config import AISettings
from musicdl.sources.models import Candidate, normalize_text
from musicdl.sources.search import SearchResult, search_result_version

from .client import OpenAICompatibleClient
from .models import AICompletionClient, AIError, AIEvent, AIRankResult, emit_event


class _RankingAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordered_tokens: list[str]


def _message(candidates: tuple[Candidate, ...], query: str) -> dict[str, str]:
    disclosed = []
    for index, candidate in enumerate(candidates, 1):
        disclosed.append({
            "token": f"candidate-{index}",
            "title": candidate.title,
            "artist": candidate.artist,
            "album": candidate.album,
            "duration": candidate.duration,
            "bitrate": candidate.bitrate,
            "format": candidate.format,
        })
    return {"role": "user", "content": json.dumps({"query": query, "candidates": disclosed}, ensure_ascii=False, separators=(",", ":"))}


def _fallback(search: SearchResult, code: str, record: Callable[[AIEvent], None] | None) -> AIRankResult:
    emit_event(record, AIEvent("rank", "fallback", code))
    return AIRankResult(search, False, code)


async def advise_ranking(
    search: SearchResult,
    query: str,
    settings: AISettings,
    *,
    client: AICompletionClient | None = None,
    record: Callable[[AIEvent], None] | None = None,
) -> AIRankResult:
    query = normalize_text(query)
    if not settings.enabled:
        return _fallback(search, "disabled", record)
    if len(search.candidates) <= 1:
        return _fallback(search, "not_applicable", record)
    disclosed = tuple(search.candidates[: settings.max_candidates])
    if len(disclosed) <= 1:
        return _fallback(search, "not_applicable", record)
    provider = client or OpenAICompatibleClient(settings)
    try:
        raw = await provider.complete_json([_message(disclosed, query)])
    except asyncio.CancelledError:
        raise
    except AIError as error:
        code = error.code if error.code in {"timeout", "provider_error", "invalid_response"} else "provider_error"
        return _fallback(search, code, record)

    try:
        raw_tokens = raw.get("ordered_tokens") if isinstance(raw, dict) else None
        if not isinstance(raw_tokens, list) or any(not isinstance(token, str) for token in raw_tokens):
            raise ValueError("invalid_advice")
        advice = _RankingAdvice.model_validate(raw)
        expected = {f"candidate-{index}" for index in range(1, len(disclosed) + 1)}
        tokens = advice.ordered_tokens
        if len(tokens) != len(expected) or set(tokens) != expected or len(set(tokens)) != len(tokens):
            raise ValueError("invalid_advice")
    except (ValidationError, TypeError, ValueError):
        return _fallback(search, "invalid_advice", record)

    by_token = {f"candidate-{index}": candidate for index, candidate in enumerate(disclosed, 1)}
    reordered = tuple(by_token[token] for token in tokens) + search.candidates[len(disclosed):]
    updated = SearchResult(reordered, search.statuses, search_result_version(reordered))
    emit_event(record, AIEvent("rank", "applied"))
    return AIRankResult(updated, True)
