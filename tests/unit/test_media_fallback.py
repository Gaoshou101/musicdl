import asyncio
from pathlib import Path

import pytest

from musicdl.media import DownloadMetadata, MediaError, download_with_fallback
from musicdl.media.fallback import replacement_candidates, replacement_channel_rows
from musicdl.media.models import ArtifactRecord
from musicdl.sources.models import Candidate
from musicdl.sources.search import SearchResult
from musicdl.sources.search import SourceStatus


def candidate(source="a"):
    return Candidate(source_id=source, source_version="1", item_id="i", title="Song", artist="Artist", format="mp3")


async def chunks(*values):
    for value in values:
        yield value

ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"


class Source:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.health_calls = 0

    async def download(self, item):
        if self.fail:
            raise MediaError("download_failed")
        return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg")

    async def health(self):
        self.health_calls += 1
        return True


def test_secret_media_error_is_redacted_in_fallback(tmp_path):
    class Bad(Source):
        async def download(self, item):
            raise MediaError("https://host/?api_key=SECRET")
    events = []
    async def refresh(query, excluded):
        return SearchResult(candidates=(), statuses=(), version="v")
    result = asyncio.run(download_with_fallback(candidate(), {"a": Bad()}, tmp_path, request_id="r", query="q", refresh=refresh, record=events.append))
    assert result.download is None and result.download_error == "download_failed"
    assert all("SECRET" not in repr(x) and "https://" not in repr(x) for x in events)


def result(candidates=(), channels=()):
    return SearchResult(tuple(candidates), (SourceStatus("a", "1", "ok", len(candidates)),), "v",
                        channels=tuple(tuple(row) for row in channels))


def test_success_does_not_enter_fallback(tmp_path):
    source = Source()
    events = []
    calls = []

    async def refresh(query, excluded):
        calls.append((query, excluded))
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.download is not None
    assert outcome.failed_source_id is None
    assert calls == []
    assert source.health_calls == 0
    # No playing time was stated or measurable, so lenient mode records the
    # gap and still delivers the file.
    assert [(e.stage, e.status) for e in events] == [("duration", "unverified"), ("download", "success")]


def test_content_failure_switches_channels_and_prefers_known_lossless_source(tmp_path):
    class Invalid(Source):
        async def download(self, item, *, quality=None):
            raise MediaError("media_response_invalid")

    class Lossless(Source):
        async def download(self, item, *, quality=None):
            assert quality == "flac"
            return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg", quality="flac")

    primary = candidate("primary")
    unhealthy = candidate("unhealthy")
    preferred = candidate("preferred-lossy")
    unknown = candidate("unknown")
    capable = candidate("capable")
    events = []

    async def refresh(query, excluded):
        assert excluded == frozenset({"primary"})
        return result((unhealthy, preferred, unknown, capable))

    class Lossy(Source):
        async def download(self, item, *, quality=None):
            return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg", quality="320k")

    outcome = asyncio.run(download_with_fallback(
        primary,
        {"primary": Invalid(), "unhealthy": Invalid(), "preferred-lossy": Lossy(),
         "unknown": Invalid(), "capable": Lossless()},
        tmp_path, request_id="r", query="Song", quality="flac", quality_policy="lossless_first",
        refresh=refresh,
        channel_health=lambda source_id: (False if source_id == "unhealthy" else
                                          True if source_id in {"preferred-lossy", "capable"} else None),
        preference=lambda source_id: 10 if source_id == "preferred-lossy" else 0,
        lossless_capability=lambda source_id: source_id == "capable",
        record=events.append))

    assert outcome.download is not None
    assert outcome.download.actual_quality == "mp3"
    switches = [event for event in events if event.stage == "channel_switch"]
    assert len(switches) == 1
    assert switches[0].from_source_id == "primary" and switches[0].to_source_id == "capable"
    assert switches[0].skipped_sources["unhealthy"] == "known_unavailable"
    assert [event.source_id for event in events if event.stage == "download" and event.status == "failed"] == [
        "primary"]
    assert outcome.download.requested_quality == "flac"


