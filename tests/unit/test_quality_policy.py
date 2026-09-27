import asyncio
import pytest
from musicdl.config import WorkerSettings
from musicdl.sources import quality
from musicdl.sources.models import Candidate


def candidate(**overrides):
    return Candidate(source_id='source', source_version='1', item_id='song', title='Song', artist='Artist', format='mp3', **overrides)


def test_policy_defaults():
    settings = WorkerSettings()
    assert getattr(settings, 'quality_policy', None) == 'lossless_first'
    assert getattr(settings, 'quality_preference', 'absent') is None


@pytest.mark.parametrize('preference,expected', [(None, 'flac'), ('320k', '320k'), ('unsupported', 'flac')])
def test_requested_quality_uses_declarations(preference, expected):
    choose = getattr(quality, 'requested_quality', None)
    assert callable(choose), 'shared quality policy selector is required'
    assert choose(candidate(qualities=('320k', 'flac')), preference=preference) == expected


def test_best_available_omits_quality_request():
    choose = getattr(quality, 'requested_quality', None)
    assert callable(choose), 'shared quality policy selector is required'
    assert choose(candidate(qualities=('flac',)), policy='best_available') is None

from musicdl.media.models import DownloadMetadata, MediaError, _CloseOnce
from musicdl.media.fallback import download_with_fallback
from musicdl.sources.search import SearchResult, search_sources
from musicdl.sources.registry import SourceRegistry, SourceEntry
from musicdl.wecom.results import success_message

ID3 = b'ID3\x04\x00\x00\x00\x00\x00\x00xyz'


class AudioSource:
    def __init__(self, actual='320k', *, fail=False, body_fail=False, stated=None):
        self.actual, self.fail, self.body_fail = actual, fail, body_fail
        self.stated = actual if stated is None else stated
        self.calls = []
        self.closed = 0

    async def download(self, candidate, *, quality=None):
        self.calls.append((candidate.source_id, candidate.item_id, quality))
        if self.fail:
            raise MediaError('download_failed')
        async def chunks():
            if self.body_fail:
                raise MediaError('download_failed')
            yield b'fLaCdata' if self.actual == 'flac' else ID3
        async def close():
            self.closed += 1
        return DownloadMetadata(chunks(), extension='flac' if self.actual == 'flac' else 'mp3',
                                quality=self.stated, _close_once=_CloseOnce(close))

    async def health(self):
        return True


def lossless_candidate(source='source'):
    return candidate(qualities=('flac', '320k')).model_copy(update={'source_id': source, 'format': 'flac'})


@pytest.mark.parametrize('alternate_kind', ['lossless', 'lossy', 'unavailable', 'broken_body', 'none'])
def test_quality_switch_is_bounded_and_retains_obtainable_audio(tmp_path, alternate_kind):
    original = AudioSource()
    alternate = AudioSource('flac' if alternate_kind == 'lossless' else '320k',
                            fail=alternate_kind == 'unavailable', body_fail=alternate_kind == 'broken_body')
    events, refreshes = [], []
    async def refresh(query, excluded):
        refreshes.append(excluded)
        rows = () if alternate_kind == 'none' else (lossless_candidate('alternate'),)
        return SearchResult(rows, (), 'v', (('alternate', 'third'),) if rows else ())
    result = asyncio.run(download_with_fallback(lossless_candidate(), {'source': original, 'alternate': alternate},
        tmp_path, request_id='request', query='Song', refresh=refresh, quality='flac', record=events.append))
    assert result.download is not None
    expected_flac = alternate_kind == 'lossless'
    assert result.download.extension == ('.flac' if expected_flac else '.mp3')
    assert result.download.quality_downgraded is (not expected_flac)
    assert len(refreshes) == 1 and refreshes[0] == frozenset({'source'})
    assert len(original.calls) == 1 and len(alternate.calls) <= 1
    notices = [event for event in events if event.stage == 'quality_downgraded']
    assert len(notices) == 1 and (notices[0].requested_quality, notices[0].actual_quality) == ('flac', '320k')
    assert len(list(tmp_path.rglob('*.mp3'))) + len(list(tmp_path.rglob('*.flac'))) == 1
    assert original.closed == 1
    if alternate_kind not in {'none', 'unavailable'}:
        assert alternate.closed == 1


