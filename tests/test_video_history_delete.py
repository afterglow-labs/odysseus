"""Deletion uses reviewed snapshots, real Gallery cleanup, and owned files only."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core import database
from routes.h3_video_routes import setup_h3_video_routes
from routes.bfs_video_routes import setup_bfs_video_routes
from src import h3_video as h3, video_history_delete as history, video_preview_proxy as proxy
from src.bfs_video import BFSJobManager
from tests.test_h3_gpu_delete import db, job, gallery
from tests.test_video_queue import queue, wait_for


def client_for(managers):
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user in {'corey', 'other'})
    @app.middleware('http')
    async def identify(request, call_next):
        request.state.current_user = request.headers.get('x-user', 'corey')
        return await call_next(request)
    for manager in managers:
        app.include_router(setup_h3_video_routes(manager) if manager.root.name == 'h3' else setup_bfs_video_routes(manager))
    return TestClient(app)


@pytest.fixture
def managers(tmp_path, monkeypatch, db):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    monkeypatch.setattr(proxy, 'CACHE_ROOT', tmp_path / 'cache')
    return [h3.H3JobManager(tmp_path / 'jobs' / 'h3', gallery_directory=tmp_path / 'gallery'),
            BFSJobManager(tmp_path / 'jobs' / 'bfs', gallery_directory=tmp_path / 'gallery')]


def save(manager, stage=None, **kwargs):
    directory, state = job(manager, **kwargs)
    if stage:
        manifest = h3._read(directory / 'manifest.json')
        manifest['config'].update(recipe='seed_hunter', seed_hunter={'stage': stage})
        h3._write(directory / 'manifest.json', manifest)
    return directory, state


def test_existing_gallery_migration_shares_only_identical_owned_completed_outputs(managers, db):
    from scripts.dedupe_video_gallery import deduplicate
    manager = managers[0]
    identical, first = save(manager)
    output = gallery(db, manager, first)
    output.write_bytes((identical / 'output.mp4').read_bytes())
    edited, second = save(manager)
    edited_output = gallery(db, manager, second)
    active, third = save(manager, status='running')
    active_output = gallery(db, manager, third)
    active_output.write_bytes((active / 'output.mp4').read_bytes())
    assert deduplicate(manager) == {'linked_videos': 1, 'bytes_reclaimed': 5}
    assert output.samefile(identical / 'output.mp4')
    assert edited_output.read_bytes() == b'gallery-video'
    assert not active_output.samefile(active / 'output.mp4')
    assert deduplicate(manager)['linked_videos'] == 0
    manager.delete(first['id'], first['owner'])
    assert not output.exists() and not identical.exists()


def test_history_scope_counts_every_page_and_preserves_active_other_workflows_owners(managers):
    main, bfs = managers
    for i in range(37):
        save(main, status=['completed', 'failed', 'stopped'][i % 3])
    save(main, stage='preview')
    save(main, stage='final')
    save(main, stage='scout')
    save(bfs)
    for status in ('running', 'queued'):
        save(main, status=status)
    save(main, owner='other')
    with client_for(managers) as client:
        preview = client.post('/api/video/h3/history/delete-preview', json={'scope': 'workflow'}).json()
        assert preview['count'] == 37 and preview['preserved_active'] == 2
        assert preview['counts'] == {'completed': 13, 'failed': 12, 'stopped': 12}
        identifier = preview['snapshot_id']
        late, _ = save(main)
        replies = []
        while not replies or not replies[-1]['done']:
            response = client.post('/api/video/h3/history/delete', json={'snapshot_id': identifier, 'confirm': True})
            assert response.status_code == 200, response.text
            replies.append(response.json())
        assert [row['processed'] for row in replies] == [10, 20, 30, 37]
        assert replies[-1]['deleted'] == 37 and not replies[-1]['errors']
        assert client.post('/api/video/h3/history/delete', json={'snapshot_id': identifier, 'confirm': True}).json() == replies[-1]
        assert late.exists()
        for body, expected in [({'scope': 'workflow', 'recipe': 'seed_hunter', 'stage': 'not_scout'}, 2),
                               ({'scope': 'workflow', 'recipe': 'seed_hunter', 'stage': 'scout'}, 1),
                               ({'scope': 'all'}, 5)]:
            assert client.post('/api/video/h3/history/delete-preview', json=body).json()['count'] == expected


def test_snapshot_guards_revision_active_state_owner_and_newly_finished_jobs(managers):
    manager = managers[0]
    edited, _ = save(manager)
    active, _ = save(manager)
    removed, _ = save(manager)
    other, _ = save(manager)
    preview = history.preview(manager, 'corey')
    h3._write(edited / 'manifest.json', {'revision': 1, 'config': {}})
    h3._write(active / 'state.json', h3._read(active / 'state.json') | {'status': 'queued'})
    h3._write(other / 'state.json', h3._read(other / 'state.json') | {'owner': 'other'})
    manager.delete(removed.name, 'corey')
    reply = history.delete(manager, 'corey', preview['snapshot_id'], confirm=True)
    assert reply['deleted'] == 0 and reply['skipped'] == 4 and not reply['errors']
    assert edited.exists() and active.exists() and other.exists()


def test_partial_failure_retry_has_durable_receipt_and_keeps_submission_tombstones(managers, monkeypatch):
    manager = managers[0]
    good, _ = save(manager)
    bad, _ = save(manager)
    receipts = manager.root / '.submissions'
    receipts.mkdir()
    (receipts / (bad.name + '.json')).write_text('deduplicate accepted submission')
    before = history.preview(manager, 'corey')
    original = manager._delete_gallery
    def fail(job_id, *args):
        if job_id == bad.name:
            raise RuntimeError('Gallery is temporarily locked; retry Delete')
        return original(job_id, *args)
    monkeypatch.setattr(manager, '_delete_gallery', fail)
    result = history.delete(manager, 'corey', before['snapshot_id'], confirm=True)
    assert result['done'] and result['deleted'] == 1 and len(result['errors']) == 1
    assert not good.exists() and bad.exists()
    assert history.delete(manager, 'corey', before['snapshot_id'], confirm=True) == result
    monkeypatch.setattr(manager, '_delete_gallery', original)
    result = history.delete(manager, 'corey', before['snapshot_id'], confirm=True, retry_errors=True)
    assert result['deleted'] == 2 and not result['errors'] and not bad.exists()
    assert (receipts / (bad.name + '.json')).read_text() == 'deduplicate accepted submission'


def test_partial_filesystem_failure_keeps_reviewed_revision_for_retry(managers, monkeypatch):
    manager = managers[0]
    directory, _ = save(manager)
    h3._write(directory / 'manifest.json', h3._read(directory / 'manifest.json') | {'revision': 7})
    snapshot = history.preview(manager, 'corey')
    original_rmdir = Path.rmdir
    def fail_final(path, *args):
        if path == directory:
            raise PermissionError('Temporary filesystem failure')
        return original_rmdir(path, *args)
    monkeypatch.setattr(Path, 'rmdir', fail_final)
    result = history.delete(manager, 'corey', snapshot['snapshot_id'], confirm=True)
    assert len(result['errors']) == 1 and h3._read(directory / 'manifest.json')['revision'] == 7
    monkeypatch.setattr(Path, 'rmdir', original_rmdir)
    result = history.delete(manager, 'corey', snapshot['snapshot_id'], confirm=True, retry_errors=True)
    assert result['deleted'] == 1 and not result['errors'] and not directory.exists()


def test_actual_files_gallery_covers_cache_hardlinks_and_external_originals(managers, tmp_path, db):
    manager = managers[0]
    directory, state = save(manager, stage='preview')
    gallery_path = gallery(db, manager, state)
    external = tmp_path / 'E-library-video.mkv'
    external.write_bytes(b'original library video')
    source = directory / 'reference.mkv'
    os.link(external, source)
    neighbor, _ = save(manager, stage='preview', owner='other')
    os.link(source, neighbor / 'reference.mkv')
    (directory / 'candidates').mkdir()
    (directory / 'candidates' / 'preview.mp4').write_bytes(b'candidate')
    (directory / 'latents').mkdir()
    (directory / 'latents' / 'saved.bin').write_bytes(b'latent')
    (directory / 'external-link').symlink_to(external)
    manifest = h3._read(directory / 'manifest.json') | {'reference_videos': [str(source)], 'revision': 0}
    h3._write(directory / 'manifest.json', manifest)
    proxy.CACHE_ROOT.mkdir()
    identity = {'root': 'job-input', 'path': f'{directory.name}/0/reference_videos/0'}
    legacy = proxy.CACHE_ROOT / (proxy._cache_key(identity, source.stat(), 0, None) + '.mp4')
    legacy.write_bytes(b'legacy copy')
    own = proxy.CACHE_ROOT / (directory.name + '-' + 'a' * 64 + '.mp4')
    own.write_bytes(b'current copy')
    other = proxy.CACHE_ROOT / (neighbor.name + '-' + 'b' * 64 + '.mp4')
    other.write_bytes(b'neighbor copy')
    picker = proxy.CACHE_ROOT / ('c' * 64 + '.mp4')
    picker.write_bytes(b'server file browser copy')
    result = history.delete(manager, 'corey', history.preview(manager, 'corey', recipe='seed_hunter', stage='not_scout')['snapshot_id'], confirm=True)
    assert result['deleted'] == 1 and not result['errors']
    assert not directory.exists() and not gallery_path.exists() and not own.exists() and not legacy.exists()
    assert external.read_bytes() == b'original library video' and picker.exists()
    assert (neighbor / 'reference.mkv').read_bytes() == b'original library video' and other.exists()
    with db() as session:
        assert session.get(database.GalleryImage, state['id']) is None
        assert session.get(database.GalleryAlbum, state['id']).cover_id is None


@pytest.mark.parametrize('body', [{'scope': {}}, {'scope': []}, {'recipe': {}},
    {'recipe': 'seed_hunter', 'stage': []}, {'scope': 'all', 'stage': 'scout'},
    {'scope': 'workflow', 'unknown': 1}])
def test_invalid_preview_fields_return_400(managers, body):
    with client_for(managers) as client:
        response = client.post('/api/video/h3/history/delete-preview', json=body)
        assert response.status_code == 400, response.text


def test_confirm_owner_family_expiry_and_concurrent_repeat(managers):
    main, bfs = managers
    save(main)
    snapshot = history.preview(main, 'corey')
    body = {'snapshot_id': snapshot['snapshot_id'], 'confirm': True}
    with client_for(managers) as client:
        for path, headers, expected in [('/api/video/h3/history/delete', {'x-user': 'reader'}, 403),
                ('/api/video/h3/history/delete', {'x-user': 'other'}, 404),
                ('/api/video/bfs/history/delete', {}, 404)]:
            assert client.post(path, headers=headers, json=body).status_code == expected
        assert client.post('/api/video/h3/history/delete', json={**body, 'confirm': False}).status_code == 400
        lock_path = main.root.parent / '.history-deletions' / (snapshot['snapshot_id'] + '.lock')
        with h3._locked(lock_path):
            busy = client.post('/api/video/h3/history/delete', json=body)
        assert busy.status_code == 423 and busy.headers['Retry-After'] == '2'
    with ThreadPoolExecutor(2) as executor:
        def call():
            try:
                return history.delete(main, 'corey', **body)
            except RuntimeError as error:
                return str(error)
        results = list(executor.map(lambda _: call(), range(2)))
    assert any(isinstance(result, dict) and result['deleted'] == 1 for result in results)
    assert history.delete(main, 'corey', **body)['deleted'] == 1
    stale = history.preview(main, 'corey')
    path = main.root.parent / '.history-deletions' / (stale['snapshot_id'] + '.json')
    h3._write(path, h3._read(path) | {'expires_at': 0})
    with pytest.raises(RuntimeError, match='expired'):
        history.delete(main, 'corey', stale['snapshot_id'], confirm=True)


def test_real_queue_delete_api_stops_waiters_removes_files_and_gallery_keeps_render(queue, db, monkeypatch):
    managers, launch, root = queue
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    monkeypatch.setattr(proxy, 'CACHE_ROOT', root / 'preview-cache')
    running, _, _ = launch(hold=True)
    wait_for(lambda: (running / 'started').exists(), bool)
    first, _, _ = launch()
    second, _, _ = launch(1)
    outputs = [gallery(db, manager, h3._read(path / 'state.json')) for manager, path in zip(managers, (first, second))]
    processes = [h3._read(path / 'state.json')['supervisor'] for path in (first, second)]
    before = time.monotonic()
    with client_for(managers) as client:
        response = client.post('/api/video/h3/queue/delete', json={'scope': 'all'})
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt['succeeded'] == 2 and receipt['failed'] == 0 and receipt['errors'] == []
        assert all(row['deleted'] for row in receipt['results'])
        assert all(not path.exists() for path in (first, second, *outputs))
        assert all(not h3._live_record(record) for record in processes)
        assert h3._read(running / 'state.json')['status'] == 'running'
        assert not (root / 'overlap').exists()
        assert time.monotonic() - before < 15
        # Cancel intentionally keeps a stopped history entry; delete-queued
        # correctly explains why that entry no longer belongs to its scope.
        stopped, _, _ = launch()
        cancelled = client.post('/api/video/h3/queue/cancel', json={'scope': 'family'}).json()
        assert cancelled['succeeded'] == 1 and stopped.exists()
        empty = client.post('/api/video/h3/queue/delete', json={'scope': 'family'}).json()
        assert empty['succeeded'] == 0 and empty['failed'] == 0
        assert 'stopped history' in empty['message']
        preview = client.post('/api/video/h3/history/delete-preview', json={'scope': 'workflow'}).json()
        result = client.post('/api/video/h3/history/delete', json={'snapshot_id': preview['snapshot_id'], 'confirm': True}).json()
        assert result['deleted'] == 1 and not result['errors'] and not stopped.exists()
        assert h3._read(running / 'state.json')['status'] == 'running'
