"""One tiny completion, reported the way an operator's screen needs it.

The advisor only ever needs the code -- it falls back to the deterministic
answer either way, so a failed call is invisible by design. An operator filling
in an endpoint needs the opposite: which failure it was, what the endpoint said
about it, and how long it took, because "AI 没生效" is otherwise unattributable
from the panel and costs a log window and a container to explain.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from musicdl.config import AISettings

from .client import OpenAICompatibleClient
from .models import AICompletionClient, AIError

# A probe is a screen an operator waits on, so it carries its own ceiling. An
# endpoint configured for two minutes should not hold a browser open that long
# to say that it answered, and a probe that waits longer than the advisor's own
# budget would report on a patience the product does not have.
MAX_AI_PROBE_SECONDS = 30.0

# How much of the answer the panel echoes back: enough to see the model answered
# something rather than nothing, not enough to carry a whole completion into a
# settings screen.
MAX_AI_PROBE_REPLY_CHARS = 200

# The smallest request the contract admits. The system turn is what satisfies
# ``response_format: json_object`` -- an OpenAI-compatible endpoint refuses that
# without the word JSON somewhere in the prompt -- and it leaves the model no
# question to interpret.
PROBE_MESSAGES: tuple[dict[str, str], ...] = (
    {"role": "system", "content": 'Reply with JSON only, exactly {"ok": true}.'},
    {"role": "user", "content": "ping"},
)


@dataclass(frozen=True)
class AIProbeResult:
    """What one test request cost and what it said.

    ``code`` is the advisor's own vocabulary, so the panel and the fallback path
    name a failure the same way. ``detail`` is the endpoint's own evidence,
    which is never a secret: the client reports a status line, not a body.
    """

    ok: bool
    code: str | None
    detail: str | None
    took_ms: int
    model: str | None
    budget_ms: int
    reply: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "detail": self.detail,
                "took_ms": self.took_ms, "model": self.model,
                "budget_ms": self.budget_ms, "reply": self.reply}


def _result(*, ok: bool, code: str | None, detail: str | None, took_ms: int,
            model: str | None, budget_ms: int, reply: str | None = None) -> AIProbeResult:
    return AIProbeResult(ok=ok, code=code, detail=detail, took_ms=took_ms,
                         model=model, budget_ms=budget_ms, reply=reply)


async def probe_endpoint(settings: AISettings,
                         *, client: AICompletionClient | None = None) -> AIProbeResult:
    """Ask the configured endpoint for one tiny answer.

    A deployment with the advisor switched off, or with credentials this screen
    cannot complete, is reported as such without a request leaving the process:
    the point of the button is to say what is wrong, and spending 30 seconds to
    learn that no key is stored is not a diagnosis.
    """
    if not settings.enabled:
        return _result(ok=False, code="disabled", detail=None, took_ms=0,
                       model=settings.model, budget_ms=0)
    if settings.api_key is None or not settings.api_key.get_secret_value() or not settings.model:
        return _result(ok=False, code="unconfigured", detail=None, took_ms=0,
                       model=settings.model, budget_ms=0)

    budget = min(settings.timeout, MAX_AI_PROBE_SECONDS)
    provider = client or OpenAICompatibleClient(settings.model_copy(update={"timeout": budget}))
    started = time.perf_counter()
    try:
        answer = await provider.complete_json(list(PROBE_MESSAGES))
    except asyncio.CancelledError:
        raise
    except AIError as error:
        took_ms = int((time.perf_counter() - started) * 1000)
        return _result(ok=False, code=error.code, detail=error.detail, took_ms=took_ms,
                       model=settings.model, budget_ms=int(budget * 1000))
    took_ms = int((time.perf_counter() - started) * 1000)
    reply = json.dumps(answer, ensure_ascii=False)[:MAX_AI_PROBE_REPLY_CHARS]
    return _result(ok=True, code=None, detail=None, took_ms=took_ms,
                   model=settings.model, budget_ms=int(budget * 1000), reply=reply)