def test_damaged_flac_is_refused_then_fallback_delivers_backup(tmp_path):
    primary = Candidate(source_id="primary", source_version="1", item_id="i", title="Song",
                        artist="Artist", format="flac", duration=30)
    backup = Candidate(source_id="backup", source_version="1", item_id="i", title="Song",
                       artist="Artist", format="flac", duration=30)
    good_flac = (Path(__file__).resolve().parents[1] / "fixtures" / "media"
                 / "ffmpeg_30s.flac").read_bytes()
    damaged_flac = bytearray(good_flac)
    damaged_flac[-1] ^= 0x01
    events = []

    class DamagedFlac(Source):
        async def download(self, item, *, quality=None):
            return DownloadMetadata(chunks(bytes(damaged_flac)), extension="flac",
                                     media_type="audio/flac", quality="flac")

    class GoodBackup(Source):
        async def download(self, item, *, quality=None):
            return DownloadMetadata(chunks(good_flac), extension="flac", media_type="audio/flac",
                                     quality="flac")

    async def refresh(query, excluded):
        assert excluded == frozenset({"primary"})
        return result((backup,))

    outcome = asyncio.run(download_with_fallback(
        primary, {"primary": DamagedFlac(), "backup": GoodBackup()}, tmp_path,
        request_id="r", query="Song", quality="flac", quality_policy="lossless_first",
        refresh=refresh, record=events.append))

    assert outcome.download is not None and outcome.download_source_id == "backup"
    assert outcome.download.actual_quality == "flac"
    assert outcome.download.duration_seconds == pytest.approx(30)
    assert [event.error_code for event in events if event.error_code] == ["incomplete_audio"]
    assert [(event.source_id, event.status) for event in events if event.stage == "download"] == [
        ("primary", "failed"), ("backup", "success")]
    assert [(event.from_source_id, event.to_source_id) for event in events
            if event.stage == "channel_switch"] == [("primary", "backup")]
    published = tmp_path / outcome.download.relative_path
    assert published.read_bytes() == good_flac
    published_files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert published_files == [published]
    assert not list(tmp_path.rglob("*.part"))


def test_unverified_flac_catalogue_mismatch_switches_to_valid_backup(tmp_path):
    from test_media_duration import flac_bytes

    primary = Candidate(source_id="primary", source_version="1", item_id="i", title="Song",
                        artist="Artist", format="flac", duration=30)
    backup = Candidate(source_id="backup", source_version="1", item_id="i", title="Song",
                       artist="Artist", format="flac", duration=30)
    good_flac = (Path(__file__).resolve().parents[1] / "fixtures" / "media"
                 / "ffmpeg_30s.flac").read_bytes()
    # The shared fixture builder emits valid frame headers with sample-rate
    # code 0, so 8000 Hz comes from STREAMINFO and all 60 seconds are present.
    mismatched_unverified = flac_bytes(60 * 8000, rate=8000, verbatim=True)
    mismatched_unverified += b"unsupported-trailer"
    events = []

    class UnverifiedMismatch(Source):
        async def download(self, item, *, quality=None):
            return DownloadMetadata(chunks(mismatched_unverified), extension="flac",
                                     media_type="audio/flac", quality="flac")

    class GoodBackup(Source):
        async def download(self, item, *, quality=None):
            return DownloadMetadata(chunks(good_flac), extension="flac", media_type="audio/flac",
                                     quality="flac")

    async def refresh(query, excluded):
        assert excluded == frozenset({"primary"})
        return result((backup,))

    outcome = asyncio.run(download_with_fallback(
        primary, {"primary": UnverifiedMismatch(), "backup": GoodBackup()}, tmp_path,
        request_id="r", query="Song", quality="flac", quality_policy="lossless_first",
        refresh=refresh, record=events.append))

    assert outcome.download is not None and outcome.download_source_id == "backup"
    assert outcome.download.actual_quality == "flac"
    assert outcome.download.duration_seconds == pytest.approx(30)
    assert [event.error_code for event in events if event.error_code] == [
        "duration_unverified", "incomplete_audio"]
    assert [(event.source_id, event.status) for event in events if event.stage == "download"] == [
        ("primary", "failed"), ("backup", "success")]
    published = tmp_path / outcome.download.relative_path
    assert published.read_bytes() == good_flac
    published_files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert published_files == [published]
    assert not list(tmp_path.rglob("*.part"))


