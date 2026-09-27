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


@pytest.mark.parametrize('tiers,requested,expected', [
    (('flac', '320k'), None, '320k'),
    (('flac24bit', 'flac'), None, 'flac24bit'),
    ((), None, '320k'),
    (('flac', '320k'), 'flac', 'flac'),
    (('flac', '320k'), '320k', '320k'),
    (('flac', '320k'), 'unavailable', 'flac'),
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
