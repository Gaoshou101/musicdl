"""Execute the real LX shim in Node when its Deno supervisor is unavailable.

These tests cover resolver policy only; Deno isolation tests remain separate.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from musicdl.sources.quality import proven_lossy, requested_quality
from musicdl.sources.models import Candidate

NODE = shutil.which('node')
SHIM = Path(__file__).resolve().parents[2] / 'src/musicdl_plugin_runner/lx_shim.js'
HARNESS = r'''
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
globalThis.invocation = { manifest: { plugin_id: 'fixture', version: '1' }, actions: [], observations: [] };
const source = readFileSync(process.argv[1], 'utf8');
await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
let selected;
globalThis.lx.on(globalThis.lx.EVENT_NAMES.request, async ({info}) => {
  selected = info.type;
  return input.answer;
});
const resolved = await globalThis.handle({operation: 'resolve', payload: input.payload});
console.log(JSON.stringify({selected, resolved}));
'''


SEARCH_HARNESS = r'''
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
globalThis.invocation = { manifest: { plugin_id: 'fixture', version: '1' }, actions: [], observations: [] };
const source = readFileSync(process.argv[1], 'utf8');
await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
let selected;
globalThis.lx.on(globalThis.lx.EVENT_NAMES.request, async ({info}) => {
  selected = info.type;
  return input.answer;
});
globalThis.lx.send(globalThis.lx.EVENT_NAMES.inited, {sources: input.sources});
const candidates = await globalThis.handle({operation: 'search', payload: input.payload});
console.log(JSON.stringify({selected, candidates}));
'''


RETRY_HARNESS = r'''
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
globalThis.invocation = { manifest: { plugin_id: 'fixture', version: '1' }, actions: [], observations: [] };
const source = readFileSync(process.argv[1], 'utf8');
await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const asked = [];
globalThis.lx.on(globalThis.lx.EVENT_NAMES.request, async ({info}) => {
  asked.push(info.type);
  const answer = input.answers[asked.length - 1];
  if (answer === undefined) return {};
  if (answer && answer.__throw) throw new Error(answer.__throw);
  return answer;
});
let resolved = null;
let error = null;
try {
  resolved = await globalThis.handle({operation: 'resolve', payload: input.payload});
} catch (caught) {
  error = String(caught && caught.message || caught);
}
console.log(JSON.stringify({asked, resolved, error}));
'''


def search(*, item, sources=None, query='Song'):
    if NODE is None:
        pytest.skip('Node executable unavailable; Deno coverage is separate')
    if sources is None:
        sources = {'kw': {'actions': ['musicSearch']}}
    payload = {'source': 'kw', 'query': query}
    process = subprocess.run([NODE, '--input-type=module', '-e', SEARCH_HARNESS, str(SHIM)],
                             input=json.dumps({'payload': payload, 'answer': {'list': [item]},
                                               'sources': sources}),
                             text=True, encoding='utf-8', capture_output=True, timeout=10, check=False)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def resolve(*, tiers=('flac', '320k'), requested=None, answer='https://cdn.example/song.mp3'):
    if NODE is None:
        pytest.skip('Node executable unavailable; Deno coverage is separate')
    candidate = {'source_id': 'fixture', 'source_version': '1', 'item_id': 'lx:kw:42',
                 'title': 'Song', 'artist': 'Artist', 'qualities': list(tiers),
                 'quality_sizes': {'flac': 10000, 'flac24bit': 20000, '320k': 1000}}
    payload = {'candidate': candidate}
    if requested is not None:
        payload['quality'] = requested
    process = subprocess.run([NODE, '--input-type=module', '-e', HARNESS, str(SHIM)],
                             input=json.dumps({'payload': payload, 'answer': answer}),
                             text=True, encoding='utf-8', capture_output=True, timeout=10, check=False)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def resolve_trace(*, tiers=('flac', '320k'), requested=None, answers=({},)):
    """Resolve once and report every tier the source was asked for, in order.

    ``answers`` supplies the reply for each ``musicUrl`` call positionally, so
    one entry lets a call fail and the next lets the one graceful retry
    succeed.  A ``{'__throw': ...}`` entry makes that call raise instead.
    """
    if NODE is None:
        pytest.skip('Node executable unavailable; Deno coverage is separate')
    candidate = {'source_id': 'fixture', 'source_version': '1', 'item_id': 'lx:kw:42',
                 'title': 'Song', 'artist': 'Artist', 'qualities': list(tiers),
                 'quality_sizes': {'flac': 10000, 'flac24bit': 20000, '320k': 1000}}
    payload = {'candidate': candidate}
    if requested is not None:
        payload['quality'] = requested
    process = subprocess.run([NODE, '--input-type=module', '-e', RETRY_HARNESS, str(SHIM)],
                             input=json.dumps({'payload': payload, 'answers': list(answers)}),
                             text=True, encoding='utf-8', capture_output=True, timeout=10, check=False)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


@pytest.mark.parametrize('tiers,requested,expected', [
    (('flac', '320k'), None, '320k'),
    (('flac24bit', 'flac'), None, 'flac24bit'),
    ((), None, '320k'),
    (('flac', '320k'), 'flac', 'flac'),
    (('flac', '320k'), '320k', '320k'),
    # A name the shim cannot recognise is no request at all, so the default
    # rule decides exactly as it would have for an absent field.
    (('flac', '320k'), 'unavailable', '320k'),
])
def test_real_shim_default_and_explicit_quality_selection(tiers, requested, expected):
    assert resolve(tiers=tiers, requested=requested)['selected'] == expected


@pytest.mark.parametrize('answer_quality,suffix,actual', [
    ('flac', 'mp3', 'mp3'),
    ('flac24bit', 'mp3', 'mp3'),
    ('320k', 'flac', 'flac'),
    ('320k', 'mp3', '320k'),
    ('flac24bit', 'flac', 'flac24bit'),
    (None, 'mp3', 'mp3'),
    (None, 'm4a', None),
    ('flac', 'm4a', None),
    ('alac', 'm4a', 'alac'),
    ('flac', 'php', 'flac'),
    (None, 'php', None),
])
def test_real_shim_reconciles_actual_quality_with_url(answer_quality, suffix, actual):
    answer = {'url': f'https://cdn.example/song.{suffix}'}
    if answer_quality is not None:
        answer['quality'] = answer_quality
    result = resolve(requested='flac', answer=answer)['resolved']
    assert result['quality'] == actual
    assert result['declared_size'] == (10000 if actual == 'flac' else None)
    # The advisory flag travels with the reference it marks: a run that kept no
    # reference must not tell the transport there is a soft one to compare.
    assert result['size_is_advisory'] == (result['declared_size'] is not None)


def test_real_shim_lossless_request_conflict_can_trigger_downgrade():
    candidate = Candidate(source_id='fixture', source_version='1', item_id='lx:kw:42',
                          title='Song', artist='Artist', qualities=('flac', '320k'))
    requested = requested_quality(candidate)
    result = resolve(requested=requested, answer={'url': 'https://cdn.example/song.mp3', 'quality': 'flac'})
    assert result['selected'] == 'flac'
    assert proven_lossy(result['resolved']['quality'])
    assert result['resolved']['quality'] == 'mp3'  # URL proves no bitrate.


def test_real_shim_search_carries_the_rows_stated_expiry():
    # Freezing a selection context keeps the instant the resolved URL dies, so
    # the row's own lifetime has to survive the search hop unchanged.
    item = {'id': '42', 'name': 'Song', 'singer': 'Artist', 'duration': 210,
            'type': 'flac', '_types': {'flac': {'size': 10000}},
            'expires_at': '2026-09-28T10:00:00Z'}
    candidates = search(item=item)['candidates']
    assert len(candidates) == 1
    assert candidates[0]['expires_at'] == '2026-09-28T10:00:00Z'


def test_real_shim_search_leaves_expiry_empty_when_the_row_states_none():
    # Inventing a lifetime would either discard a live URL or bless a dead one.
    for stated in (None, '', 1700000000, {'at': 'soon'}):
        item = {'id': '42', 'name': 'Song', 'singer': 'Artist', 'duration': 210,
                'type': 'flac', '_types': {'flac': {'size': 10000}}}
        if stated is not None:
            item['expires_at'] = stated
        candidates = search(item=item)['candidates']
        assert candidates[0]['expires_at'] is None, stated


def test_real_shim_resolve_carries_the_answers_stated_expiry():
    # The resolve hop is where the fresh URL is minted, so its lifetime has to
    # be recorded with it; a candidate context frozen without it could only
    # rediscover the deadline by failing the download.
    stated = '2026-09-28T10:00:00Z'
    result = resolve(answer={'url': 'https://cdn.example/song.flac', 'quality': 'flac',
                             'expires_at': stated})['resolved']
    assert result['expires_at'] == stated


def test_real_shim_resolve_leaves_expiry_empty_when_the_answer_states_none():
    for stated in (None, '', 1700000000, {'at': 'soon'}):
        answer = {'url': 'https://cdn.example/song.flac', 'quality': 'flac'}
        if stated is not None:
            answer['expires_at'] = stated
        assert resolve(answer=answer)['resolved']['expires_at'] is None, stated


def test_real_shim_forwards_an_explicit_lossless_request_the_source_never_declared():
    """A legal request is intent: it is asked for even with no declared tiers."""
    trace = resolve_trace(tiers=(), requested='flac',
                          answers=[{'url': 'https://cdn.example/song.flac', 'quality': 'flac'}])
    assert trace['asked'] == ['flac']
    assert trace['resolved']['quality'] == 'flac'
    assert trace['resolved']['extension'] == 'flac'


def test_real_shim_no_request_with_empty_tiers_still_uses_the_shim_default():
    trace = resolve_trace(tiers=(), answers=[{'url': 'https://cdn.example/song.mp3'}])
    assert trace['asked'] == ['320k']
    assert trace['resolved']['quality'] == 'mp3'


def test_real_shim_adopts_a_request_absent_from_the_declared_tiers():
    trace = resolve_trace(tiers=('320k',), requested='flac',
                          answers=[{'url': 'https://cdn.example/song.mp3', 'quality': '320k'}])
    assert trace['asked'] == ['flac']
    assert trace['resolved']['quality'] == '320k'


def test_real_shim_retries_once_with_the_declared_best_when_an_undeclared_request_fails():
    """One retry, at the best tier the source itself declared, and no more."""
    trace = resolve_trace(tiers=('128k', '320k'), requested='flac',
                          answers=[{}, {'url': 'https://cdn.example/song.mp3', 'quality': '320k'}])
    assert trace['asked'] == ['flac', '320k']
    assert len(trace['asked']) == 2
    # The report names the tier that actually answered, never the failed ask.
    assert trace['resolved']['quality'] == '320k'
    assert trace['resolved']['extension'] == 'mp3'
    # The declared size belongs to the tier that was really used.
    assert trace['resolved']['declared_size'] == 1000
    assert trace['resolved']['size_is_advisory'] is True


def test_real_shim_retries_a_throwing_undeclared_request_exactly_once():
    trace = resolve_trace(tiers=('320k',), requested='flac',
                          answers=[{'__throw': 'lx source threw'},
                                   {'url': 'https://cdn.example/song.mp3', 'quality': '320k'}])
    assert trace['asked'] == ['flac', '320k']
    assert trace['resolved']['quality'] == '320k'


def test_real_shim_never_retries_a_request_the_source_declared():
    trace = resolve_trace(tiers=('flac', '320k'), requested='flac', answers=[{}])
    assert trace['asked'] == ['flac']
    assert trace['resolved'] is None
    assert 'no media URL' in trace['error']


def test_real_shim_never_retries_a_declared_request_that_throws():
    trace = resolve_trace(tiers=('flac', '320k'), requested='flac', answers=[{'__throw': 'boom'}])
    assert trace['asked'] == ['flac']
    assert trace['resolved'] is None
    assert 'boom' in trace['error']
