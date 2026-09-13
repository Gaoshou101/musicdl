import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx

from musicdl.config import AISettings

from .models import AIError

MAX_AI_CONTENT_CHARS = 16_384
MAX_AI_BODY_BYTES = 65_536
MAX_AI_JSON_DEPTH = 128


def _exceeds_json_depth(value: str | bytes, maximum: int = MAX_AI_JSON_DEPTH) -> bool:
    """Check JSON nesting without recursively parsing attacker-controlled input."""
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if isinstance(character, int):
            character = chr(character)
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                return True
        elif character in "]}":
            depth -= 1
    return False


class OpenAICompatibleClient:
    def __init__(self, settings: AISettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._transport = transport

    async def complete_json(self, messages: Sequence[dict[str, str]]) -> dict[str, Any]:
        endpoint = str(self._settings.base_url).rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.api_key.get_secret_value()}"}
        payload = {
            "model": self._settings.model,
            "messages": list(messages),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._settings.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with asyncio.timeout(self._settings.timeout):
                    async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                        response.raise_for_status()
                        chunks = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_AI_BODY_BYTES:
                                raise ValueError("response body too large")
                            chunks.append(chunk)
                        body = b"".join(chunks)
        except (httpx.ReadTimeout, asyncio.TimeoutError):
            raise AIError("timeout") from None
        except httpx.HTTPError:
            raise AIError("provider_error") from None
        except ValueError:
            raise AIError("invalid_response") from None

        try:
            if _exceeds_json_depth(body):
                raise ValueError
            outer = json.loads(body)
            choices = outer["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            if not isinstance(choices[0], dict):
                raise ValueError
            if "message" not in choices[0] or not isinstance(choices[0]["message"], dict):
                raise ValueError
            if "content" not in choices[0]["message"]:
                raise ValueError
            content = choices[0]["message"]["content"]
            if not isinstance(content, str) or len(content) > MAX_AI_CONTENT_CHARS:
                raise ValueError
            if _exceeds_json_depth(content):
                raise ValueError
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError, RecursionError):
            raise AIError("invalid_response") from None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._settings.model!r})"
