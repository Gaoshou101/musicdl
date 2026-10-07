import asyncio

import pytest

from musicdl.media.admission import AdmissionQueueFull, AdmissionTimeout, DownloadAdmission


def test_admission_bounds_pending_queue_and_releases_permits():
    async def run():
        admission = DownloadAdmission(active_limit=1, pending_limit=1,
                                      per_source_limit=1, per_user_limit=1)
        async with admission.acquire(source_id="source", user_id="user"):
            async def queued_download():
                async with admission.acquire(source_id="other", user_id="other-user"):
                    return "started"

            queued = asyncio.create_task(queued_download())
            await asyncio.sleep(0)
            assert admission.snapshot()["active"] == 1
            assert admission.snapshot()["pending"] == 1
            with pytest.raises(AdmissionQueueFull):
                async with admission.acquire(source_id="third"):
                    pytest.fail("a full queue must not start another download")

        assert await queued == "started"
        assert admission.snapshot()["active"] == 0
        assert admission.snapshot()["pending"] == 0
        assert admission.snapshot()["source_active"] == 0
        assert admission.snapshot()["user_active"] == 0

    asyncio.run(run())


def test_admission_timeout_and_cancellation_remove_waiters():
    async def run():
        admission = DownloadAdmission(active_limit=1, pending_limit=2,
                                      per_source_limit=1, per_user_limit=1)
        async with admission.acquire(source_id="source", user_id="user"):
            with pytest.raises(AdmissionTimeout):
                async with admission.acquire(source_id="source", user_id="user", timeout=0.01):
                    pytest.fail("timed out request must not receive a permit")
            assert admission.snapshot()["pending"] == 0

            async def wait_for_permit():
                async with admission.acquire(source_id="source", user_id="user"):
                    return True

            cancelled = asyncio.create_task(wait_for_permit())
            await asyncio.sleep(0)
            assert admission.snapshot()["pending"] == 1
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert admission.snapshot()["pending"] == 0

        async with admission.acquire(source_id="source", user_id="user"):
            assert admission.snapshot()["active"] == 1
        assert admission.snapshot()["active"] == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "held,requested",
    [
        ({"source_id": "source"}, {"source_id": "source", "user_id": "other"}),
        ({"user_id": "user"}, {"source_id": "other", "user_id": "user"}),
    ],
)
def test_admission_enforces_source_and_user_active_limits_independently(held, requested):
    async def run():
        admission = DownloadAdmission(active_limit=2, pending_limit=1,
                                      per_source_limit=1, per_user_limit=1)
        async with admission.acquire(**held):
            with pytest.raises(AdmissionTimeout):
                async with admission.acquire(**requested, timeout=0):
                    pytest.fail("per-source and per-user limits must hold independently")
            assert admission.snapshot()["active"] == 1
            assert admission.snapshot()["pending"] == 0

    asyncio.run(run())


def test_source_transition_releases_old_source_and_queues_with_global_user_permits_retained():
    async def run():
        admission = DownloadAdmission(active_limit=3, pending_limit=2,
                                      per_source_limit=1, per_user_limit=3)
        first = await admission.acquire(source_id="a", user_id="one").__aenter__()
        blocker = await admission.acquire(source_id="b", user_id="other").__aenter__()
        switched = asyncio.create_task(first.switch_source("b"))
        for _ in range(100):
            if admission.snapshot()["source_pending"] == 1:
                break
            await asyncio.sleep(0)
        assert admission.snapshot()["active"] == 2
        assert admission.snapshot()["source_active"] == 1
        assert admission.snapshot()["source_pending"] == 1
        assert first.source_id is None

        third = await admission.acquire(source_id="a", user_id="one").__aenter__()
        assert admission.snapshot()["active"] == 3
        await blocker.release()
        await switched
        assert first.source_id == "b"
        assert admission.snapshot()["source_active"] == 2
        assert admission.snapshot()["active"] == 2
        await third.release()
        await first.release()
        assert admission.snapshot()["active"] == 0
        assert admission.snapshot()["source_pending"] == 0
        assert admission.snapshot()["source_active"] == 0

    asyncio.run(asyncio.wait_for(run(), timeout=2.0))


def test_source_transition_cancellation_drops_waiter_and_keeps_permit_releasable():
    async def run():
        admission = DownloadAdmission(active_limit=2, pending_limit=2,
                                      per_source_limit=1, per_user_limit=2)
        waiting = await admission.acquire(source_id="old", user_id="u").__aenter__()
        blocker = await admission.acquire(source_id="new", user_id="v").__aenter__()
        transition = asyncio.create_task(waiting.switch_source("new"))
        for _ in range(100):
            if admission.snapshot()["source_pending"] == 1:
                break
            await asyncio.sleep(0)
        transition.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transition
        assert admission.snapshot()["source_pending"] == 0
        assert admission.snapshot()["active"] == 2
        assert admission.snapshot()["source_active"] == 1
        await waiting.release()
        await blocker.release()
        assert admission.snapshot()["active"] == 0
        assert admission.snapshot()["source_active"] == 0

    asyncio.run(run())


def test_source_transition_wait_expires_and_clears_the_source_slot():
    async def run():
        admission = DownloadAdmission(active_limit=2, pending_limit=1,
                                      per_source_limit=1, per_user_limit=2)
        waiting = await admission.acquire(source_id="old", user_id="u",
                                          transition_timeout=0.01).__aenter__()
        blocker = await admission.acquire(source_id="new", user_id="v").__aenter__()
        with pytest.raises(AdmissionTimeout):
            await waiting.switch_source("new")
        assert waiting.take_source_wait_interruption_status() == "admission_wait"
        assert admission.snapshot()["source_pending"] == 0
        assert admission.snapshot()["active"] == 2
        assert admission.snapshot()["source_active"] == 1
        await waiting.release()
        await blocker.release()
        assert admission.snapshot()["active"] == 0

    asyncio.run(run())
