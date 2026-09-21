"""The panel's test button: one request, and a report an operator can read."""

from __future__ import annotations

import asyncio
import json

import pytest

from musicdl.ai import AIError, diagnose
from musicdl.ai.diagnose import MAX_AI_PROBE_SECONDS, PROBE_MESSAGES, probe_endpoint
from musicdl.config import AISettings


def settings(**kwargs) -> AISettings:
    values = {
        "enabled": True,
        "base_url": "https://provider.example/v1",
        "api_key": "phase5-secret",
        "model": "phase5-model",
        "timeout": 4.0,
    }
    values.update(kwargs)
    return AISettings(**values)


class FakeClient:
    """A client that answers once, or refuses the way the real one would."""

    def __init__(self, answer=None, error=None):
        self.answer = answer if answer is not None else {"ok": True}
        self.error = error
        self.messages = None

    async def complete_json(self, messages):
        self.messages = list(messages)
        if self.error is not None:
            raise self.error
        return self.answer


class ExplodingClient:
    async def complete_json(self, messages):  # pragma: no cover - must not run
        raise AssertionError("a request left the process")


def run(coro):
    return asyncio.run(coro)


def test_a_disabled_advisor_answers_without_a_request():
    result = run(probe_endpoint(settings(enabled=False), client=ExplodingClient()))
    assert (result.ok, result.code, result.took_ms, result.budget_ms) == (False, "disabled", 0, 0)


def test_credentials_the_advisor_cannot_use_are_reported_without_a_request():
    # The panel adopts a cleared secret onto the live object, so this state is
    # reachable even though the model itself refuses to be built with it.
    disabled = settings()
    disabled.api_key = None
    result = run(probe_endpoint(disabled, client=ExplodingClient()))
    assert (result.ok, result.code) == (False, "unconfigured")

    modelless = settings()
    modelless.model = None
    assert run(probe_endpoint(modelless, client=ExplodingClient())).code == "unconfigured"


def test_a_working_endpoint_reports_the_model_the_answer_and_the_round_trip():
    client = FakeClient(answer={"ok": True})
    result = run(probe_endpoint(settings(), client=client))
    assert result.ok is True
    assert (result.code, result.detail) == (None, None)
    assert result.model == "phase5-model"
    assert result.budget_ms == 4_000
    assert result.took_ms >= 0
    assert json.loads(result.reply) == {"ok": True}
    assert client.messages == list(PROBE_MESSAGES)
    # An OpenAI-compatible endpoint rejects `response_format: json_object` unless
    # the prompt itself mentions JSON, and the client always sends it.
    assert "JSON" in client.messages[0]["content"]


def test_a_failure_keeps_its_code_and_the_endpoint_own_status_line():
    client = FakeClient(error=AIError("provider_error", "HTTP 401"))
    result = run(probe_endpoint(settings(), client=client))
    assert (result.ok, result.code, result.detail) == (False, "provider_error", "HTTP 401")
    assert result.reply is None
    assert result.budget_ms == 4_000


def test_the_probe_waits_on_its_own_ceiling_not_the_deployed_one(monkeypatch):
    seen = {}

    class Recording:
        def __init__(self, settings):
            seen["timeout"] = settings.timeout

        async def complete_json(self, messages):
            return {"ok": True}

    monkeypatch.setattr(diagnose, "OpenAICompatibleClient", Recording)
    result = run(probe_endpoint(settings(timeout=120.0)))
    assert seen["timeout"] == MAX_AI_PROBE_SECONDS
    assert result.budget_ms == int(MAX_AI_PROBE_SECONDS * 1000)


def test_the_answer_is_bounded_before_it_reaches_the_screen():
    reply = run(probe_endpoint(settings(), client=FakeClient(answer={"blob": "x" * 5_000})))
    assert len(reply.reply) == diagnose.MAX_AI_PROBE_REPLY_CHARS


def test_a_cancelled_probe_stays_cancelled():
    class Cancelling:
        async def complete_json(self, messages):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(probe_endpoint(settings(), client=Cancelling()))