def test_a_source_that_states_no_tier_is_judged_by_the_bytes_it_sent(tmp_path):
    """An unstated tier is not evidence of a downgrade; the verified file is.

    The source answers a lossless request with no tier at all and sends MP3
    bytes.  Nothing is retried -- the alternate-source switch is only ever
    triggered by a tier the source itself declared -- but the report still
    says the lossless request was not answered with lossless audio, because
    the verdict is read off the container the bytes proved.
    """
    source = AudioSource(None)
    async def refresh(*args):
        pytest.fail('an unstated tier is not evidence of downgrade')
    result = asyncio.run(download_with_fallback(lossless_candidate(), {'source': source}, tmp_path,
        request_id='r', query='Song', refresh=refresh, quality='flac'))
    assert result.download.quality_downgraded
    # ``quality`` keeps its one meaning -- the tier the source stated -- while
    # the tier that really arrived is named separately, so the report is never
    # emptier than the file.
    assert result.download.quality is None
    assert result.download.actual_quality == 'mp3'
    assert result.download.extension == '.mp3'


def test_success_event_names_the_tier_asked_for_and_the_tier_that_arrived(tmp_path):
    """The success event carries both tiers, side by side, from the real answer."""
    events = []
    source = AudioSource('320k')
    async def refresh(*args):
        return SearchResult((), (), 'v')
    result = asyncio.run(download_with_fallback(lossless_candidate(), {'source': source}, tmp_path,
        request_id='r', query='Song', refresh=refresh, quality='flac', record=events.append))
    delivered = [event for event in events if event.stage == 'download' and event.status == 'success']
    assert len(delivered) == 1
    assert (delivered[0].requested_quality, delivered[0].actual_quality) == ('flac', '320k')
    assert (result.download.requested_quality, result.download.actual_quality) == ('flac', '320k')
    assert result.download.quality_downgraded


@pytest.mark.parametrize('stated,container,expected', [
    ('flac', 'mp3', 'mp3'),
    ('flac24bit', 'mp3', 'mp3'),
    ('flac', 'flac', 'flac'),
    ('flac24bit', '.flac', 'flac24bit'),
    ('320k', 'mp3', '320k'),
    ('alac', 'm4a', 'm4a'),
    ('master', 'mp3', 'master'),
    (None, '.flac', 'flac'),
    (None, None, None),
])
def test_served_quality_lets_the_verified_container_outrank_a_contradicted_label(stated, container, expected):
    assert quality.served_quality(stated, container) == expected


def test_a_label_the_verified_container_contradicts_never_reaches_the_report(tmp_path):
    """A `.flac` path that answers MP3 bytes is reported as MP3.

    The transport writes the container the bytes announce, so a report that
    repeated the path's claim would name a file that is not on disk.
    """
    events = []
    source = AudioSource('320k', stated='flac')
    async def refresh(*args):
        return SearchResult((), (), 'v')
    result = asyncio.run(download_with_fallback(lossless_candidate(), {'source': source}, tmp_path,
        request_id='r', query='Song', refresh=refresh, quality='flac', record=events.append))
    delivered = [event for event in events if event.stage == 'download' and event.status == 'success']
    assert len(delivered) == 1
    assert delivered[0].actual_quality == 'mp3'
    assert result.download.actual_quality == 'mp3'
    assert result.download.extension == '.mp3'
    # The source labelled the answer lossless and the file is not, so the
    # downgrade verdict follows the bytes rather than the label.
    assert result.download.quality_downgraded
    # ``quality`` keeps the tier the source claimed; only the reconciliation
    # onto the verified container is new.
    assert result.download.quality == 'flac'


def test_a_switched_source_records_the_tier_it_was_actually_asked_for(tmp_path):
    """The record keeps the tier the alternate was asked for, not the opening request.

    A downgrade switch asks the alternate for whichever tier *it* declares.  The
    download record is what a replay re-reads, so a record naming the job's
    opening request would ask that source for something it never offered.
    """
    from test_worker_job import Redis, State, WeCom
    from musicdl.worker.workers import JobWorker
    state, wecom = State(), WeCom()
    original, alternate = AudioSource(), AudioSource()
    alternate_row = lossless_candidate('alternate').model_copy(update={'qualities': ('320k',)})
    async def refresh(query, excluded):
        return SearchResult((alternate_row,), (), 'v', (('alternate',),))
    worker = JobWorker(Redis(), wecom, {'source': original, 'alternate': alternate}, str(tmp_path),
                       state=state, refresh=refresh)
    job = {'request_id': 'r', 'from_user': 'u', 'candidate': lossless_candidate().model_dump(mode='json')}
    async def scenario():
        first = await worker.handle_job(job, job_id='quality-tier')
        effect = await state.get_job_effect('quality-tier', 'download')
        second = await worker.handle_job(job, job_id='quality-tier')
        return first, effect.result, second
    first, persisted, second = asyncio.run(scenario())
    assert alternate.calls == [('alternate', 'song', '320k')]
    assert first.download.requested_quality == '320k'
    assert persisted['requested_quality'] == '320k'
    assert second.download.requested_quality == '320k'
    assert len(original.calls) == len(alternate.calls) == 1
    assert len(wecom.sent) == 1


