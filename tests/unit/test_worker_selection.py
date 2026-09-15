import asyncio
import json
import pytest
from musicdl.worker import bind_user_selection, get_user_selection
from musicdl.worker.selection import _hash
from musicdl.sources.models import Candidate
from musicdl.wecom.state import SelectionContext

class Redis:
    def __init__(self): self.data = {}; self.sets=[]
    async def set(self, key, value, **kwargs): self.data[key] = value; self.sets.append((key,kwargs))
    async def get(self, key): return self.data.get(key)

def candidate(item="item"):
    return Candidate(source_id="src", source_version="1", item_id=item, title="Song", artist="Artist")

def test_bind_and_get_selection_support_bytes_and_preserve_routing():
    redis = Redis(); context = SelectionContext("corp", "user", "req", "ver", {1: candidate()})
    async def run():
        await bind_user_selection(redis, "token", context, query="song")
        key = next(iter(redis.data)); redis.data[key] = redis.data[key].encode()
        return await get_user_selection(redis, "corp", "user")
    result = asyncio.run(run())
    assert {k: result[k] for k in ("corp_id", "from_user", "request_id", "version", "query")} == {"corp_id":"corp", "from_user":"user", "request_id":"req", "version":"ver", "query":"song"}
    assert result["generation"] == 0
    assert result["candidates"] == {"1": candidate().model_dump(mode="json")}

def test_bind_and_get_selection_use_custom_namespace_and_ttl():
    redis = Redis(); context = SelectionContext("corp", "user", "req", "ver", {1: candidate()})
    async def run():
        await bind_user_selection(redis, "token", context, query="song", ttl=321, namespace="{tenant}")
        return await get_user_selection(redis, "corp", "user", namespace="{tenant}")
    result=asyncio.run(run())
    assert result["token"]=="token"
    assert len(redis.sets)==3 and all(key.startswith("{tenant}:") and kwargs=={"ex":321} for key,kwargs in redis.sets)

def test_bind_selection_uses_one_atomic_eval_for_three_indexes_and_never_falls_back():
    class AtomicRedis:
        def __init__(self, fail=False): self.calls=[]; self.fail=fail
        async def eval(self, script, numkeys, *args):
            self.calls.append((script, numkeys, args))
            if self.fail: raise RuntimeError("eval unavailable")
            return 1
        async def set(self, *args, **kwargs):
            raise AssertionError("atomic bind must not fall back to SET")
    context = SelectionContext("corp", "user", "req", "ver", {1: candidate()},
                               query="song", selection_generation=2)
    redis = AtomicRedis()
    asyncio.run(bind_user_selection(redis, "token", context, ttl=321, namespace="{tenant}"))
    assert len(redis.calls) == 1
    script, numkeys, args = redis.calls[0]
    assert numkeys == 3 and len(args) == 6
    assert args[:3] == (
        "{tenant}:worker-selection:" + _hash("token"),
        "{tenant}:worker-user:" + _hash("corp:user"),
        "{tenant}:worker-request:" + _hash("req"),
    )
    assert json.loads(args[3]) == {
        "corp_id": "corp", "from_user": "user", "request_id": "req", "version": "ver",
        "query": "song", "generation": 2,
        "candidates": {"1": candidate().model_dump(mode="json")}, "token": "token",
    }
    assert args[4:] == ("token", 321)
    assert '"token"' not in script and '"corp"' not in script and '"request"' not in script
    failed = AtomicRedis(fail=True)
    with pytest.raises(RuntimeError, match="eval unavailable"):
        asyncio.run(bind_user_selection(failed, "token", context))
    assert len(failed.calls) == 1

def test_bind_rejects_malformed_snapshots_and_generation():
    redis = Redis()
    with pytest.raises(ValueError, match="invalid selection context"):
        asyncio.run(bind_user_selection(redis, "token",
                                        SelectionContext("corp", "user", "req", "ver", {1: "item"})))
    with pytest.raises(ValueError, match="invalid selection context"):
        asyncio.run(bind_user_selection(redis, "token",
                                        SelectionContext("corp", "user", "req", "ver", {1: candidate()},
                                                         selection_generation=-1)))
    assert redis.data == {}

def test_bind_carries_the_frozen_snapshot_over_the_index_route():
    redis = Redis(); context = SelectionContext("corp", "user", "req", "ver", {2: candidate("id")})
    asyncio.run(bind_user_selection(redis, "tok", context, ttl=60))
    stored = json.loads(next(iter(redis.data.values())))
    assert stored["candidates"]["2"]["item_id"] == "id"
    assert stored["version"] == "ver" and stored["generation"] == 0