def test_best_available_preserves_reprompt_instead_of_content_switching(tmp_path):
    class Invalid(Source):
        async def download(self, item):
            raise MediaError("signature_mismatch")

    primary, backup = candidate("primary"), candidate("backup")
    calls_to_backup = []

    class Backup(Source):
        async def download(self, item):
            calls_to_backup.append(item.source_id)
            return await super().download(item)

    source = Backup()
    calls = []
    events = []

    async def refresh(query, excluded):
        calls.append(excluded)
        return result((backup,))

    outcome = asyncio.run(download_with_fallback(
        primary, {"primary": Invalid(), "backup": source}, tmp_path,
        request_id="r", query="Song", quality=None, quality_policy="best_available",
        refresh=refresh, record=events.append))

    assert outcome.download is None and outcome.download_error == "signature_mismatch"
    assert outcome.refreshed is not None and outcome.refreshed.candidates == (backup,)
    assert calls == [frozenset({"primary"})]
    assert calls_to_backup == []
    assert not [event for event in events if event.stage == "channel_switch"]


def test_failed_quality_replacement_is_recorded_before_original_stream_is_kept(tmp_path):
    class Lossy(Source):
        async def download(self, item, *, quality=None):
            return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg", quality="320k")

    class Unavailable(Source):
        async def download(self, item, *, quality=None):
            raise MediaError("download_failed")

    primary = candidate("primary")
    alternate = candidate("alternate")
    events = []

    async def refresh(query, excluded):
        return result((alternate,))

    outcome = asyncio.run(download_with_fallback(
        primary, {"primary": Lossy(), "alternate": Unavailable()}, tmp_path,
        request_id="r", query="Song", quality="flac", quality_policy="lossless_first",
        refresh=refresh, record=events.append))

    assert outcome.download is not None and outcome.download_source_id == "primary"
    assert [(event.source_id, event.stage, event.status, event.error_code)
            for event in events if event.stage == "download" and event.status == "failed"] == [
                ("alternate", "download", "failed", "download_failed")]


def test_content_switch_budget_is_bounded_and_never_retries_a_source(tmp_path):
    class Invalid(Source):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.attempts = 0

        async def download(self, item, *, quality=None):
            self.attempts += 1
            raise MediaError("incomplete_audio")

    primary = candidate("primary")
    alternatives = [candidate(name) for name in ("a", "b", "c", "d")]
    sources = {name: Invalid() for name in ("primary", "a", "b", "c", "d")}
    calls = []
    events = []

    async def refresh(query, excluded):
        calls.append(excluded)
        # A refresh can include a previously failed source. The job-local
        # attempted set, rather than trusting refresh ordering, owns exclusion.
        return result((alternatives[0], alternatives[0], *alternatives[1:]))

    outcome = asyncio.run(download_with_fallback(
        primary, sources, tmp_path, request_id="r", query="Song", quality="flac",
        quality_policy="lossless_first", refresh=refresh, record=events.append))

    assert outcome.download is None and outcome.download_error == "incomplete_audio"
    assert calls == [frozenset({"primary"})]
    assert [source.attempts for source in sources.values()] == [1, 1, 1, 1, 0]
    assert len([event for event in events if event.stage == "channel_switch"]) == 3
    assert [event.source_id for event in events if event.stage == "download" and event.status == "failed"] == [
        "primary", "a", "b", "c"]


def test_collapsed_search_row_can_fall_back_to_its_second_channel(tmp_path):
    class Invalid(Source):
        async def download(self, item, *, quality=None):
            raise MediaError("media_response_invalid")

    class Lossless(Source):
        async def download(self, item, *, quality=None):
            assert quality == "flac"
            return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg", quality="flac")

    first, second, third = (candidate(name) for name in ("first", "second", "third"))
    refreshed = result((first,), ((first, second, third),))
    events = []

    async def refresh(query, excluded):
        return refreshed

    outcome = asyncio.run(download_with_fallback(
        first, {"first": Invalid(), "second": Lossless(), "third": Invalid()}, tmp_path,
        request_id="r", query="Song", quality="flac", quality_policy="lossless_first",
        refresh=refresh, record=events.append))

    assert outcome.download is not None and outcome.download_source_id == "second"
    assert outcome.channel_switches == 1
    assert set(outcome.attempted_source_ids) == {"first", "second"}
    assert [(event.from_source_id, event.to_source_id) for event in events
            if event.stage == "channel_switch"] == [("first", "second")]


