"""Cross-entry-point and transition tests for source-aware admission."""

import asyncio

import httpx
import pytest

from musicdl.media.admission import DownloadAdmission
from musicdl.media.fallback import download_with_fallback
from musicdl.media.models import DownloadMetadata, MediaError
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult, SourceStatus
from musicdl.worker.workers import JobWorker

from test_panel_download_admission import (
    _refresh_result as panel_refresh_result,
    candidate as panel_candidate,
    panel_client,
    sign_in,
)
from test_worker_job import Redis as WorkerRedis, State as WorkerState, WeCom, job_payload


ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"


async def _wait_until(predicate, message, *, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(poll(), timeout=timeout)
    except TimeoutError:
        raise AssertionError(message) from None


def test_panel_and_worker_fallbacks_share_the_actual_source_limit(tmp_path):
    async def run():
        admission = DownloadAdmission(active_limit=8, pending_limit=8,
                                      per_source_limit=2, per_user_limit=6)
        originals = ("panel-a", "panel-b", "worker-c")
        failures = {source_id: 0 for source_id in originals}
        shared_candidate = panel_candidate(
            source_id="shared", source_version="v1", item_id="shared-item",
            title="Converged Song", artist="Artist", format="mp3", qualities=("flac",),
        )
        streams_released = asyncio.Event()

        class FailingOriginal:
            def __init__(self, source_id):
                self.source_id = source_id

            async def download(self, item, *, quality=None):
                failures[self.source_id] += 1
                raise MediaError("incomplete_audio")

            async def health(self):
                return True

        class SharedFallback:
            def __init__(self):
                self.calls = 0
                self.active_streams = 0
                self.maximum_streams = 0
                self.two_streams_started = asyncio.Event()

            async def download(self, item, *, quality=None):
                self.calls += 1
                self.active_streams += 1
                self.maximum_streams = max(self.maximum_streams, self.active_streams)
                if self.active_streams == 2:
                    self.two_streams_started.set()

                async def chunks():
                    try:
                        await asyncio.wait_for(streams_released.wait(), timeout=4.0)
                        yield ID3
                    finally:
                        self.active_streams -= 1

                return DownloadMetadata(chunks(), extension="mp3", media_type="audio/mpeg",
                                        declared_size=len(ID3), quality="flac")

            async def health(self):
                return True

        shared = SharedFallback()
        resolvers = {source_id: FailingOriginal(source_id) for source_id in originals}
        resolvers["shared"] = shared

        async def refresh(query, excluded):
            return panel_refresh_result(shared_candidate)

        app, _, runtime, _, _, _ = panel_client(
            tmp_path, resolvers["panel-a"], admission,
        )
        runtime.resolvers = resolvers
        runtime.refresh = refresh

        worker = JobWorker(
            WorkerRedis(), WeCom(), resolvers, str(tmp_path / "worker-media"),
            state=WorkerState(), refresh=refresh, quality_policy="lossless_first",
            resolve_stream_timeout=4.0, refresh_timeout=2.0,
            download_admission=admission,
        )
        panel_candidates = [
            panel_candidate(source_id=source_id, item_id=f"{source_id}-item",
                            title="Converged Song", artist="Artist", format="mp3",
                            qualities=("flac",))
            for source_id in originals[:2]
        ]
        worker_candidate = panel_candidate(
            source_id="worker-c", item_id="worker-c-item", title="Converged Song",
            artist="Artist", format="mp3", qualities=("flac",),
        )
        tasks = []
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://test") as client:
                csrf = await sign_in(client)
                headers = {"x-csrf-token": csrf}
                for item in panel_candidates:
                    tasks.append(asyncio.create_task(client.post(
                        "/admin/download",
                        json={"candidate": item.model_dump(mode="json"),
                              "query": "Converged Song Artist"},
                        headers=headers,
                    )))
                tasks.append(asyncio.create_task(worker.handle_job(
                    job_payload(candidate=worker_candidate.model_dump(mode="json"),
                                query="Converged Song Artist"),
                    job_id="worker-converge-1-0",
                )))

                await asyncio.wait_for(shared.two_streams_started.wait(), timeout=2.0)
                await _wait_until(
                    lambda: all(failures[source_id] == 1 for source_id in originals)
                    and admission.snapshot()["source_pending"] == 1,
                    "all original sources must converge, with the third fallback queued",
                )
                assert shared.calls == 2
                assert shared.active_streams == 2
                assert shared.maximum_streams == 2
                assert admission.snapshot()["source_active"] == 1
                assert admission.snapshot()["source_pending"] == 1

                streams_released.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=4.0)
                assert [response.status_code for response in results[:2]] == [200, 200]
                assert results[2].download is not None
                assert shared.calls == 3
                assert shared.maximum_streams == 2
                assert shared.active_streams == 0
        finally:
            streams_released.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        assert admission.snapshot()["active"] == 0
        assert admission.snapshot()["source_active"] == 0
        assert admission.snapshot()["user_active"] == 0

    asyncio.run(asyncio.wait_for(run(), timeout=8.0))


