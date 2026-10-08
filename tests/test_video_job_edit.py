"""Queued edits retain FIFO identity and inputs without racing worker startup."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from src import h3_video as h3
from src import video_job_edit as edit
from tests.test_video_submission import backend, send, image

REAL_POPEN = subprocess.Popen


def create(backend, family='h3', **kwargs):
    response = send(backend, family, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def url(job, family='h3'):
    return '/api/video/' + family + '/jobs/' + job['id']


def patch(backend, job, family='h3', *, config=None, revision=0, retain=None, files=None, owner='corey'):
    data = {'config': json.dumps(config or backend.configs[family]), 'revision': str(revision)}
    if retain is not None:
        data['retain_inputs'] = json.dumps(retain)
    return backend.client().patch(url(job, family), headers={'x-user': owner}, data=data, files=files)


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_edit_same_job_preserves_fifo_and_original_media(backend, family):
    first = create(backend, family)
    later = create(backend, family, name='next.mp4')
    manager = backend.managers[family]
    directory = manager.directory(first['id'])
    before_state = (directory / 'state.json').read_bytes()
    before_manifest = h3._read(directory / 'manifest.json')
    response = backend.client().get(url(first, family) + '/edit')
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['revision'] == 0 and draft['config']['prompt'] == backend.configs[family]['prompt']
    if family == 'h3':
        assert draft['config']['lora'] == ''
    else:
        assert draft['config']['components']['speed_lora'] == ''
    assert str(manager.root) not in response.text and '/models/' not in response.text
    field = 'reference_videos' if family == 'h3' else 'source_video'
    assert draft['inputs'][field] == [{'index': 0, 'name': 'clip01.mp4', 'size': len(b'video fixture')}]
    config = {**draft['config'], 'prompt': 'A revised prompt', 'steps': 9, 'seed': 543}
    saved = patch(backend, first, family, config=config)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result['id'] == first['id'] and result['revision'] == 1 and result['queue_position'] == 1
    assert result['created_at'] == first['created_at'] and result['total_steps'] == 9
    assert result['started_at'] is None
    assert backend.client().get(url(later, family) + '/edit').json()['revision'] == 0
    assert (directory / 'state.json').read_bytes() == before_state
    after = h3._read(directory / 'manifest.json')
    assert after[field] == before_manifest[field]
    assert after['config']['steps'] == 9 and after['config']['seed'] == 543
    assert backend.client().get(url(first, family) + '/edit').json()['config']['prompt'] == 'A revised prompt'
    assert len(backend.calls) == 2  # No new supervisor or duplicate job for an ordinary edit.


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_stale_or_running_edits_never_replace_original(backend, family):
    job = create(backend, family)
    manager = backend.managers[family]
    directory = manager.directory(job['id'])
    assert patch(backend, job, family).status_code == 200
    saved = (directory / 'manifest.json').read_bytes()
    assert patch(backend, job, family).status_code == 409
    assert (directory / 'manifest.json').read_bytes() == saved
    for status in ('running', 'completed', 'stopped', 'failed'):
        state = h3._read(directory / 'state.json')
        h3._write(directory / 'state.json', {**state, 'status': status})
        assert backend.client().get(url(job, family) + '/edit').status_code == 409
        assert patch(backend, job, family, revision=1).status_code == 409
        assert (directory / 'manifest.json').read_bytes() == saved


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_owner_family_admin_and_missing_boundaries(backend, family):
    job = create(backend, family)
    client = backend.client()
    for suffix in ('', '/edit'):
        method = client.patch if not suffix else client.get
        assert method(url(job, family) + suffix, headers={'x-user': 'other'}).status_code == 404
        assert method(url(job, family) + suffix, headers={'x-user': 'reader'}).status_code == 403
        assert method(url(job, 'bfs' if family == 'h3' else 'h3') + suffix).status_code == 404
        assert method('/api/video/' + family + '/jobs/' + 'a' * 32 + suffix).status_code == 404


def test_retained_multireferences_keep_order_append_new_and_remove_explicitly(backend):
    job = create(backend, extra=[('reference_videos', ('one.mp4', b'first', 'video/mp4')),
                               ('reference_images', ('reference.png', image(), 'image/png'))])
    directory = backend.managers['h3'].directory(job['id'])
    before = h3._read(directory / 'manifest.json')
    config = {**backend.configs['h3'], 'prompt': 'Changed'}
    retained = {'reference_videos': [1], 'reference_images': [0]}
    response = patch(backend, job, config=config, retain=retained,
                     files=[('reference_videos', ('third.mp4', b'third', 'video/mp4'))])
    assert response.status_code == 200, response.text
    after = h3._read(directory / 'manifest.json')
    assert [Path(value).read_bytes() for value in after['reference_videos']] == [b'video fixture', b'third']
    assert after['input_names']['reference_videos'] == ['clip01.mp4', 'third.mp4']
    assert after['reference_images'] == before['reference_images']
    assert not Path(before['reference_videos'][0]).exists()
    assert response.json()['source_name'] == 'third.mp4'
    assert not list((directory.parent / '.edits').iterdir())


def test_bfs_replace_identity_and_source_without_repeating_instructions(backend):
    job = create(backend, 'bfs')
    directory = backend.managers['bfs'].directory(job['id'])
    original = h3._read(directory / 'manifest.json')
    original['config'].pop('prompt_notes')  # Existing pre-editor jobs.
    h3._write(directory / 'manifest.json', original)
    draft = backend.client().get(url(job, 'bfs') + '/edit').json()['config']
    assert draft['prompt'] == backend.configs['bfs']['prompt']
    response = patch(backend, job, 'bfs', config=draft, retain={}, files=[
        ('source_video', ('replacement.mp4', b'replacement', 'video/mp4')),
        ('identity_image', ('newface.png', image(), 'image/png')),
    ])
    assert response.status_code == 200, response.text
    saved = h3._read(directory / 'manifest.json')
    assert saved['config']['prompt'] == original['config']['prompt']
    assert not Path(original['source_video']).exists() and not Path(original['identity_image']).exists()
    assert saved['source_name'] == 'replacement.mp4'


@pytest.mark.parametrize('change,retain,files', [
    ({'steps': 0}, None, None), ({'model': '/etc/passwd'}, None, None),
    ({}, {'reference_videos': [99]}, None), ({}, {'reference_videos': [True]}, None),
    ({}, {'reference_videos': [0, 0]}, None), ({}, {'/etc/passwd': []}, None),
    ({}, {}, None), ({}, None, [('reference_images', ('bad.png', b'not an image', 'image/png'))]),
])
def test_failed_validation_preserves_manifest_files_and_waiter(backend, change, retain, files):
    job = create(backend)
    directory = backend.managers['h3'].directory(job['id'])
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    response = patch(backend, job, config={**backend.configs['h3'], **change}, retain=retain, files=files)
    assert response.status_code == 400, response.text
    after = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    assert before == after and len(backend.calls) == 1


def test_parallel_saves_only_one_wins_and_upload_cleanup(backend):
    job = create(backend)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(patch, backend, job, config={**backend.configs['h3'], 'prompt': prompt},
                               files=[('reference_videos', ('extra.mp4', b'extra', 'video/mp4'))])
                   for prompt in ('Edit A', 'Edit B')]
        results = [future.result() for future in futures]
    assert sorted(response.status_code for response in results) == [200, 409]
    success = next(response.json() for response in results if response.status_code == 200)
    manager = backend.managers['h3']
    manifest = h3._read(manager.directory(job['id']) / 'manifest.json')
    assert manifest['revision'] == 1 and manifest['config']['prompt'] == success['prompt']
    assert len(manifest['reference_videos']) == 2
    assert not list((manager.root / '.edits').iterdir())
    assert len(backend.calls) == 1


def test_start_during_upload_rejects_save_and_cleans_staging(backend, monkeypatch):
    job = create(backend)
    directory = backend.managers['h3'].directory(job['id'])
    original = (directory / 'manifest.json').read_bytes()
    from routes import h3_video_routes
    store = h3_video_routes._store_uploads
    async def start_then_store(*args):
        result = await store(*args)
        with h3._locked(directory / 'job.lock'):
            state = h3._read(directory / 'state.json')
            h3._write(directory / 'state.json', {**state, 'status': 'running'})
        return result
    monkeypatch.setattr(h3_video_routes, '_store_uploads', start_then_store)
    response = patch(backend, job, files=[('reference_videos', ('extra.mp4', b'extra', 'video/mp4'))])
    assert response.status_code == 409, response.text
    assert (directory / 'manifest.json').read_bytes() == original
    assert not list((directory.parent / '.edits').iterdir())


def test_retained_and_new_bytes_share_one_upload_budget(backend, monkeypatch):
    job = create(backend)
    from routes import video_job_edit_routes
    monkeypatch.setattr(video_job_edit_routes, 'get_chat_upload_max_bytes', lambda: 15)
    response = patch(backend, job, files=[('reference_videos', ('extra.mp4', b'extra', 'video/mp4'))])
    assert response.status_code == 413, response.text
    assert backend.client().get(url(job) + '/edit').json()['revision'] == 0


def test_worker_waiting_on_job_lock_reads_updated_gpu_and_manifest(backend, monkeypatch):
    job = create(backend)
    manager = backend.managers['h3']
    directory = manager.directory(job['id'])
    seen, ready = {}, threading.Event()
    original_lock = h3._locked
    from contextlib import contextmanager
    @contextmanager
    def announce_lock(path, **kwargs):
        if threading.current_thread().name == 'test-render' and Path(path).name == 'job.lock':
            ready.set()
        with original_lock(path, **kwargs) as lease:
            yield lease
    monkeypatch.setattr(h3, '_locked', announce_lock)
    def spawn(command, **kwargs):
        seen['env'] = kwargs['env']['CUDA_VISIBLE_DEVICES']
        seen['prompt'] = h3._read(directory / 'manifest.json')['config']['prompt']
        return type('Process', (), {'pid': os.getpid(), 'returncode': 1, 'poll': lambda self: 1})()
    monkeypatch.setattr(h3.subprocess, 'Popen', spawn)
    monkeypatch.setattr(h3, 'stop_workers', lambda *args, **kwargs: True)
    with (directory.parent.parent / 'test-execution.lock').open('a') as lease:
        with original_lock(directory / 'job.lock'):
            thread = threading.Thread(target=h3._render, args=(directory / 'manifest.json', lease), name='test-render')
            thread.start()
            assert ready.wait(3)
            # The render is blocked behind edit's lock. A pre-lock read would
            # retain the old GPU even though the worker opens the new prompt.
            manifest = h3._read(directory / 'manifest.json')
            manifest['config'].update(gpu='GPU-edited', prompt='Latest prompt')
            h3._write(directory / 'manifest.json', manifest)
        thread.join(5)
    assert not thread.is_alive()
    assert seen == {'env': 'GPU-edited', 'prompt': 'Latest prompt'}


@pytest.mark.parametrize('fails_spawn', [False, True])
def test_legacy_supervisor_upgrade_is_exact_pid_and_spawn_failure_keeps_original(backend, monkeypatch, fails_spawn):
    job = create(backend)
    manager = backend.managers['h3']
    directory = manager.directory(job['id'])
    state = h3._read(directory / 'state.json')
    state.pop('supervisor_edit_version')
    state['supervisor'] = {'pid': 987654, 'start': 'old-start'}
    h3._write(directory / 'state.json', state)
    before = (directory / 'manifest.json').read_bytes()
    events = []
    monkeypatch.setattr(h3, '_snapshot', lambda: {987654: (1, 'old-start'), 987655: (1, 'new-start')})
    monkeypatch.setattr(h3, '_owned_workers', lambda _: {})
    real_read = Path.read_bytes
    def read(path):
        if str(path) == '/proc/987654/cmdline':
            return b'python\0-m\0src.h3_video\0--supervise\0' + str(directory / 'manifest.json').encode() + b'\0'
        return real_read(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    def spawn(*args, **kwargs):
        events.append('spawn')
        if fails_spawn:
            raise OSError('Synthetic spawn failure')
        return type('Process', (), {'pid': 987655, 'wait': lambda self: 0})()
    monkeypatch.setattr(h3.subprocess, 'Popen', spawn)
    monkeypatch.setattr(h3, '_stop_supervisor', lambda record: events.append(('stop', record['pid'])))
    response = patch(backend, job, config={**backend.configs['h3'], 'prompt': 'Edited legacy'})
    saved = h3._read(directory / 'state.json')
    if fails_spawn:
        assert response.status_code == 409, response.text
        assert saved == state and (directory / 'manifest.json').read_bytes() == before
        assert events == ['spawn']
    else:
        assert response.status_code == 200, response.text
        assert events == ['spawn', ('stop', 987654)]
        assert saved['supervisor'] == {'pid': 987655, 'start': 'new-start'}
        assert saved['supervisor_edit_version'] == 1
        for field in ('queue_order', 'created_at', 'status', 'id', 'owner'):
            assert saved[field] == state[field]


def test_real_legacy_waiter_upgrade_preserves_active_render_and_fifo(backend, tmp_path, monkeypatch):
    """Exercise the actual detached supervisor/process lock using tiny files."""
    monkeypatch.setattr(h3.subprocess, 'Popen', REAL_POPEN)
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'gallery.db'))
    manager = backend.managers['h3']
    manager.project = Path(h3.PROJECT_ROOT)
    manager.worker = tmp_path / 'worker.py'
    manager.worker.write_text('''import json,os,sys,time
from pathlib import Path
p=Path(sys.argv[2]); d=p.parent; m=json.loads(p.read_text())
(d/'observed.json').write_text(json.dumps({'gpu':os.environ['CUDA_VISIBLE_DEVICES'], 'prompt':m['config']['prompt']}))
deadline=time.monotonic()+15
while m['config']['prompt']=='hold' and not (d/'release').exists():
    if time.monotonic()>deadline: raise RuntimeError('Synthetic test timed out')
    time.sleep(.03)
Path(m['output_path']).write_bytes(b'fixture-MP4')
''')
    jobs = []
    def wait(predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.03)
        raise AssertionError('Synthetic queue timed out')
    try:
        backend.configs['h3']['prompt'] = 'hold'
        first = create(backend)
        jobs.append(first)
        first_dir = manager.directory(first['id'])
        wait(lambda: (first_dir / 'observed.json').exists())
        active = h3._read(first_dir / 'state.json')['worker']
        backend.configs['h3']['prompt'] = 'original queued'
        queued = create(backend)
        jobs.append(queued)
        directory = manager.directory(queued['id'])
        with h3._locked(directory / 'job.lock'):
            state = h3._read(directory / 'state.json')
            state.pop('supervisor_edit_version')
            h3._write(directory / 'state.json', state)
        old = state['supervisor']
        backend.inventories['h3']['gpus'].append({'id': 'GPU-new-selection', 'nvfp4': True})
        response = patch(backend, queued, config={**backend.configs['h3'], 'prompt': 'edited queued', 'gpu': 'GPU-new-selection'})
        assert response.status_code == 200, response.text
        upgraded = h3._read(directory / 'state.json')
        assert upgraded['supervisor']['pid'] != old['pid'] and not h3._live_record(old)
        assert h3._live_record(active) and not (directory / 'observed.json').exists()
        assert upgraded['queue_order'] == state['queue_order'] and upgraded['created_at'] == state['created_at']
        (first_dir / 'release').touch()
        wait(lambda: h3._read(directory / 'state.json').get('status') not in h3.ACTIVE)
        observed = h3._read(directory / 'observed.json')
        assert observed == {'gpu': 'GPU-new-selection', 'prompt': 'edited queued'}
        assert h3._read(directory / 'state.json')['status'] == 'completed'
        assert h3._read(first_dir / 'state.json')['finished_at'] <= h3._read(directory / 'state.json')['started_at']
    finally:
        for job in jobs:
            manager.cancel(job['id'], 'corey')
