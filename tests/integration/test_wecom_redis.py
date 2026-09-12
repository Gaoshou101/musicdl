import asyncio
import os
import secrets

import pytest

from musicdl.wecom.state import RedisStateStore, SelectionContext, SelectionRejected


redis_url = os.getenv("MUSICDL_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not redis_url, reason="MUSICDL_TEST_REDIS_URL is not configured"
)


def test_real_redis_atomic_state_contract():
    return asyncio.run(_test_real_redis_atomic_state_contract())


async def _test_real_redis_atomic_state_contract():
    redis = pytest.importorskip("redis.asyncio")
    client = redis.Redis.from_url(redis_url, decode_responses=False)
    namespace = "{musicdl:test:" + secrets.token_hex(8) + "}"
    store = RedisStateStore(client, namespace=namespace)
    try:
        await client.ping()
        results = await asyncio.gather(
            *(store.enqueue_message("c", "u", "r", {"text": "hello"}) for _ in range(20))
        )
        assert len({r.stream_id for r in results}) == 1
        assert await client.xlen(store.message_stream) == 1
        ctx = SelectionContext("c", "u", "r", "v1", {1: "candidate"})
        token = await store.issue_selection(ctx)
        with pytest.raises(SelectionRejected):
            await store.consume_selection(
                token,
                SelectionContext("c", "other", "r", "v1", {1: "candidate"}),
                1,
            )
        consumed = await asyncio.gather(*(store.consume_selection(token, ctx, 1) for _ in range(5)))
        assert len({r.job_id for r in consumed}) == 1
        assert sum(r.duplicate for r in consumed) == 4
        assert await client.xlen(store.job_stream) == 1
    finally:
        keys = [key async for key in client.scan_iter(match=f"{namespace}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