def test_collapsed_search_row_can_switch_through_all_three_alternates(tmp_path):
    class Invalid(Source):
        def __init__(self, source_id):
            super().__init__()
            self.source_id = source_id
            self.attempts = []

        async def download(self, item, *, quality=None):
            self.attempts.append(item.item_id)
            raise MediaError("media_response_invalid")

    rows = tuple(Candidate(source_id=name, source_version="1", item_id=f"{name}-item",
                           title="Song", artist="Artist", format="flac")
                 for name in ("primary", "a", "b", "c"))
    sources = {row.source_id: Invalid(row.source_id) for row in rows}
    events = []

    async def refresh(query, excluded):
        return result((rows[0],), (rows,))

    outcome = asyncio.run(download_with_fallback(
        rows[0], sources, tmp_path, request_id="r", query="Song", quality="flac",
        quality_policy="lossless_first", refresh=refresh, record=events.append))

    switches = [event for event in events if event.stage == "channel_switch"]
    assert outcome.download is None and outcome.channel_switches == 3
    assert set(outcome.attempted_source_ids) == {"primary", "a", "b", "c"}
    assert [source.attempts for source in sources.values()] == [["primary-item"], ["a-item"],
                                                                ["b-item"], ["c-item"]]
    assert len(switches) == 3
    assert switches[1].skipped_sources["a"] == "already_attempted"
    assert switches[2].skipped_sources["b"] == "already_attempted"


def test_refresh_failed_source_membership_checks_the_collapsed_channel_rows(tmp_path):
    primary = candidate("primary")
    backup = candidate("backup")
    source = Source(fail=True)
    events = []

    async def refresh(query, excluded):
        return result((backup,), ((backup, primary),))

    outcome = asyncio.run(download_with_fallback(
        primary, {"primary": source, "backup": Source()}, tmp_path, request_id="r", query="Song",
        refresh=refresh, record=events.append))

    assert outcome.refreshed is None
    assert outcome.refresh_error == "refresh_included_failed_source"


def test_empty_channel_rows_preserve_the_legacy_candidate_pool():
    wanted, backup = candidate("primary"), candidate("backup")
    foreign = Candidate(source_id="foreign", source_version="1", item_id="other",
                        title="Other Song", artist="Other Artist")
    legacy_result = result((backup, foreign))
    channel_rows = replacement_channel_rows(wanted, legacy_result)

    legacy = replacement_candidates(wanted, legacy_result.candidates,
                                    {"backup": Source(), "foreign": Source()})
    empty_channels = replacement_candidates(wanted, legacy_result.candidates,
                                            {"backup": Source(), "foreign": Source()},
                                            channel_rows=channel_rows)

    assert empty_channels == legacy == ([backup], {"foreign": "different_recording"})


def test_timed_out_content_replacement_records_one_media_timeout_failure(tmp_path):
    class Invalid(Source):
        async def download(self, item, *, quality=None):
            raise MediaError("media_response_invalid")

    class Slow(Source):
        async def download(self, item, *, quality=None):
            await asyncio.sleep(0.5)
            return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg")

    primary, backup = candidate("primary"), candidate("backup")
    events = []

    async def refresh(query, excluded):
        return result((backup,))

    outcome = asyncio.run(download_with_fallback(
        primary, {"primary": Invalid(), "backup": Slow()}, tmp_path, request_id="r", query="Song",
        quality="flac", quality_policy="lossless_first", refresh=refresh,
        resolve_stream_timeout=0.05, record=events.append))

    failures = [event for event in events if event.source_id == "backup"
                and event.stage == "download" and event.status == "failed"]
    assert outcome.download_error == "media_timeout"
    assert [(event.error_code, event.requested_quality) for event in failures] == [("media_timeout", "flac")]


def test_duplicate_replacement_rows_do_not_mark_a_chosen_source_skipped():
    wanted = candidate("primary")
    backup = candidate("backup")

    ranked, skipped = replacement_candidates(wanted, (backup, backup), {"backup": Source()})

    assert ranked == [backup]
    assert "backup" not in skipped