def test_legacy_resolver_without_keyword_remains_usable(tmp_path):
    class Legacy(AudioSource):
        async def download(self, candidate):
            return await super().download(candidate)
    async def refresh(*args):
        return SearchResult((), (), 'v')
    result = asyncio.run(download_with_fallback(lossless_candidate(), {'source': Legacy()}, tmp_path,
        request_id='r', query='Song', refresh=refresh, quality='flac'))
    assert result.download is not None


def test_worker_replay_keeps_actual_quality_and_path_without_switching_again(tmp_path):
    from test_worker_job import Redis, State, WeCom
    from musicdl.worker.workers import JobWorker
    state, wecom = State(), WeCom()
    original, alternate = AudioSource(), AudioSource()
    refreshes = []
    async def refresh(query, excluded):
        refreshes.append(excluded)
        return SearchResult((lossless_candidate('alternate'),), (), 'v', (('alternate',),))
    worker = JobWorker(Redis(), wecom, {'source': original, 'alternate': alternate}, str(tmp_path),
                       state=state, refresh=refresh)
    job = {'request_id': 'r', 'from_user': 'u', 'candidate': lossless_candidate().model_dump(mode='json')}
    async def scenario():
        first = await worker.handle_job(job, job_id='quality-1')
        second = await worker.handle_job(job, job_id='quality-1')
        return first, second
    first, second = asyncio.run(scenario())
    assert first.download == second.download
    assert second.download.quality == '320k' and second.download.quality_downgraded
    assert second.download.relative_path.suffix == '.mp3'
    assert len(original.calls) == len(alternate.calls) == len(refreshes) == 1
    assert len(wecom.sent) == 1
    assert 'MP3 320kbps' in wecom.sent[0][1] and '13 B' in wecom.sent[0][1]
    assert '未取到无损' in wecom.sent[0][1]
    record = asyncio.run(state.get_artifact('quality-1'))
    assert record.target_relative_path.endswith('.mp3')


def test_a_replay_reconciles_a_stored_label_with_the_bytes_on_disk(tmp_path):
    """A record written by an earlier revision cannot make a replay lie.

    The artifact's own container is the evidence a replay stands on, so a stored
    ``actual_quality`` the file contradicts is dropped exactly the way a fresh
    download drops it.
    """
    from test_worker_job import Redis, State, WeCom
    from musicdl.worker.workers import JobWorker
    state, wecom = State(), WeCom()
    original, alternate = AudioSource(), AudioSource()

    async def refresh(query, excluded):
        return SearchResult((lossless_candidate('alternate'),), (), 'v', (('alternate',),))

    worker = JobWorker(Redis(), wecom, {'source': original, 'alternate': alternate}, str(tmp_path),
                       state=state, refresh=refresh)
    job = {'request_id': 'r', 'from_user': 'u', 'candidate': lossless_candidate().model_dump(mode='json')}

    async def scenario():
        await worker.handle_job(job, job_id='quality-1')
        return await worker._replay_download('quality-1', job, lossless_candidate(), 'u',
                                             asyncio.get_running_loop().time() + 5,
                                             outcome={'requested_quality': 'flac', 'actual_quality': 'flac'})

    replayed = asyncio.run(scenario())
    assert replayed.download.relative_path.suffix == '.mp3'
    assert replayed.download.actual_quality == 'mp3'
    assert replayed.download.quality == 'mp3'


def test_a_replay_re_derives_the_downgrade_from_the_artifact(tmp_path):
    """A stored verdict cannot outlive the bytes it was written about.

    The record was written when the label said ``flac``; the artifact on disk
    is MP3.  The replay reports what the file is, so the stale ``False`` does
    not reach the report.
    """
    from test_worker_job import Redis, State, WeCom
    from musicdl.worker.workers import JobWorker
    state, wecom = State(), WeCom()
    original, alternate = AudioSource(), AudioSource()
    async def refresh(query, excluded):
        return SearchResult((lossless_candidate('alternate'),), (), 'v', (('alternate',),))
    worker = JobWorker(Redis(), wecom, {'source': original, 'alternate': alternate}, str(tmp_path),
                       state=state, refresh=refresh)
    job = {'request_id': 'r', 'from_user': 'u', 'candidate': lossless_candidate().model_dump(mode='json')}
    async def scenario():
        await worker.handle_job(job, job_id='quality-1')
        return await worker._replay_download('quality-1', job, lossless_candidate(), 'u',
                                             asyncio.get_running_loop().time() + 5,
                                             outcome={'requested_quality': 'flac', 'actual_quality': 'flac',
                                                      'quality_downgraded': False})
    replayed = asyncio.run(scenario())
    assert replayed.download.actual_quality == 'mp3'
    assert replayed.download.quality_downgraded


