"""Bulk changes retain per-job media and cannot duplicate accepted reruns."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import uuid

import pytest

from src import h3_video as h3, video_queue_bulk as bulk
from tests.test_video_submission import backend, send, image


def create(backend, family='h3', **kwargs):
    response = send(backend, family, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def selected(*jobs):
    return [{'id': job['id'], 'revision': job['revision']} for job in jobs]


def request(backend, action, jobs, patch=None, *, family='h3', key=None, owner='corey'):
    payload = {'jobs': selected(*jobs), 'patch': patch or {}}
    if action == 'rerun':
        payload['request_id'] = key or str(uuid.uuid4())
    return backend.client().post(f'/api/video/{family}/queue/{action}', headers={'x-user': owner}, json=payload)


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_bulk_edit_preserves_fifo_all_inputs_and_each_prompt(backend, family):
    first, second = create(backend, family), create(backend, family, name='next.mp4')
    manager = backend.managers[family]
    directories = [manager.directory(job['id']) for job in (first, second)]
    before = [h3._read(path / 'manifest.json') for path in directories]
    states = [(path / 'state.json').read_bytes() for path in directories]
    response = request(backend, 'edit', [second, first], {'steps': 9, 'seed': 123}, family=family)
    assert response.status_code == 200, response.text
    assert response.json()['updated'] == 2
    assert [row['id'] for row in response.json()['jobs']] == [first['id'], second['id']]
    for index, path in enumerate(directories):
        manifest = h3._read(path / 'manifest.json')
        assert manifest == {**before[index], 'revision': 1, 'config': {**before[index]['config'], 'steps': 9, 'seed': 123}}
        assert (path / 'state.json').read_bytes() == states[index]
    assert len(backend.calls) == 2


@pytest.mark.parametrize('problem', ['stale', 'running', 'other-owner', 'bad-patch', 'mode', 'invalid-input'])
def test_all_jobs_validate_before_any_bulk_edit(backend, problem):
    jobs = [create(backend), create(backend)]
    manager = backend.managers['h3']
    second = manager.directory(jobs[1]['id'])
    patch, expected = {'steps': 8}, 409
    if problem == 'stale':
        manifest = h3._read(second / 'manifest.json')
        h3._write(second / 'manifest.json', {**manifest, 'revision': 1})
    elif problem in {'running', 'other-owner'}:
        state = h3._read(second / 'state.json')
        state.update({'status': 'running'} if problem == 'running' else {'owner': 'other'})
        h3._write(second / 'state.json', state)
        expected = 404 if problem == 'other-owner' else 409
    elif problem in {'bad-patch', 'mode'}:
        patch = {'steps': 0} if problem == 'bad-patch' else {'mode': 't2va'}
        expected = 400
    elif problem == 'invalid-input':
        manifest = h3._read(second / 'manifest.json')
        manifest['reference_videos'] = ['/etc/passwd']
        h3._write(second / 'manifest.json', manifest)
        expected = 400
    before = [(manager.directory(job['id']) / 'manifest.json').read_bytes() for job in jobs]
    response = request(backend, 'edit', jobs, patch)
    assert response.status_code == expected, response.text
    assert before == [(manager.directory(job['id']) / 'manifest.json').read_bytes() for job in jobs]


def test_concurrent_bulk_edits_have_one_revision_winner(backend):
    jobs = [create(backend), create(backend)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda steps: request(backend, 'edit', jobs, {'steps': steps}), [8, 12]))
    assert sorted(result.status_code for result in results) == [200, 409]
    saved = [h3._read(backend.managers['h3'].directory(job['id']) / 'manifest.json') for job in jobs]
    assert saved[0]['config']['steps'] == saved[1]['config']['steps'] and all(row['revision'] == 1 for row in saved)


def test_lightweight_snapshots_filter_owner_status_and_hide_paths(backend):
    first, second = create(backend), create(backend)
    create(backend, owner='other')
    directory = backend.managers['h3'].directory(first['id'])
    state = h3._read(directory / 'state.json')
    h3._write(directory / 'state.json', {**state, 'status': 'completed'})
    client = backend.client()
    for status, ids in [('queued', [second['id']]), ('finished', [first['id']]), ('all', [first['id'], second['id']])]:
        response = client.get('/api/video/h3/queue/jobs?status=' + status)
        assert response.status_code == 200, response.text
        assert [job['id'] for job in response.json()['jobs']] == ids
        assert 'log_tail' not in response.text and str(backend.managers['h3'].root.parent) not in response.text
        assert all(job['config']['model'] == backend.configs['h3']['model'] for job in response.json()['jobs'])
        assert 'queue' in response.json()


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_rerun_changed_settings_clones_independent_inputs_and_preserves_originals(backend, family):
    extras = [('reference_videos', ('shared.mp4', b'shared', 'video/mp4')),
              ('reference_images', ('still.png', image(), 'image/png'))] if family == 'h3' else None
    jobs = [create(backend, family, extra=extras), create(backend, family, name='next.mp4')]
    manager = backend.managers[family]
    before = [(manager.directory(job['id']) / 'manifest.json').read_bytes() for job in jobs]
    key = str(uuid.uuid4())
    response = request(backend, 'rerun', list(reversed(jobs)), {'steps': 9, 'prompt': 'Updated prompt'}, family=family, key=key)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['rerun'] == 2 and result['failed'] == 0
    assert [row['source_id'] for row in result['jobs']] == [job['id'] for job in jobs]
    assert not set(job['id'] for job in jobs) & set(job['id'] for job in result['jobs'])
    assert before == [(manager.directory(job['id']) / 'manifest.json').read_bytes() for job in jobs]
    for index, row in enumerate(result['jobs']):
        old, new = json.loads(before[index]), h3._read(manager.directory(row['id']) / 'manifest.json')
        assert new['config']['steps'] == 9
        assert 'Updated prompt' in new['config']['prompt']
        assert new['input_names'] == old['input_names'] and new['source_name'] == old['source_name']
        field = 'reference_videos' if family == 'h3' else 'source_video'
        original_path = Path(old[field][0] if family == 'h3' else old[field])
        clone_path = Path(new[field][0] if family == 'h3' else new[field])
        assert original_path != clone_path and original_path.stat().st_ino == clone_path.stat().st_ino
        original_path.unlink()
        assert clone_path.read_bytes()
    retry = request(backend, 'rerun', list(reversed(jobs)), {'steps': 9, 'prompt': 'Updated prompt'}, family=family, key=key)
    assert retry.status_code == 200 and retry.json()['jobs'] == result['jobs']
    assert len(backend.calls) == 4


def test_concurrent_rerun_requests_create_one_copy_and_reject_changed_payload(backend):
    job, key = create(backend), str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: request(backend, 'rerun', [job], {'steps': 8}, key=key), range(2)))
    assert all(result.status_code == 200 for result in results), [result.text for result in results]
    assert results[0].json()['jobs'] == results[1].json()['jobs'] and len(backend.calls) == 2
    assert request(backend, 'rerun', [job], {'steps': 9}, key=key).status_code == 409
    new_id = results[0].json()['jobs'][0]['id']
    shutil.rmtree(backend.managers['h3'].directory(new_id))
    retry = request(backend, 'rerun', [job], {'steps': 8}, key=key)
    assert retry.status_code == 200 and retry.json()['failed'] == 1 and retry.json()['rerun'] == 0
    assert not backend.managers['h3'].directory(new_id).exists() and len(backend.calls) == 2


def test_failed_rerun_validation_publishes_no_jobs_or_copies(backend):
    jobs = [create(backend), create(backend)]
    manager = backend.managers['h3']
    before = {path.name for path in manager.root.iterdir() if h3.JOB_ID.fullmatch(path.name)}
    response = request(backend, 'rerun', jobs, {'steps': -1})
    assert response.status_code == 400
    assert {path.name for path in manager.root.iterdir() if h3.JOB_ID.fullmatch(path.name)} == before
    assert len(backend.calls) == 2


def test_partial_rerun_failure_retries_only_unaccepted_copies(backend, monkeypatch):
    jobs = [create(backend), create(backend)]
    manager, key = backend.managers['h3'], str(uuid.uuid4())
    launch = manager.launch
    attempts = []
    def launch_once_failure(*args):
        attempts.append(args[0].name)
        if len(attempts) == 2:
            raise RuntimeError('Disk temporarily unavailable')
        return launch(*args)
    monkeypatch.setattr(manager, 'launch', launch_once_failure)
    first = request(backend, 'rerun', jobs, {'steps': 8}, key=key)
    assert first.status_code == 200 and first.json()['rerun'] == 1 and first.json()['failed'] == 1
    retry = request(backend, 'rerun', jobs, {'steps': 8}, key=key)
    assert retry.status_code == 200 and retry.json()['rerun'] == 2 and retry.json()['failed'] == 0
    assert len(attempts) == 3 and attempts[1] == attempts[2] and len(backend.calls) == 4


def test_deleted_immediately_after_rerun_launch_reports_error_and_keeps_tombstone(backend, monkeypatch):
    job, key = create(backend), str(uuid.uuid4())
    manager = backend.managers['h3']
    launch = manager.launch
    deleted = []
    def launch_then_delete(directory, *args):
        result = launch(directory, *args)
        deleted.append(directory)
        shutil.rmtree(directory)
        return result
    monkeypatch.setattr(manager, 'launch', launch_then_delete)
    response = request(backend, 'rerun', [job], {'steps': 8}, key=key)
    assert response.status_code == 200, response.text
    assert response.json()['succeeded'] == 0 and response.json()['failed'] == 1
    assert response.json()['jobs'] == [] and 'deleted' in response.json()['errors'][0]['error']
    retry = request(backend, 'rerun', [job], {'steps': 8}, key=key)
    assert retry.status_code == 200 and retry.json()['failed'] == 1 and retry.json()['succeeded'] == 0
    assert len(deleted) == 1 and not deleted[0].exists() and len(backend.calls) == 2


def test_bulk_bfs_component_patch_merges_other_roles(backend):
    job = create(backend, 'bfs')
    directory = backend.managers['bfs'].directory(job['id'])
    before = h3._read(directory / 'manifest.json')
    response = request(backend, 'edit', [job], {'components': {'speed_lora': ''}}, family='bfs')
    assert response.status_code == 200, response.text
    assert h3._read(directory / 'manifest.json')['config'] == before['config']


def test_bulk_route_guards_and_request_bounds(backend):
    job = create(backend)
    assert request(backend, 'edit', [job], {'steps': 8}, owner='reader').status_code == 403
    assert request(backend, 'rerun', [job], owner='other').status_code == 404
    client = backend.client()
    for payload in [{'jobs': []}, {'jobs': selected(job) * 2, 'patch': {'steps': 8}},
                    {'jobs': [{'id': job['id'], 'revision': True}], 'patch': {'steps': 8}}]:
        assert client.post('/api/video/h3/queue/edit', json=payload).status_code == 400
    assert client.post('/api/video/h3/queue/edit', content=b' ' * (512 * 1024 + 1)).status_code == 413
    assert client.post('/api/video/h3/queue/pause', json={'paused': 'true'}).status_code == 400
    assert client.post('/api/video/h3/queue/cancel', json={'scope': {}}).status_code == 400


@pytest.mark.parametrize('status', ['running', 'completed', 'failed', 'stopped'])
def test_rerun_accepts_saved_jobs_in_any_state_without_changing_original(backend, status):
    job = create(backend)
    directory = backend.managers['h3'].directory(job['id'])
    state = h3._read(directory / 'state.json')
    h3._write(directory / 'state.json', {**state, 'status': status})
    (directory / 'output.mp4').write_bytes(b'original output')
    before = (directory / 'state.json').read_bytes()
    response = request(backend, 'rerun', [job], {'steps': 8})
    assert response.status_code == 200 and response.json()['rerun'] == 1
    assert (directory / 'state.json').read_bytes() == before
    assert (directory / 'output.mp4').read_bytes() == b'original output'


def test_bulk_edit_200_saved_jobs_scans_inventory_once_and_keeps_labels(backend):
    jobs = [create(backend, name=f'clip{index:03d}.mp4') for index in range(200)]
    manager, scans = backend.managers['h3'], []
    manager.inventory = lambda: scans.append(True) or backend.inventories['h3']
    response = request(backend, 'edit', jobs, {'steps': 8, 'seed': 123})
    assert response.status_code == 200, response.text
    assert response.json()['updated'] == 200 and scans == [True]
    assert [row['source_name'] for row in response.json()['jobs']] == [f'clip{index:03d}.mp4' for index in range(200)]
    assert all(row['config']['steps'] == 8 and row['revision'] == 1 for row in response.json()['jobs'])
    assert len(backend.calls) == 200