def test_failure_refreshes_once_excludes_source_and_checks_health(tmp_path):
    source = Source(fail=True)
    events = []
    calls = []
    replacement = candidate("b")

    async def refresh(query, excluded):
        calls.append((query, excluded))
        return result((replacement,))

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.download is None
    assert outcome.download_error == "download_failed"
    assert outcome.refreshed.candidates == (replacement,)
    assert calls == [("Song", frozenset({"a"}))]
    assert source.health_calls == 1
    assert [(e.stage, e.status) for e in events] == [
        ("download", "failed"), ("refresh", "success"), ("health", "success")
    ]


def test_an_unexpected_resolver_failure_is_a_failed_channel_not_an_uncertain_effect(tmp_path):
    """A bare resolver error must reach the refresh path, not the uncertain one.

    Measured 2026-09-28: the plugin client answers a resolve it could not complete with
    a plain ``RuntimeError``.  A lossless ask resolves in ``download_with_fallback``
    rather than inside ``download_candidate``, which is where that shape used to be
    normalised, so without the same normalisation here the job would be recorded as
    uncertain and retried to a dead letter instead of re-prompting the operator with
    another channel.
    """
    class Broken(Source):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def download(self, item, *, quality=None):
            self.calls += 1
            assert quality == "flac"
            raise RuntimeError("plugin_resolve_failed")

    source = Broken()
    events = []
    calls = []
    replacement = candidate("b")

    async def refresh(query, excluded):
        calls.append((query, excluded))
        return result((replacement,))

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", quality="flac", refresh=refresh, record=events.append))

    assert outcome.download is None and outcome.download_error == "download_failed"
    assert source.calls == 1
    assert outcome.refreshed is not None and outcome.refreshed.candidates == (replacement,)
    assert calls == [("Song", frozenset({"a"}))]
    assert [(event.stage, event.status, event.error_code) for event in events] == [
        ("download", "failed", "download_failed"),
        ("refresh", "success", None),
        ("health", "success", None),
    ]


def test_refresh_failure_still_checks_health_and_redacts_exception(tmp_path):
    source = Source(fail=True)
    events = []

    async def refresh(query, excluded):
        raise RuntimeError("https://x.test/?api_key=SECRET&session=private")

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.refresh_error == "refresh_failed"
    assert outcome.healthy is True
    for secret in ("SECRET", "private", "api_key", "session", "https://x.test/?api_key=SECRET&session=private"):
        assert secret not in str(outcome)
        assert all(secret not in str(event.__dict__) for event in events)
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "download_failed"),
        ("refresh", "failed", "refresh_failed"),
        ("health", "success", None),
    ]


def test_missing_source_refreshes_and_marks_health_unavailable(tmp_path):
    events = []
    calls = []

    async def refresh(query, excluded):
        calls.append(excluded)
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.download_error == "source_unavailable"
    assert outcome.healthy is None
    assert calls == [frozenset({"a"})]
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "source_unavailable"),
        ("refresh", "success", None),
        ("health", "unavailable", "source_unavailable"),
    ]


def test_refresh_containing_failed_source_is_rejected(tmp_path):
    source = Source(fail=True)
    events = []

    async def refresh(query, excluded):
        return result((candidate("a"),))

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))

    assert outcome.refreshed is None
    assert outcome.refresh_error == "refresh_included_failed_source"
    assert source.health_calls == 1


def test_health_failure_is_redacted(tmp_path):
    class Unhealthy(Source):
        async def health(self):
            self.health_calls += 1
            raise RuntimeError("session=SECRET_API_KEY")

    source = Unhealthy(fail=True)

    async def refresh(query, excluded):
        return result()

    events = []
    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
        request_id="r", query="Song", refresh=refresh, record=events.append))
    assert outcome.healthy is None
    assert outcome.refresh_error is None
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "download_failed"),
        ("refresh", "success", None),
        ("health", "failed", "health_failed"),
    ]
    for secret in ("SECRET_API_KEY", "session", "api_key"):
        assert secret not in str(outcome)
        assert all(secret not in str(event.__dict__) for event in events)


def test_download_cancellation_does_not_fallback(tmp_path):
    class CancelDownload(Source):
        async def download(self, item):
            raise asyncio.CancelledError

    events = []
    calls = []

    async def refresh(query, excluded):
        calls.append(1)
        return result()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_with_fallback(candidate(), {"a": CancelDownload()}, tmp_path,
            request_id="r", query="Song", refresh=refresh, record=events.append))
    assert calls == []
    assert [(e.stage, e.status) for e in events] == [("download", "failed")]