@pytest.mark.parametrize('policy,preference,expected', [
    ('lossless_first', None, 'lossless'),
    ('lossless_first', '320k', 'lossy'),
    ('lossless_first', 'unsupported', 'lossless'),
    ('best_available', None, 'lossy'),
    ('best_available', '320k', 'lossy'),
    ('best_available', 'flac', 'lossless'),
    ('best_available', 'unsupported', 'lossless'),
])
def test_channel_selection_and_offers_share_quality_order(policy, preference, expected):
    class Search:
        def __init__(self, row): self.row = row
        async def search(self, query): return [self.row]
    entries = []
    for name, qualities, priority in [('lossy', ('320k',), 0), ('lossless', ('flac',), 10)]:
        row = candidate(qualities=qualities).model_copy(update={'source_id': name})
        entries.append(SourceEntry(name, '1', Search(row), priority=priority))
    result = asyncio.run(search_sources(SourceRegistry(entries), 'Song', quality_policy=policy,
                                        quality_preference=preference))
    assert len(result.candidates) == 1
    assert result.candidates[0].source_id == expected
    assert result.offers[0][0] == expected


def test_quality_refresh_is_reused_for_normal_failure_with_offers_intact(tmp_path):
    original, alternate = AudioSource(body_fail=True), AudioSource(body_fail=True)
    refreshed = SearchResult((lossless_candidate('alternate'),), (), 'v', (('alternate', 'third'),))
    calls = []
    async def refresh(query, excluded):
        calls.append(excluded)
        return refreshed
    result = asyncio.run(download_with_fallback(lossless_candidate(), {'source': original, 'alternate': alternate},
        tmp_path, request_id='r', query='Song', refresh=refresh, quality='flac'))
    assert result.download is None
    assert result.refreshed is refreshed and result.refreshed.offers == (('alternate', 'third'),)
    assert len(calls) == 1


def test_quality_refresh_honors_its_own_timeout_and_keeps_original(tmp_path):
    original, alternate = AudioSource(), AudioSource('flac')
    cancelled = []
    async def refresh(query, excluded):
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return SearchResult((lossless_candidate('alternate'),), (), 'v', (('alternate',),))
    result = asyncio.run(download_with_fallback(lossless_candidate(), {'source': original, 'alternate': alternate},
        tmp_path, request_id='r', query='Song', refresh=refresh, quality='flac',
        resolve_stream_timeout=2.0, refresh_timeout=0.01))
    assert alternate.calls == [], 'refresh timeout must prevent the alternate attempt'
    assert cancelled == [True]
    assert result.download is not None and result.download.quality == '320k'
    assert result.download.quality_downgraded and result.download.extension == '.mp3'


def test_best_available_preserves_legacy_populated_format_bitrate_order_and_offers():
    class Search:
        def __init__(self, row): self.row = row
        async def search(self, query): return [self.row]
    rows = [
        ('flac', 'flac', 1000, ('flac',), 0),
        ('alac-first', 'alac', 1500, ('alac',), 0),
        ('alac-second', 'alac', 1500, ('master', 'flac'), 5),
        ('lossy', 'mp3', 320, ('master', 'flac'), 0),
    ]
    entries = []
    for name, container, bitrate, tiers, priority in rows:
        row = candidate(qualities=tiers).model_copy(update={
            'source_id': name, 'format': container, 'bitrate': bitrate, 'size': 1000,
        })
        entries.append(SourceEntry(name, '1', Search(row), priority=priority))
    result = asyncio.run(search_sources(SourceRegistry(entries), 'Song', quality_policy='best_available'))
    # v1.0.4 compares lossless(format), then bitrate before completeness/size/priority.
    assert [row.source_id for row in result.candidates] == ['alac-first', 'flac', 'lossy']
    assert result.offers == (('alac-first', 'alac-second'), ('flac',), ('lossy',))
    assert result.candidates[0].bitrate == 1500 and result.candidates[0].format == 'alac'
