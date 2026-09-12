import asyncio
import hashlib

import pytest

from musicdl.wecom.state import (
    RedisStateStore,
    SelectionContext,
    SelectionRejected,
    StateUnavailable,
)


class AsyncEvalClient:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = []

    async def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        if self.error:
            raise self.error
        return self.results.pop(0)


def run(awaitable):
    return asyncio.run(awaitable)


def context():
    return SelectionContext("corp", "user", "req", "v1", {1: "candidate-9"})


def test_enqueue_message_queued_and_duplicate_are_explicit_statuses():
    client = AsyncEvalClient([[0, b"171-0"], [1, b"171-0"]])
    store = RedisStateStore(client)
    first = run(store.enqueue_message("c", "u", "r", {"text": "hi"}))
    second = run(store.enqueue_message("c", "u", "r", {"text": "hi"}))
    assert (first.stream_id, first.duplicate) == ("171-0", False)
    assert (second.stream_id, second.duplicate) == ("171-0", True)


def test_enqueue_message_rejects_unknown_response_shape():
    with pytest.raises(StateUnavailable):
        run(RedisStateStore(AsyncEvalClient([b"171-0"])).enqueue_message("c", "u", "r", {}))


def test_redis_errors_are_stable():
    with pytest.raises(StateUnavailable, match="^state store unavailable$"):
        run(
            RedisStateStore(AsyncEvalClient(error=RuntimeError("secret"))).enqueue_message(
                "c", "u", "r", {}
            )
        )


def test_issue_selection_uses_digest_and_retries_set_nx_collision():
    client = AsyncEvalClient([0, 1])
    store = RedisStateStore(client)
    token = run(store.issue_selection(context()))
    assert len(token) > 20
    assert hashlib.sha256(token.encode()).hexdigest() in client.calls[-1][2][0]
    assert token not in str(client.calls)


def test_consume_rejects_wrong_binding_without_burning_token_then_retries_correctly():
    client = AsyncEvalClient([1, [2, b""], [0, b"job-1"], [1, b"job-1"]])
    store = RedisStateStore(client)
    token = run(store.issue_selection(context()))
    wrong = SelectionContext("corp", "other", "req", "v1", {1: "candidate-9"})
    with pytest.raises(SelectionRejected):
        run(store.consume_selection(token, wrong, 1))
    result = run(store.consume_selection(token, context(), 1))
    retry = run(store.consume_selection(token, context(), 1))
    assert (result.job_id, result.duplicate) == ("job-1", False)
    assert (retry.job_id, retry.duplicate) == ("job-1", True)


def test_consume_rejects_wrong_index():
    client = AsyncEvalClient([1, [2, b""]])
    store = RedisStateStore(client)
    token = run(store.issue_selection(context()))
    with pytest.raises(SelectionRejected):
        run(store.consume_selection(token, context(), 2))