def test_refresh_cancellation_does_not_check_health(tmp_path):
    source = Source(fail=True)
    events = []

    async def refresh(query, excluded):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
            request_id="r", query="Song", refresh=refresh, record=events.append))
    assert source.health_calls == 0
    assert [(e.stage, e.status) for e in events] == [("download", "failed")]


def test_health_cancellation_stops_after_refresh(tmp_path):
    class CancelHealth(Source):
        async def health(self):
            self.health_calls += 1
            raise asyncio.CancelledError

    source = CancelHealth(fail=True)
    events = []

    async def refresh(query, excluded):
        return result()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path,
            request_id="r", query="Song", refresh=refresh, record=events.append))
    assert [(e.stage, e.status) for e in events] == [
        ("download", "failed"), ("refresh", "success")
    ]


def test_recorder_failure_does_not_block_fallback_stages(tmp_path):
    source = Source(fail=True)
    calls = []
    async def refresh(query, excluded):
        calls.append(excluded)
        return result()
    def record(_): raise RuntimeError("observer")
    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path, request_id="r", query="q", refresh=refresh, record=record))
    assert outcome.download_error == "download_failed" and outcome.refresh_error is None and outcome.healthy is True
    assert calls == [frozenset({"a"})] and source.health_calls == 1


def test_media_root_resolve_failure_direct_and_fallback(tmp_path, monkeypatch):
    from musicdl.media import download_candidate
    root = Path(tmp_path)
    original = Path.resolve
    def resolve(self, *args, **kwargs):
        if self == root:
            raise OSError("api_key=SECRET")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    events = []
    with pytest.raises(MediaError) as caught:
        asyncio.run(download_candidate(candidate(), Source(), root, request_id="d", record=events.append))
    assert caught.value.code == "download_failed" and caught.value.__suppress_context__
    assert events[0].error_code == "download_failed"
    calls = []
    async def refresh(query, excluded):
        calls.append(excluded)
        return result()
    source = Source()
    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, root, request_id="f", query="q", refresh=refresh))
    assert outcome.download_error == "download_failed" and calls == [frozenset({"a"})] and source.health_calls == 1
    assert "SECRET" not in repr((caught.value, events, outcome))


def test_resolve_stream_budget_bounds_a_slow_download(tmp_path):
    class Slow(Source):
        async def download(self, item):
            await asyncio.sleep(0.5)
            return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg")

    source = Slow()
    events = []

    async def refresh(query, excluded):
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path, request_id="r",
        query="Song", refresh=refresh, resolve_stream_timeout=0.05, record=events.append))

    assert outcome.download is None
    assert outcome.download_error == "media_timeout"
    assert source.health_calls == 1
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "download_cancelled"),
        ("download", "failed", "media_timeout"),
        ("refresh", "success", None),
        ("health", "success", None),
    ]


def test_refresh_budget_is_enforced_before_health(tmp_path):
    source = Source(fail=True)

    async def slow_refresh(query, excluded):
        await asyncio.sleep(0.5)
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path, request_id="r",
        query="Song", refresh=slow_refresh, refresh_timeout=0.05))

    assert outcome.refresh_error == "refresh_failed"
    assert outcome.refreshed is None
    assert source.health_calls == 1


def test_health_budget_is_configurable(tmp_path):
    class SlowHealth(Source):
        async def health(self):
            self.health_calls += 1
            await asyncio.sleep(0.5)
            return True

    source = SlowHealth(fail=True)
    events = []

    async def refresh(query, excluded):
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": source}, tmp_path, request_id="r",
        query="Song", refresh=refresh, health_timeout=0.05, record=events.append))

    assert outcome.healthy is None
    assert [(e.stage, e.status, e.error_code) for e in events][-1] == ("health", "failed", "health_failed")


@pytest.mark.parametrize("kwargs", [
    {"resolve_stream_timeout": 0},
    {"resolve_stream_timeout": -1},
    {"resolve_stream_timeout": float("nan")},
    {"resolve_stream_timeout": True},
    {"refresh_timeout": 0},
    {"refresh_timeout": float("inf")},
    {"health_timeout": 0},
    {"health_timeout": float("nan")},
])
def test_invalid_budgets_are_rejected(tmp_path, kwargs):
    async def refresh(query, excluded):
        return result()

    with pytest.raises(ValueError, match="invalid_timeout"):
        asyncio.run(download_with_fallback(candidate(), {"a": Source()}, tmp_path, request_id="r",
            query="q", refresh=refresh, **kwargs))


