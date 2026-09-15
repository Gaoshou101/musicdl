import asyncio
import hashlib
import json
from pathlib import PurePosixPath

import pytest

from musicdl.media import DownloadMetadata, MediaError, download_candidate
from musicdl.media.models import ArtifactRecord, _CloseOnce
from musicdl.sources.models import Candidate
from musicdl.wecom.state import ARTIFACT_TRANSITION_SCRIPT, CLAIM_ARTIFACT_SCRIPT, ArtifactConflict
from musicdl.wecom.state import (
    RedisStateStore,
    SelectionContext,
    SelectionRejected,
    StateUnavailable,
    artifact_first_free_slot,
    artifact_slot_target,
    artifact_takeover_action,
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


ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"


class ScriptRedis:
    """In-memory stand-in for the Redis surface the artifact ledger scripts use.

    ``eval`` dispatches on the exact script text and mirrors the documented Lua
    transitions, including the ``TIME`` read, so the Python wrapper is exercised
    end to end. The Lua bodies themselves are only exercised against real Redis.
    """

    def __init__(self):
        self.hashes = {}
        self.strings = {}
        self.expiries = []
        self.calls = []
        self.conflict_once = False
        self.race_slot = None

    async def ping(self): return True
    async def get(self, key): return self.strings.get(key)
    async def set(self, key, value, **kwargs): self.strings[key] = value; return True
    async def delete(self, *keys): return 1
    async def expire(self, key, seconds): self.expiries.append((str(key), int(seconds))); return True
    async def hgetall(self, key): return dict(self.hashes.get(key, {}))

    async def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        keys = list(args[:numkeys]); argv = [str(value) for value in args[numkeys:]]
        if script == CLAIM_ARTIFACT_SCRIPT:
            return self._claim(keys, argv)
        if script == ARTIFACT_TRANSITION_SCRIPT:
            return self._transition(keys, argv)
        if "TIME" in script:
            return ["1700000000", "123456"]
        raise AssertionError("unexpected script")

    @staticmethod
    def _flat(values):
        result = []
        for key, value in values.items():
            result.extend([key, value])
        return result

    def _claim(self, keys, argv):
        slot_key, record_key = keys
        existing = self.hashes.get(record_key)
        if existing:
            return self._flat(existing)
        if self.race_slot is not None:
            self.hashes.setdefault(slot_key, {})[str(self.race_slot)] = "other-job"
            self.race_slot = None
        bucket = self.hashes.setdefault(slot_key, {})
        if argv[0] in bucket or self.conflict_once:
            return ["__conflict__"]
        bucket[argv[0]] = argv[1]
        created = {"job_id": argv[1], "candidate_id": argv[2], "base_hash": argv[3],
                   "target_relative_path": argv[4], "temporary_relative_path": argv[5],
                   "extension": argv[6], "media_type": argv[7], "declared_size": argv[8],
                   "owner": argv[9], "fence": argv[10], "allocation_slot": argv[0], "state": "prepared"}
        self.hashes[record_key] = created
        if int(argv[11]) > 0:
            self.expiries.append((record_key, int(argv[11])))
        return self._flat(created)

    def _transition(self, keys, argv):
        key = keys[0]
        values = self.hashes.get(key)
        if not values:
            return ["__missing__"]
        (owner, fence, allowed, state, ttl, size_bytes, sha256, extension, media_type,
         declared_size, new_owner, new_fence, lease_until) = argv
        if owner and values.get("owner") != owner:
            return ["__stale__"]
        if fence and str(values.get("fence")) != fence:
            return ["__stale__"]
        if values.get("state") not in set(allowed.split(",")):
            return ["__stale__"]
        if state:
            values["state"] = state
        for name, value in (("size_bytes", size_bytes), ("sha256", sha256), ("extension", extension),
                            ("media_type", media_type), ("declared_size", declared_size),
                            ("owner", new_owner), ("fence", new_fence), ("lease_until_ms", lease_until)):
            if value:
                values[name] = value
        if int(ttl) > 0:
            self.expiries.append((key, int(ttl)))
        return self._flat(values)


def artifact(state="prepared", *, owner="owner-1", fence=1, lease_until_ms=None, slot=1):
    return ArtifactRecord(job_id="1-0", candidate_id="candidate-9", temporary_relative_path=".musicdl-staging/a.1.part",
                          target_relative_path="Song.mp3", allocation_slot=slot, extension=".mp3",
                          media_type="audio/mpeg", size_bytes=len(ID3) if state != "prepared" else None,
                          sha256=hashlib.sha256(ID3).hexdigest() if state != "prepared" else None,
                          owner=owner, fence=fence, lease_until_ms=lease_until_ms, state=state)


class Source:
    def __init__(self, metadata_factory):
        self.metadata_factory = metadata_factory
        self.calls = 0

    async def download(self, _candidate):
        self.calls += 1
        return self.metadata_factory()

    async def health(self): return True


def playable_metadata():
    async def chunks():
        yield ID3
    return DownloadMetadata(chunks=chunks(), extension="mp3", media_type="audio/mpeg",
                            declared_size=len(ID3), _close_once=_CloseOnce(_noop))


async def _noop():
    return None


def test_artifact_slot_targets_follow_the_base_suffix_rule():
    base = PurePosixPath("华语/Artist/Song.mp3")
    assert artifact_slot_target(base, ".mp3", 1).as_posix() == "华语/Artist/Song.mp3"
    assert artifact_slot_target(base, ".mp3", 2).as_posix() == "华语/Artist/Song (2).mp3"
    assert artifact_slot_target(PurePosixPath("Song"), ".mp3", 3).as_posix() == "Song (3).mp3"
    with pytest.raises(ValueError, match="invalid artifact slot"):
        artifact_slot_target(base, ".mp3", 0)


def test_first_free_slot_skips_preexisting_files_and_ledger_holes(tmp_path):
    base = PurePosixPath("Song.mp3")
    assert artifact_first_free_slot(tmp_path, base, ".mp3", set()) == 1
    (tmp_path / "Song.mp3").write_bytes(ID3)
    assert artifact_first_free_slot(tmp_path, base, ".mp3", set()) == 2
    (tmp_path / "Song (3).mp3").write_bytes(ID3)
    assert artifact_first_free_slot(tmp_path, base, ".mp3", set()) == 2
    assert artifact_first_free_slot(tmp_path, base, ".mp3", {2}) == 4


@pytest.mark.parametrize("state,expected", [
    ("published", "replay"), ("uncertain", "uncertain"), ("stream_complete", "verify_publish"),
    ("publishing", "verify_complete"), ("physically_published", "verify_complete"),
    ("external_started", "uncertain"), ("prepared", "resume"),
])
def test_takeover_action_matrix(state, expected):
    assert artifact_takeover_action(artifact(state), new_owner="owner-2", new_fence=2, now_ms=1000) == expected


def test_takeover_action_reports_a_live_foreign_lease_only():
    live = artifact("prepared", lease_until_ms=2000)
    assert artifact_takeover_action(live, new_owner="owner-2", new_fence=2, now_ms=1000) == "owner_conflict"
    assert artifact_takeover_action(live, new_owner="owner-1", new_fence=2, now_ms=1000) == "resume"
    assert artifact_takeover_action(live, new_owner="owner-2", new_fence=2, now_ms=2000) == "resume"


@pytest.mark.parametrize("state,expected", [("published", "replay"), ("uncertain", "uncertain")])
def test_terminal_states_beat_a_live_foreign_lease(state, expected):
    # A published or uncertain record only owes idempotent verification, so a live lease from a
    # previous owner must not block a higher-fence replay.
    live = artifact(state, lease_until_ms=10 ** 12)
    assert artifact_takeover_action(live, new_owner="owner-2", new_fence=2, now_ms=1000) == expected


@pytest.mark.parametrize("fence", [1, 0, -1, True, "2", None])
def test_takeover_action_rejects_a_non_higher_fence(fence):
    with pytest.raises(ValueError, match="stale artifact fence"):
        artifact_takeover_action(artifact("prepared"), new_owner="owner-2", new_fence=fence, now_ms=0)


def _prepare(store, root, *, job_id="1-0", base="华语/Artist/Song.mp3", owner="owner-1", fence=1,
             extension="mp3", declared_size=None, ttl=3600):
    return store.prepare_artifact(job_id, candidate(), media_root=root, base_relative_path=base,
                                  extension=extension, media_type="audio/mpeg", declared_size=declared_size,
                                  owner=owner, fence=fence, ttl=ttl)


def test_prepare_artifact_reserves_the_lowest_free_slot_and_is_idempotent(tmp_path):
    async def scenario():
        store = RedisStateStore(ScriptRedis())
        first = await _prepare(store, tmp_path)
        again = await _prepare(store, tmp_path, owner="owner-9", fence=4)
        return first, again
    first, again = run(scenario())
    digest = hashlib.sha256(b"1-0").hexdigest()[:40]
    assert first.state == "prepared" and first.allocation_slot == 1
    assert first.target_relative_path == "华语/Artist/Song.mp3"
    assert first.temporary_relative_path == f".musicdl-staging/{digest}.1.part"
    assert tmp_path.as_posix() not in first.target_relative_path
    assert tmp_path.as_posix() not in first.temporary_relative_path
    assert again == first and again.owner == "owner-1" and again.fence == 1


def test_prepare_artifact_retries_a_concurrently_claimed_slot(tmp_path):
    redis = ScriptRedis(); redis.race_slot = 1
    record = run(_prepare(RedisStateStore(redis), tmp_path, base="Song.mp3"))
    assert record.allocation_slot == 2 and record.target_relative_path == "Song (2).mp3"
    assert len(redis.calls) >= 2


def test_prepare_artifact_validates_the_allocation_inputs(tmp_path):
    store = RedisStateStore(ScriptRedis())
    cases = [
        dict(base="../escape.mp3"), dict(base="/abs/Song.mp3"), dict(base="Song\\w.mp3"), dict(base=""),
        dict(base="a/../b.mp3"), dict(extension=""), dict(extension="mp3.exe.exe"), dict(extension="a/b"),
        dict(owner=""), dict(owner="x" * 257), dict(fence=0), dict(fence=True), dict(ttl=-1), dict(ttl=True),
    ]
    for case in cases:
        kwargs = {"base": "Song.mp3", "extension": "mp3", "owner": "owner-1", "fence": 1, "ttl": 3600}
        kwargs.update(case)
        with pytest.raises(ValueError):
            run(_prepare(store, tmp_path, **kwargs))
    with pytest.raises(ValueError, match="invalid artifact size"):
        run(_prepare(store, tmp_path, declared_size=0))
    with pytest.raises(ValueError, match="invalid job id"):
        run(_prepare(store, tmp_path, job_id=""))


def test_artifact_transitions_require_the_exact_owner_fence_and_state(tmp_path):
    digest = hashlib.sha256(ID3).hexdigest()

    async def scenario():
        store = RedisStateStore(ScriptRedis())
        await _prepare(store, tmp_path)
        with pytest.raises(ArtifactConflict):
            await store.record_stream_complete("1-0", owner="owner-2", fence=1, size_bytes=len(ID3),
                                               sha256=digest, extension=".mp3", media_type="audio/mpeg",
                                               declared_size=None, ttl=0)
        with pytest.raises(ArtifactConflict):
            await store.claim_artifact_publish("1-0", owner="owner-1", fence=1, ttl=0)
        completed = await store.record_stream_complete("1-0", owner="owner-1", fence=1, size_bytes=len(ID3),
                                                       sha256=digest, extension=".mp3", media_type="audio/mpeg",
                                                       declared_size=None, ttl=0)
        publishing = await store.claim_artifact_publish("1-0", owner="owner-1", fence=1, ttl=0)
        with pytest.raises(ArtifactConflict):
            await store.mark_artifact_published("1-0", owner="owner-1", fence=2, ttl=0)
        published = await store.mark_artifact_published("1-0", owner="owner-1", fence=1, ttl=0)
        return completed, publishing, published

    completed, publishing, published = run(scenario())
    assert [record.state for record in (completed, publishing, published)] == [
        "stream_complete", "publishing", "published"]
    assert published.size_bytes == len(ID3) and published.sha256 == digest


def test_takeover_artifact_claims_a_free_lease_and_marks_expired_external_work_uncertain(tmp_path):
    async def scenario():
        store = RedisStateStore(ScriptRedis())
        await _prepare(store, tmp_path)
        taken = await store.takeover_artifact("1-0", new_owner="owner-2", new_fence=2, now_ms=1000, lease_ms=500)
        claimed = await store.get_artifact("1-0")
        conflict = await store.takeover_artifact("1-0", new_owner="owner-3", new_fence=3, now_ms=1200, lease_ms=500)
        expired = await store.takeover_artifact("1-0", new_owner="owner-3", new_fence=4, now_ms=2000, lease_ms=500)
        return taken, claimed, conflict, expired, await store.get_artifact("1-0")

    taken, claimed, conflict, expired, final = run(scenario())
    assert taken == "resume" and (claimed.owner, claimed.fence, claimed.lease_until_ms) == ("owner-2", 2, 1500)
    assert conflict == "owner_conflict"
    assert expired == "resume" and final.owner == "owner-3"


def test_takeover_artifact_marks_an_expired_external_stage_uncertain(tmp_path):
    async def scenario():
        store = RedisStateStore(ScriptRedis())
        reservation = await _prepare(store, tmp_path)
        await store.record_stream_complete("1-0", owner="owner-1", fence=1, size_bytes=len(ID3),
                                          sha256=hashlib.sha256(ID3).hexdigest(), extension=".mp3",
                                          media_type="audio/mpeg", declared_size=None, ttl=0)
        await store._artifact_transition("1-0", allowed={"stream_complete"}, owner="owner-1", fence=1,
                                         state="external_started", new_owner="owner-1", new_fence=1)
        action = await store.takeover_artifact("1-0", new_owner="owner-2", new_fence=2, now_ms=5000, lease_ms=500)
        return reservation, action, await store.get_artifact("1-0")

    _, action, final = run(scenario())
    assert action == "uncertain" and final.state == "uncertain" and final.owner == "owner-2"


def test_redis_state_store_reads_its_own_clock():
    store = RedisStateStore(ScriptRedis())
    assert run(store.redis_now_ms()) == 1700000000123
    with pytest.raises(StateUnavailable):
        run(RedisStateStore(AsyncEvalClient([None])).redis_now_ms())


def test_artifact_operations_surface_redis_errors_as_unavailable():
    store = RedisStateStore(AsyncEvalClient(error=RuntimeError("secret")))
    with pytest.raises(StateUnavailable):
        run(store.get_artifact("1-0"))
    with pytest.raises(StateUnavailable):
        run(_prepare(store, "does-not-matter"))


def _download(root, *, reservation, store, source, fmt="mp3"):
    return download_candidate(Candidate(source_id="src", source_version="1", item_id="candidate-9",
                                        title="Song", artist="Artist", format=fmt),
                              source, root, request_id="r", reservation=reservation,
                              artifact_store=store, owner="owner-1", fence=1)


def test_download_candidate_publishes_through_the_store_and_replays_without_the_source(tmp_path):
    source = Source(playable_metadata)

    async def scenario():
        store = RedisStateStore(ScriptRedis())
        reservation = await _prepare(store, tmp_path, base="Song.mp3", declared_size=len(ID3))
        first = await _download(tmp_path, reservation=reservation, store=store, source=source)
        record = await store.get_artifact("1-0")
        second = await _download(tmp_path, reservation=record, store=store, source=source)
        return first, record, second

    first, record, second = run(scenario())
    assert source.calls == 1 and record.state == "published"
    assert first.relative_path.as_posix() == "Song.mp3" and second == first
    assert (tmp_path / "Song.mp3").read_bytes() == ID3
    assert list((tmp_path / ".musicdl-staging").iterdir()) == []


def test_download_candidate_resumes_stream_complete_without_the_source(tmp_path):
    source = Source(playable_metadata)

    async def scenario():
        store = RedisStateStore(ScriptRedis())
        reservation = await _prepare(store, tmp_path, base="Song.mp3", declared_size=len(ID3))
        staging = tmp_path / reservation.temporary_relative_path
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(ID3)
        completed = await store.record_stream_complete("1-0", owner="owner-1", fence=1, size_bytes=len(ID3),
                                                       sha256=hashlib.sha256(ID3).hexdigest(), extension=".mp3",
                                                       media_type="audio/mpeg", declared_size=len(ID3), ttl=3600)
        result = await _download(tmp_path, reservation=completed, store=store, source=source)
        return result, await store.get_artifact("1-0"), staging

    result, record, staging = run(scenario())
    assert source.calls == 0 and record.state == "published"
    assert result.relative_path.as_posix() == "Song.mp3"
    assert (tmp_path / "Song.mp3").read_bytes() == ID3 and not staging.exists()


@pytest.mark.parametrize("state", ["uncertain", "external_started"])
def test_download_candidate_refuses_uncertain_or_external_reservations(tmp_path, state):
    source = Source(playable_metadata)
    reservation = artifact(state)
    with pytest.raises(MediaError, match="artifact_uncertain"):
        run(_download(tmp_path, reservation=reservation, store=RedisStateStore(ScriptRedis()), source=source))
    assert source.calls == 0


def test_download_candidate_refuses_partial_prepared_state(tmp_path):
    source = Source(playable_metadata)

    async def scenario():
        store = RedisStateStore(ScriptRedis())
        reservation = await _prepare(store, tmp_path, base="Song.mp3")
        staging = tmp_path / reservation.temporary_relative_path
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"partial")
        return await _download(tmp_path, reservation=reservation, store=store, source=source)

    with pytest.raises(MediaError, match="artifact_uncertain"):
        run(scenario())
    assert source.calls == 0 and not (tmp_path / "Song.mp3").exists()


def test_download_candidate_rejects_a_replayed_target_for_a_different_format(tmp_path):
    source = Source(playable_metadata)

    async def scenario():
        store = RedisStateStore(ScriptRedis())
        reservation = await _prepare(store, tmp_path, base="Song.mp3", declared_size=len(ID3))
        await _download(tmp_path, reservation=reservation, store=store, source=source)
        record = await store.get_artifact("1-0")
        with pytest.raises(MediaError, match="artifact_uncertain"):
            await _download(tmp_path, reservation=record, store=store, source=source, fmt="m4a")
        return source.calls

    assert run(scenario()) == 1
