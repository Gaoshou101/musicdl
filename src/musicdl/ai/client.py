import json
from collections.abc import Sequence
from typing import Any

import httpx

from musicdl.config import AISettings

from .models import AIError

MAX_AI_CONTENT_CHARS = 16_384


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
            ) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
        except httpx.ReadTimeout:
            raise AIError("timeout") from None
        except httpx.HTTPError:
            raise AIError("provider_error") from None

        try:
            outer = response.json()
            choices = outer["choices"]
            content = choices[0]["message"]["content"]
            if not isinstance(content, str) or len(content) > MAX_AI_CONTENT_CHARS:
                raise ValueError
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            raise AIError("invalid_response") from None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._settings.model!r})"
