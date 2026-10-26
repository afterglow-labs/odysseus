import json
import uuid
from pathlib import Path

import pytest

from src import h3_video as h3
from tests.test_video_submission import backend, send
from tests.test_video_queue_bulk import request


@pytest.mark.parametrize('family', ['h3', 'bfs'])
@pytest.mark.parametrize('status', ['failed', 'stopped'])
def test_failed_retry_keeps_identity_inputs_batch_and_resumes_once(backend, family, status):
    batch = {'id': uuid.uuid4().hex, 'name': 'Three videos', 'count': 3}
    jobs = [send(backend, family, name=f'clip-{i}.mp4', extra=[('batch', (None, json.dumps(batch)))]).json() for i in range(3)]
    manager = backend.managers[family]
    job = jobs[1]; folder = manager.directory(job['id'])
    state = h3._read(folder / 'state.json')
    h3._write(folder / 'state.json', {**state, 'status': status, 'error': 'old failure', 'supervisor': None, 'worker': None})
    for name in ['progress.json', 'candidates.json', 'scout.json']:
        h3._write(folder / name, {'old': True})
    (folder / 'render.log').write_text('old traceback')
    (folder / 'cancel').touch()
    original = h3._read(folder / 'manifest.json')
    input_paths = [p for field in original['input_names'] for p in (original[field] if isinstance(original[field], list) else [original[field]])]
    media = {p: Path(p).read_bytes() for p in input_paths}
    key = str(uuid.uuid4())
    result = request(backend, 'rerun', [job], family=family, key=key)
    assert result.status_code == 200, result.text
    assert result.json()['failed'] == 0, result.text
    retried = result.json()['jobs'][0]
    assert retried['id'] == retried['source_id'] == job['id'] and retried['retried']
    saved = h3._read(folder / 'manifest.json'); state = h3._read(folder / 'state.json')
    assert saved['batch'] == batch and saved['config'] == original['config']
    assert saved['revision'] == 1
    assert state['status'] == 'queued' and state['error'] is None
    assert {p: Path(p).read_bytes() for p in media} == media
    assert not (folder / 'cancel').exists() and not (folder / 'progress.json').exists()
    assert (folder / 'render.log').read_text() == ''
    repeat = request(backend, 'rerun', [job], family=family, key=key)
    assert repeat.json()['jobs'] == result.json()['jobs']
    assert len(backend.calls) == 4
    history = backend.client().get(f'/api/video/{family}/jobs?page=1&group_batches=true').json()
    assert history['pagination']['total'] == 1 and history['jobs'][0]['batch_count'] == 3
    children = backend.client().get(f'/api/video/{family}/batches/{batch["id"]}').json()
    assert {item['id'] for item in children['jobs']} == {item['id'] for item in jobs}
    # A second request with the obsolete revision cannot start another attempt.
    conflict = request(backend, 'rerun', [job], family=family)
    assert conflict.status_code == 409
    assert len(backend.calls) == 4


def test_active_filter_applies_before_paging_and_batch_members(backend):
    m = backend.managers['h3']; batch = {'id': uuid.uuid4().hex, 'name': 'Mixed states', 'count': 3}
    jobs = [send(backend, 'h3', extra=[('batch', (None, json.dumps(batch)))]).json() for _ in range(3)]
    for job, status in zip(jobs, ['completed', 'queued', 'failed']):
        file = m.directory(job['id']) / 'state.json'; h3._write(file, {**h3._read(file), 'status': status})
    for _ in range(12):
        job = send(backend, 'h3').json(); file = m.directory(job['id']) / 'state.json'
        h3._write(file, {**h3._read(file), 'status': 'failed'})
    client = backend.client()
    listing = client.get('/api/video/h3/jobs?page=1&page_size=1&group_batches=true&active_only=true').json()
    assert listing['pagination']['total'] == 1
    assert listing['jobs'][0]['id'] == 'batch-' + batch['id']
    detail = client.get(f'/api/video/h3/batches/{batch["id"]}?active_only=true').json()
    assert [j['id'] for j in detail['jobs']] == [jobs[1]['id']]
    assert detail['pagination']['total'] == 1
    assert client.get('/api/video/h3/jobs?active_only=maybe').status_code == 400
    assert client.get('/api/video/h3/jobs?active_only=true', headers={'x-user': 'other'}).json()['jobs'] == []


def test_retry_spawn_failure_does_not_publish_a_queued_job(backend, monkeypatch):
    job = send(backend, 'h3').json(); manager = backend.managers['h3']; folder = manager.directory(job['id'])
    state = {**h3._read(folder/'state.json'), 'status':'failed', 'supervisor':None}
    h3._write(folder/'state.json', state)
    manifest = (folder/'manifest.json').read_bytes()
    spawn = h3.subprocess.Popen
    def fail(*args, **kwargs): raise OSError('Cannot spawn')
    monkeypatch.setattr(h3.subprocess, 'Popen', fail)
    key = str(uuid.uuid4())
    result = request(backend, 'rerun', [job], key=key).json()
    assert result['failed'] == 1 and not result['jobs']
    assert h3._read(folder/'state.json') == state and (folder/'manifest.json').read_bytes() == manifest
    monkeypatch.setattr(h3.subprocess, 'Popen', spawn)
    assert request(backend, 'rerun', [job], key=key).json()['succeeded'] == 1


def test_retry_recovers_partial_manifest_and_old_receipt_never_restarts_later_attempt(backend):
    from src.video_job_retry import retry_job
    job = send(backend, 'h3').json(); manager = backend.managers['h3']; folder = manager.directory(job['id'])
    h3._write(folder/'state.json', {**h3._read(folder/'state.json'), 'status':'failed', 'supervisor':None})
    manifest = h3._read(folder/'manifest.json'); key = uuid.uuid4().hex
    h3._write(folder/'manifest.json', {**manifest, 'revision':1, 'retry_request_id':key})
    retry_job(manager, 'corey', job['id'], manifest['config'], 0, key)
    assert h3._read(folder/'state.json')['status'] == 'queued'
    assert h3._read(folder/'manifest.json')['revision'] == 1
    retry_job(manager, 'corey', job['id'], manifest['config'], 0, key)
    assert len(backend.calls) == 2
