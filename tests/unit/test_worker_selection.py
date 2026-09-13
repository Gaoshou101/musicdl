import asyncio
from musicdl.worker import bind_user_selection, get_user_selection
from musicdl.wecom.state import SelectionContext

class Redis:
    def __init__(self): self.data = {}; self.sets=[]
    async def set(self, key, value, **kwargs): self.data[key] = value; self.sets.append((key,kwargs))
    async def get(self, key): return self.data.get(key)

def test_bind_and_get_selection_support_bytes_and_preserve_routing():
    redis = Redis(); context = SelectionContext("corp", "user", "req", "ver", {1: "item"})
    async def run():
        await bind_user_selection(redis, "token", context, query="song")
        key = next(iter(redis.data)); redis.data[key] = redis.data[key].encode()
        return await get_user_selection(redis, "corp", "user")
    result = asyncio.run(run())
    assert {k: result[k] for k in ("corp_id", "from_user", "request_id", "version", "query")} == {"corp_id":"corp", "from_user":"user", "request_id":"req", "version":"ver", "query":"song"}
    assert result["candidates"] == {"1":"item"}

def test_bind_and_get_selection_use_custom_namespace_and_ttl():
    redis = Redis(); context = SelectionContext("corp", "user", "req", "ver", {1: "item"})
    async def run():
        await bind_user_selection(redis, "token", context, ttl=321, namespace="{tenant}")
        return await get_user_selection(redis, "corp", "user", namespace="{tenant}")
    result=asyncio.run(run())
    assert result["token"]=="token"
    assert len(redis.sets)==3 and all(key.startswith("{tenant}:") and kwargs=={"ex":321} for key,kwargs in redis.sets)
