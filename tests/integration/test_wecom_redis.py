import asyncio
import os
import secrets
import shutil
import tempfile
from pathlib import Path

import pytest

from musicdl.sources.models import Candidate
from musicdl.wecom.state import (
    ArtifactConflict,
    RedisStateStore,
    SelectionContext,
    SelectionRejected,
)


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


def test_real_redis_artifact_ledger_contract():
    return asyncio.run(_test_real_redis_artifact_ledger_contract())


async def _prepare(store, root, job_id, *, owner="owner-1", fence=1):
    return await store.prepare_artifact(
        job_id,
        Candidate(source_id="src", source_version="1", item_id="candidate-9", title="Song",
                  artist="Artist", format="mp3"),
        media_root=root,
        base_relative_path="华语/Artist/Song.mp3",
        extension=".mp3",
        media_type="audio/mpeg",
        declared_size=None,
        owner=owner,
        fence=fence,
        ttl=60,
    )


async def _test_real_redis_artifact_ledger_contract():
    redis = pytest.importorskip("redis.asyncio")
    client = redis.Redis.from_url(redis_url, decode_responses=False)
    namespace = "{musicdl:test:" + secrets.token_hex(8) + "}"
    store = RedisStateStore(client, namespace=namespace)
    root = Path(tempfile.mkdtemp())
    try:
        await client.ping()
        first = await _prepare(store, root, "1-0")
        assert (first.allocation_slot, first.target_relative_path, first.state) == (
            1, "华语/Artist/Song.mp3", "prepared")
        assert await _prepare(store, root, "1-0", owner="owner-9", fence=9) == first
        allocated = await asyncio.gather(*(_prepare(store, root, f"{index}-0") for index in range(2, 8)))
        assert {record.allocation_slot for record in allocated} == {2, 3, 4, 5, 6, 7}
        assert await store.redis_now_ms() > 1_600_000_000_000
        taken = await store.takeover_artifact(
            "1-0", new_owner="owner-2", new_fence=2, now_ms=await store.redis_now_ms(), lease_ms=60000)
        assert taken == "resume"
        assert (await store.get_artifact("1-0")).owner == "owner-2"
        with pytest.raises(ArtifactConflict):
            await store.record_stream_complete(
                "1-0", owner="owner-1", fence=1, size_bytes=10, sha256="a" * 64,
                extension=".mp3", media_type="audio/mpeg", declared_size=None, ttl=60)
        completed = await store.record_stream_complete(
            "1-0", owner="owner-2", fence=2, size_bytes=10, sha256="a" * 64,
            extension=".mp3", media_type="audio/mpeg", declared_size=None, ttl=60)
        assert completed.state == "stream_complete" and completed.size_bytes == 10
        publishing = await store.claim_artifact_publish("1-0", owner="owner-2", fence=2, ttl=60)
        published = await store.mark_artifact_published("1-0", owner="owner-2", fence=2, ttl=60)
        assert (publishing.state, published.state) == ("publishing", "published")
        assert await store.takeover_artifact(
            "1-0", new_owner="owner-3", new_fence=3, now_ms=await store.redis_now_ms(), lease_ms=1000) == "replay"
        with pytest.raises(ValueError, match="invalid artifact path"):
            await store.prepare_artifact(
                "9-0",
                Candidate(source_id="src", source_version="1", item_id="i", title="T", artist="A"),
                media_root=root, base_relative_path="../escape.mp3", extension=".mp3",
                media_type="audio/mpeg", declared_size=None, owner="owner-1", fence=1, ttl=60)
    finally:
        keys = [key async for key in client.scan_iter(match=f"{namespace}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
        shutil.rmtree(root, ignore_errors=True)
