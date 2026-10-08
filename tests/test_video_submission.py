"""Bulk upload retries enqueue each owner/family submission at most once."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import shutil
import struct
import threading
import time
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from routes.h3_video_routes import setup_h3_video_routes
from routes.bfs_video_routes import setup_bfs_video_routes
from src import h3_video as h3, bfs_video as bfs
from src.video_submission import HEADER, launch_job, submission


def image():
    data = io.BytesIO()
    Image.new('RGB', (16, 16), 'blue').save(data, 'PNG')
    return data.getvalue()


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    model_dir = tmp_path / 'models'
    model_dir.mkdir()
    for name in ['minimax_h3_ref2va_nvfp4.safetensors', 'qwen3vl_32b_minimax_h3_nvfp4.safetensors',
                 'minimax_h3_video_vae.safetensors', 'minimax_h3_audio_vae.safetensors',
                 'minimax_h3_head_swap_v1.0_r32.safetensors']:
        key = 'model.lora_A.weight' if 'head_swap' in name else 'model.weight'
        header = json.dumps({key: {'dtype': 'F32', 'shape': [1], 'data_offsets': [0, 4]}}).encode()
        (model_dir / name).write_bytes(struct.pack('<Q', len(header)) + header + b'0000')
    calls = []
    def spawn(*args, **kwargs):
        calls.append(args)
        # A real process identity keeps the queued receipt live. No subprocess
        # or model worker is created; the stub wait immediately returns.
        return SimpleNamespace(pid=os.getpid(), wait=lambda: 0)
    monkeypatch.setattr(h3.subprocess, 'Popen', spawn)
    managers, inventories, configs = {}, {}, {}
    for family, cls, discover in [('h3', h3.H3JobManager, h3.discover_components), ('bfs', bfs.BFSJobManager, bfs.discover_components)]:
        manager = cls(tmp_path / 'jobs' / family, project=tmp_path, gallery_directory=tmp_path / 'gallery')
        components = discover([model_dir])
        current = {'components': components, 'gpus': [{'id': 'GPU-00000000-0000-0000-0000-000000000001', 'nvfp4': True}],
                   'runtime_ready': True, 'runtime_error': '', 'defaults': dict(h3.DEFAULTS)}
        manager.inventory = lambda current=current: current
        if family == 'h3':
            config = {**h3.DEFAULTS, 'gpu': current['gpus'][0]['id'], 'mode': 'ref2va', 'prompt': 'Same exact prompt.'}
            config.update({role: next(c['id'] for c in components if c['role'] == role)
                           for role in ['model', 'encoder', 'video_vae', 'audio_vae']})
        else:
            workflow = bfs.BY_ID['h3_head_swap']
            config = {'workflow_id': workflow['id'], 'gpu': current['gpus'][0]['id'], 'prompt': 'Same exact prompt.',
                      'components': {slot['key']: next(c['id'] for c in components if bfs.matches(c, slot, workflow))
                                     for slot in workflow['slots'] if slot['required']}}
        managers[family], inventories[family], configs[family] = manager, current, config
    def client():
        app = FastAPI()
        app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user in {'corey', 'other'})
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.current_user = request.headers.get('x-user', 'corey')
            return await call_next(request)
        app.include_router(setup_h3_video_routes(managers['h3']))
        app.include_router(setup_bfs_video_routes(managers['bfs']))
        return TestClient(app)
    return SimpleNamespace(client=client, managers=managers, inventories=inventories, configs=configs, calls=calls)


def send(backend, family, key=None, *, name='clip01.mp4', owner='corey', video=b'video fixture', extra=None):
    field = 'reference_videos' if family == 'h3' else 'source_video'
    files = [(field, (name, video, 'video/mp4'))]
    if family == 'bfs':
        files.insert(0, ('identity_image', ('head.png', image(), 'image/png')))
    if extra:
        files = extra + files
    return backend.client().post('/api/video/' + family + '/jobs',
        headers={'x-user': owner, **({HEADER: key} if key is not None else {})},
        data={'config': json.dumps(backend.configs[family])}, files=files)


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_retry_after_lost_response_and_restart_returns_original_snapshot(backend, family):
    key = str(uuid.uuid4())
    first = send(backend, family, key, name='../private/clip01.mp4')
    assert first.status_code == 201, first.text
    job = first.json()
    assert job['source_name'] == 'clip01.mp4' and job['submission_id'] == uuid.UUID(key).hex
    manager = backend.managers[family]
    manifest = h3._read(manager.directory(job['id']) / 'manifest.json')
    assert manifest['source_name'] == 'clip01.mp4'
    assert manifest['input_names']['reference_videos' if family == 'h3' else 'source_video'] == ['clip01.mp4']
    # Simulate a new web process with unavailable inventory. Retry receipts must
    # not depend on the old process or resend/revalidate a multi-GB upload.
    fresh = type(manager)(manager.root, project=manager.project, gallery_directory=manager.gallery_directory)
    fresh.inventory = lambda: pytest.fail('A duplicate request must not rescan inventory')
    backend.managers[family] = fresh
    retry = backend.client().post('/api/video/' + family + '/jobs', headers={HEADER: uuid.UUID(key).hex})
    assert retry.status_code == 201, retry.text
    assert retry.json()['id'] == job['id'] and retry.json()['prompt'] == job['prompt']
    assert len(backend.calls) == 1


def test_same_key_is_independent_per_owner_and_family(backend):
    key = str(uuid.uuid4())
    rows = [send(backend, family, key, owner=owner) for family, owner in [('h3', 'corey'), ('h3', 'other'), ('bfs', 'corey')]]
    assert all(row.status_code == 201 for row in rows), [row.text for row in rows]
    assert len({row.json()['id'] for row in rows}) == 3 and len(backend.calls) == 3
    visible = backend.client().get('/api/video/h3/jobs').json()['jobs']
    assert [row['id'] for row in visible] == [rows[0].json()['id']]


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_concurrent_duplicate_requests_only_publish_one_job(backend, family):
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(backend, family, key), range(2)))
    assert all(row.status_code == 201 for row in results), [row.text for row in results]
    assert len({row.json()['id'] for row in results}) == 1 and len(backend.calls) == 1


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_failed_staging_can_retry_but_deleted_job_cannot_restart(backend, family):
    key = str(uuid.uuid4())
    failed = send(backend, family, key, video=b'')
    assert failed.status_code == 400
    manager = backend.managers[family]
    assert not list((manager.root / '.submissions').glob('*.json'))
    accepted = send(backend, family, key)
    assert accepted.status_code == 201, accepted.text
    shutil.rmtree(manager.directory(accepted.json()['id']))
    retry = send(backend, family, key)
    assert retry.status_code == 409 and 'deleted' in retry.text
    assert len(backend.calls) == 1


def test_invalid_keys_and_nonadmin_never_scan_or_stage(backend):
    for key in ['../outside', 'x' * 1000, '']:
        response = send(backend, 'h3', key)
        assert response.status_code == 400
    assert send(backend, 'h3', str(uuid.uuid4()), owner='reader').status_code == 403
    assert not backend.managers['h3'].root.exists() and not backend.calls


def test_no_key_preserves_multiple_reference_videos_and_new_job_semantics(backend):
    extra = [('reference_videos', ('shared.mp4', b'shared video', 'video/mp4'))]
    first = send(backend, 'h3', extra=extra)
    second = send(backend, 'h3', extra=extra)
    assert first.status_code == second.status_code == 201
    assert first.json()['id'] != second.json()['id']
    manifest = h3._read(backend.managers['h3'].directory(first.json()['id']) / 'manifest.json')
    assert len(manifest['reference_videos']) == 2
    assert manifest['input_names']['reference_videos'] == ['shared.mp4', 'clip01.mp4']


def test_200_distinct_files_use_one_inventory_scan_and_keep_labels(backend):
    manager = backend.managers['h3']
    scans = []
    manager.inventory = lambda: scans.append(True) or backend.inventories['h3']
    started = time.monotonic()
    ids = set()
    with backend.client() as client:
        for index in range(200):
            key = str(uuid.uuid4())
            result = client.post('/api/video/h3/jobs', headers={HEADER: key},
                data={'config': json.dumps(backend.configs['h3'])},
                files={'reference_videos': (f'clip{index:03d}.mp4', b'video', 'video/mp4')})
            assert result.status_code == 201, result.text
            assert result.json()['source_name'] == f'clip{index:03d}.mp4'
            ids.add(result.json()['id'])
    assert len(ids) == len(backend.calls) == 200
    assert len(scans) <= 1 + int((time.monotonic() - started) / 10)
    assert len(list((manager.root / '.submissions').glob('lock-*'))) <= 64


def test_cancelled_launch_holds_retry_lock_until_receipt_is_published(tmp_path):
    manager = h3.H3JobManager(tmp_path / 'jobs')
    started, release = threading.Event(), threading.Event()
    key = uuid.uuid4().hex
    def launch(directory, owner, config, uploads):
        started.set()
        assert release.wait(3)
        h3._write(directory / 'state.json', {'id': directory.name, 'owner': owner, 'status': 'completed', 'created_at': time.time()})
        return {'id': directory.name}
    manager.launch = launch
    async def scenario():
        async def first():
            async with submission(manager, 'corey', 'h3', key) as pending:
                directory = pending.stage()
                await launch_job(manager, directory, 'corey', {}, {})
        task = asyncio.create_task(first())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        entered = asyncio.Event()
        async def retry():
            async with submission(manager, 'corey', 'h3', key) as pending:
                entered.set()
                return pending.existing()
        duplicate = asyncio.create_task(retry())
        await asyncio.sleep(.1)
        assert not entered.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await duplicate
    result = asyncio.run(scenario())
    assert result['status'] == 'completed' and manager.directory(result['id']).exists()


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_upload_stops_before_using_disk_reserve_and_cleans_receipt(backend, monkeypatch, family):
    monkeypatch.setattr('src.video_submission.shutil.disk_usage', lambda _: SimpleNamespace(free=512 * 1024 * 1024 + 1))
    response = send(backend, family, str(uuid.uuid4()))
    assert response.status_code == 507 and 'disk space' in response.text
    manager = backend.managers[family]
    assert not backend.calls
    assert not list((manager.root / '.submissions').glob('*.json'))
    assert not [path for path in manager.root.iterdir() if h3.JOB_ID.fullmatch(path.name)]


def test_upload_rechecks_free_space_each_chunk(tmp_path, monkeypatch):
    from src.video_submission import write_upload_chunk, UploadSpaceError
    checks = []
    def free(_):
        checks.append(True)
        return SimpleNamespace(free=512 * 1024 * 1024 + (2 * 1024 * 1024 if len(checks) == 1 else 0))
    monkeypatch.setattr('src.video_submission.shutil.disk_usage', free)
    with (tmp_path / 'video.mp4').open('wb') as target:
        write_upload_chunk(target, tmp_path, b'v' * 1024 * 1024)
        with pytest.raises(UploadSpaceError):
            write_upload_chunk(target, tmp_path, b'v' * 1024 * 1024)
    assert len(checks) == 2 and (tmp_path / 'video.mp4').stat().st_size == 1024 * 1024


def test_submission_inventory_refreshes_after_its_short_cache_expires(backend):
    manager = backend.managers['h3']
    scans = []
    manager.inventory = lambda: scans.append(True) or backend.inventories['h3']
    assert manager.submission_inventory() is manager.submission_inventory()
    assert len(scans) == 1
    manager._submission_inventory_cache = (0, backend.inventories['h3'])
    manager.submission_inventory()
    assert len(scans) == 2


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_inventory_advertises_batch_retry_capability(tmp_path, monkeypatch, family):
    module = h3 if family == 'h3' else bfs
    monkeypatch.setattr(module, 'discover_components', lambda roots: [])
    monkeypatch.setattr(module, 'cache_roots', lambda project: [])
    monkeypatch.setattr(module, 'gpu_inventory', lambda: [])
    monkeypatch.setattr(module, 'runtime_error', lambda *args: '')
    if family == 'bfs':
        monkeypatch.setattr(module, 'helper_error', lambda *args: '')
    manager_type = h3.H3JobManager if family == 'h3' else bfs.BFSJobManager
    manager = manager_type(tmp_path / family, project=tmp_path)
    assert manager.inventory()['batch_jobs'] is True