def test_reservation_owner_and_fence_are_forwarded_to_download(tmp_path, monkeypatch):
    captured = {}

    async def fake_download(candidate_value, source_value, media_root, **kwargs):
        captured.update(kwargs)
        return "downloaded"

    monkeypatch.setattr("musicdl.media.fallback.download_candidate", fake_download)
    reservation = ArtifactRecord(job_id="job", candidate_id="i", temporary_relative_path=".musicdl-staging/t.part",
                                 target_relative_path="t.mp3", allocation_slot=0, extension="mp3",
                                 media_type="audio/mpeg")

    async def refresh(query, excluded):
        return result()

    outcome = asyncio.run(download_with_fallback(candidate(), {"a": Source()}, tmp_path, request_id="r",
        query="q", refresh=refresh, reservation=reservation, artifact_store="store", owner="owner", fence=7))

    assert outcome.download == "downloaded"
    assert captured["reservation"] is reservation
    assert (captured["artifact_store"], captured["owner"], captured["fence"]) == ("store", "owner", 7)


def test_fallback_reresolves_a_descriptor_the_transport_retired(tmp_path):
    """A descriptor that dies between resolve and stream is resolved again.

    The transport refuses a URL the source has already retired, so the frozen
    answer can never be streamed blind; the fallback asks the source for a live
    one and keeps the download instead of reporting a failure.
    """
    retired, alive = "2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z"

    class Retiring(Source):
        def __init__(self):
            super().__init__()
            self.resolved = 0

        async def download(self, item, quality=None):
            self.resolved += 1
            return DownloadMetadata(chunks(ID3), extension="mp3", media_type="audio/mpeg",
                                    quality="flac",
                                    expires_at=retired if self.resolved == 1 else alive)

    source = Retiring()
    events = []

    async def refresh(query, excluded):
        return result()

    outcome = asyncio.run(download_with_fallback(
        candidate(), {"a": source}, tmp_path, request_id="r", query="Song",
        quality="flac", quality_policy="lossless_first", refresh=refresh, record=events.append))

    assert outcome.download is not None
    assert source.resolved == 2
    # The retired answer is reported honestly rather than hidden, and the
    # download only counts once the live descriptor has produced the bytes.
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("download", "failed", "media_url_expired"),
        ("duration", "unverified", "duration_unverified"),
        ("download", "success", None),
    ]
def test_a_reresolved_answer_stamps_the_quality_it_delivered(tmp_path):
    """The verdict describes the bytes that arrive, not the retired descriptor.

    A source can answer 320k, have that URL retire before it is streamed, and
    answer FLAC on the second resolve.  The file is lossless, so the record must
    not keep calling it a downgrade just because the first answer was lossy.
    """
    retired, alive = "2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z"
    flac = b"fLaC" + b"\x00" * 12

    class Retiring(Source):
        def __init__(self):
            super().__init__()
            self.resolved = 0

        async def download(self, item, quality=None):
            self.resolved += 1
            first = self.resolved == 1
            return DownloadMetadata(chunks(ID3 if first else flac),
                                    extension="mp3" if first else "flac",
                                    media_type="audio/mpeg" if first else "audio/flac",
                                    quality="320k" if first else "flac",
                                    expires_at=retired if first else alive)

    source = Retiring()
    events = []

    async def refresh(query, excluded):
        return result()

    outcome = asyncio.run(download_with_fallback(
        candidate(), {"a": source}, tmp_path, request_id="r", query="Song", quality="flac",
        quality_policy="lossless_first", max_quality_switches=0, refresh=refresh, record=events.append))

    download = outcome.download
    assert download is not None
    assert source.resolved == 2
    assert (download.extension, download.actual_quality) == (".flac", "flac")
    assert download.quality_downgraded is False
    # The lossy answer is still reported as it was observed -- the record just
    # does not repeat that verdict about a file the second resolve made lossless.
    assert [(e.stage, e.status, e.error_code) for e in events] == [
        ("quality_downgraded", "observed", None),
        ("download", "failed", "media_url_expired"),
        ("duration", "unverified", "duration_unverified"),
        ("download", "success", None),
    ]
