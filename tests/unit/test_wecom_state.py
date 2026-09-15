import asyncio
import hashlib
import json

import pytest

from musicdl.sources.models import Candidate
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

    async def ping(self):
        if self.error:
            raise self.error
        return True

    async def get(self, key):
        if self.error:
            raise self.error
        return b"1"


def run(awaitable):
    return asyncio.run(awaitable)


def candidate(item="candidate-9"):
    return Candidate(source_id="src", source_version="1", item_id=item, title="Song", artist="Artist")


def context():
    return SelectionContext("corp", "user", "req", "v1", {1: candidate()})


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
    wrong = SelectionContext("corp", "other", "req", "v1", {1: candidate()})
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


def test_ping_and_lookup_success():
    store = RedisStateStore(AsyncEvalClient())
    assert run(store.ping()) is True
    assert run(store.lookup_message("corp", "req")) is True


def test_ping_and_lookup_errors_are_unavailable():
    store = RedisStateStore(AsyncEvalClient(error=RuntimeError("secret")))
    with pytest.raises(StateUnavailable):
        run(store.ping())
    with pytest.raises(StateUnavailable):
        run(store.lookup_message("corp", "req"))


def test_issue_selection_freezes_the_full_candidate_snapshot_and_generation():
    client = AsyncEvalClient([1])
    store = RedisStateStore(client)
    selected = SelectionContext("corp", "user", "req", "v1", {1: candidate("a"), 3: candidate("b")},
                                query="song", selection_generation=3)
    run(store.issue_selection(selected))
    payload = json.loads(client.calls[-1][2][1])
    assert payload["candidates"] == {"1": candidate("a").model_dump(mode="json"),
                                     "3": candidate("b").model_dump(mode="json")}
    assert (payload["query"], payload["generation"]) == ("song", 3)


def test_consume_sends_every_job_field_and_the_job_record_prefix():
    client = AsyncEvalClient([[0, b"17-0"]])
    store = RedisStateStore(client)
    selected = SelectionContext("corp", "user", "req", "v1", {2: candidate("a")}, query="song",
                                selection_generation=4)
    result = run(store.consume_selection("tok", selected, 2))
    assert (result.job_id, result.duplicate) == ("17-0", False)
    script, numkeys, call = client.calls[-1]
    digest = hashlib.sha256(b"tok").hexdigest()
    assert numkeys == 4
    assert call[:4] == (f"{{musicdl}}:selection:{digest}", f"{{musicdl}}:selection-job:{digest}",
                        "{musicdl}:stream:jobs", "{musicdl}:job:")
    assert call[4:] == ("corp", "user", "req", "v1", "4", "2", "song", 172800)
    for field in ("'candidate',frozen", "'corp_id',ARGV[1]", "'query',ARGV[7]", "'generation',ARGV[5]",
                  "KEYS[4]..job"):
        assert field in script


def test_consume_rejects_stale_generation_from_redis():
    client = AsyncEvalClient([[2, b""]])
    with pytest.raises(SelectionRejected):
        run(RedisStateStore(client).consume_selection("tok", context(), 1))


def test_consume_rejects_index_outside_the_snapshot_before_redis():
    client = AsyncEvalClient()
    with pytest.raises(SelectionRejected):
        run(RedisStateStore(client).consume_selection("tok", context(), 5))
    assert client.calls == []


@pytest.mark.parametrize("candidates", [
    {},
    {0: candidate()},
    {101: candidate()},
    {1: {"item_id": "x"}},
    {1: "candidate-9"},
    {"x": candidate()},
])
def test_issue_selection_rejects_incomplete_or_malformed_snapshots(candidates):
    selected = SelectionContext("corp", "user", "req", "v1", candidates)
    with pytest.raises(ValueError, match="invalid selection context"):
        run(RedisStateStore(AsyncEvalClient([1])).issue_selection(selected))


@pytest.mark.parametrize("generation", [-1, True, 2 ** 31, "1"])
def test_issue_selection_rejects_invalid_generation(generation):
    selected = SelectionContext("corp", "user", "req", "v1", {1: candidate()},
                                selection_generation=generation)
    with pytest.raises(ValueError, match="invalid selection context"):
        run(RedisStateStore(AsyncEvalClient([1])).issue_selection(selected))


def test_issue_selection_rejects_oversized_query():
    selected = SelectionContext("corp", "user", "req", "v1", {1: candidate()}, query="x" * 513)
    with pytest.raises(ValueError, match="invalid selection context"):
        run(RedisStateStore(AsyncEvalClient([1])).issue_selection(selected))