def test_opposite_source_transitions_progress_and_cancellation_drains_counts():
    async def run():
        admission = DownloadAdmission(active_limit=4, pending_limit=2,
                                      per_source_limit=1, per_user_limit=1)
        permits = []
        transitions = []
        try:
            first = await admission.acquire(source_id="a", user_id="user-a").__aenter__()
            second = await admission.acquire(source_id="b", user_id="user-b").__aenter__()
            permits.extend((first, second))

            a_to_b = asyncio.create_task(first.switch_source("b"))
            transitions.append(a_to_b)
            await _wait_until(lambda: admission.snapshot()["source_pending"] == 1,
                              "A to B transition did not queue")
            b_to_a = asyncio.create_task(second.switch_source("a"))
            transitions.append(b_to_a)
            await asyncio.wait_for(asyncio.gather(a_to_b, b_to_a), timeout=1.0)

            assert first.source_id == "b"
            assert second.source_id == "a"
            assert admission.snapshot()["source_pending"] == 0

            waiting = await admission.acquire(source_id="d", user_id="user-c").__aenter__()
            blocker = await admission.acquire(source_id="c", user_id="user-d").__aenter__()
            permits.extend((waiting, blocker))
            cancelled_switch = asyncio.create_task(waiting.switch_source("c"))
            transitions.append(cancelled_switch)
            await _wait_until(lambda: admission.snapshot()["source_pending"] == 1,
                              "cancelled source transition did not queue")
            cancelled_switch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_switch

            assert waiting.source_id is None
            assert admission.snapshot()["source_pending"] == 0
            assert admission.snapshot()["active"] == 4
            assert admission.snapshot()["user_active"] == 4
        finally:
            for task in transitions:
                if not task.done():
                    task.cancel()
            if transitions:
                await asyncio.gather(*transitions, return_exceptions=True)
            for permit in reversed(permits):
                await permit.release()

        snapshot = admission.snapshot()
        assert snapshot["active"] == 0
        assert snapshot["source_active"] == 0
        assert snapshot["source_pending"] == 0
        assert snapshot["user_active"] == 0

    asyncio.run(asyncio.wait_for(run(), timeout=4.0))


def test_quality_fallback_reacquires_original_source_before_streaming_held_metadata(tmp_path):
    async def run():
        admission = DownloadAdmission(active_limit=3, pending_limit=1,
                                      per_source_limit=1, per_user_limit=2)
        original_candidate = Candidate(
            source_id="original", source_version="v1", item_id="original-item",
            title="Quality Return", artist="Artist", format="mp3", qualities=("flac",),
        )
        alternate_candidate = Candidate(
            source_id="alternate", source_version="v1", item_id="alternate-item",
            title="Quality Return", artist="Artist", format="mp3", qualities=("flac",),
        )
        original_stream_started = asyncio.Event()
        alternate_resolve_started = asyncio.Event()
        allow_alternate_answer = asyncio.Event()
        alternate_stream_started = asyncio.Event()

        class OriginalSource:
            async def download(self, item, *, quality=None):
                async def chunks():
                    original_stream_started.set()
                    yield ID3

                return DownloadMetadata(chunks(), extension="mp3", media_type="audio/mpeg",
                                        declared_size=len(ID3), quality="320k")

            async def health(self):
                return True

        class LowerQualityAlternate:
            async def download(self, item, *, quality=None):
                alternate_resolve_started.set()
                await asyncio.wait_for(allow_alternate_answer.wait(), timeout=3.0)

                async def chunks():
                    alternate_stream_started.set()
                    yield ID3

                return DownloadMetadata(chunks(), extension="mp3", media_type="audio/mpeg",
                                        declared_size=len(ID3), quality="128k")

            async def health(self):
                return True

        async def refresh(query, excluded):
            return SearchResult(
                (alternate_candidate,),
                (SourceStatus("alternate", "v1", "ok", 1),),
                "v1",
            )

        permit = await admission.acquire(source_id="original", user_id="download-owner",
                                         transition_timeout=3.0).__aenter__()
        blocker = None
        download_task = None
        result = None
        try:
            download_task = asyncio.create_task(download_with_fallback(
                original_candidate,
                {"original": OriginalSource(), "alternate": LowerQualityAlternate()},
                tmp_path / "media",
                request_id="quality-return",
                query="Quality Return Artist",
                refresh=refresh,
                quality="flac",
                quality_policy="lossless_first",
                max_quality_switches=1,
                resolve_stream_timeout=4.0,
                refresh_timeout=1.0,
                health_timeout=1.0,
                admission_permit=permit,
            ))
            await asyncio.wait_for(alternate_resolve_started.wait(), timeout=1.0)
            blocker = await admission.acquire(source_id="original", user_id="other-download",
                                              timeout=0.5).__aenter__()
            allow_alternate_answer.set()
            await _wait_until(lambda: admission.snapshot()["source_pending"] == 1,
                              "original descriptor was not blocked on reacquiring its source")
            assert permit.source_id is None
            assert not original_stream_started.is_set()
            assert not alternate_stream_started.is_set()

            await blocker.release()
            blocker = None
            result = await asyncio.wait_for(download_task, timeout=2.0)
            assert result.download is not None
            assert result.download_source_id == "original"
            assert original_stream_started.is_set()
            assert not alternate_stream_started.is_set()
            assert permit.source_id == "original"
        finally:
            allow_alternate_answer.set()
            if download_task is not None and not download_task.done():
                download_task.cancel()
            if download_task is not None:
                await asyncio.gather(download_task, return_exceptions=True)
            if blocker is not None:
                await blocker.release()
            await permit.release()

        assert result is not None and result.download is not None
        snapshot = admission.snapshot()
        assert snapshot["active"] == 0
        assert snapshot["source_active"] == 0
        assert snapshot["source_pending"] == 0
        assert snapshot["user_active"] == 0

    asyncio.run(asyncio.wait_for(run(), timeout=6.0))
