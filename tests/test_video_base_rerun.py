"""Base-form reprocessing replaces settings, retaining each job's own media."""
from copy import deepcopy
import uuid

import pytest

from src import h3_video as h3
from tests.test_video_submission import backend, image
from tests.test_video_queue_bulk import create, request
from tests.test_video_single_job_rerun import saved_uploads
from tests.test_h3_seed_hunter_api import hunter, preview, final_body


def reprocess(backend, jobs, config, *, family='h3', key=None, owner='corey'):
    return backend.client().post(f'/api/video/{family}/queue/rerun', headers={'x-user': owner}, json={
        'jobs': [{'id': job['id'], 'revision': job['revision']} for job in jobs],
        'base_config': config, 'request_id': key or str(uuid.uuid4())})


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_base_settings_reused_across_jobs_with_each_sources_files_and_frozen_receipts(backend, family):
    extra = [('reference_videos', ('other.mp4', b'other reference', 'video/mp4')),
             ('reference_images', ('face.png', image(), 'image/png')),
             ('reference_audio', ('sound.wav', b'audio reference', 'audio/wav'))] if family == 'h3' else None
    jobs = [create(backend, family, extra=extra), create(backend, family, name='next.mp4')]
    manager = backend.managers[family]
    before = [(manager.directory(j['id']) / 'manifest.json').read_bytes() for j in jobs]
    base = {**deepcopy(backend.configs[family]), 'prompt': 'Shared revised prompt', 'steps': 11, 'seed': 31,
            'width': 960, 'height': 544}
    key = str(uuid.uuid4())
    response = reprocess(backend, jobs, base, family=family, key=key)
    assert response.status_code == 200, response.text
    copies = response.json()['jobs']
    assert len(copies) == 2
    for job, copy in zip(jobs, copies, strict=True):
        original = h3._read(manager.directory(job['id']) / 'manifest.json')
        manifest = h3._read(manager.directory(copy['id']) / 'manifest.json')
        assert manifest['config']['steps'] == 11 and manifest['config']['seed'] == 31
        assert manifest['config']['width'] == 960 and manifest['config']['height'] == 544
        assert base['prompt'] in manifest['config']['prompt']
        assert saved_uploads(manifest) == saved_uploads(original)
    assert [(manager.directory(j['id']) / 'manifest.json').read_bytes() for j in jobs] == before
    retry = reprocess(backend, jobs, base, family=family, key=key)
    assert retry.status_code == 200 and retry.json()['jobs'] == copies
    assert len(backend.calls) == 4
    changed = reprocess(backend, jobs, {**base, 'steps': 13}, family=family, key=key)
    assert changed.status_code == 409 and len(backend.calls) == 4
    original_rerun = request(backend, 'rerun', [jobs[0]], family=family)
    assert original_rerun.status_code == 200
    manifest = h3._read(manager.directory(original_rerun.json()['jobs'][0]['id']) / 'manifest.json')
    assert manifest['config'] == h3._read(manager.directory(jobs[0]['id']) / 'manifest.json')['config']


def test_hunter_base_rerun_of_refinement_starts_new_previews_not_old_latent(hunter):
    job, source = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter))
    assert response.status_code == 201, response.text
    final = response.json()
    directory = hunter.managers['h3'].directory(final['id'])
    original = h3._read(directory / 'manifest.json')
    assert original['seed_hunter_input']
    original['config']['reference_video_ranges'] = [{'start_seconds': 45, 'duration_seconds': 8}]
    h3._write(directory / 'manifest.json', original)
    base = deepcopy(hunter.configs['h3'])
    base.update(prompt='A revised base prompt.', width=864, height=480, steps=12, loras=[])
    base['seed_hunter'] = {'stage': 'preview', 'preview_count': 2, 'final_frames': None}
    base['reference_video_ranges'] = [{'start_seconds': 99, 'duration_seconds': 2}]
    response = reprocess(hunter, [final], base)
    assert response.status_code == 200, response.text
    copied = hunter.managers['h3'].directory(response.json()['jobs'][0]['id'])
    manifest = h3._read(copied / 'manifest.json')
    assert manifest['config']['seed_hunter']['stage'] == 'preview'
    assert manifest['config']['seed_hunter']['preview_count'] == 2
    assert manifest['config']['seed_hunter']['final_frames'] is None
    assert manifest['config']['width'] == 864 and manifest['config']['height'] == 480
    assert manifest['config']['prompt'] == base['prompt'] and manifest['config']['steps'] == 12
    assert manifest['config']['loras'] == []
    assert manifest['config']['reference_video_ranges'] == original['config']['reference_video_ranges']
    assert not manifest.get('seed_hunter_input') and not manifest.get('seed_hunter_source')
    assert not list(copied.glob('*.safetensors'))
    assert saved_uploads(manifest) == saved_uploads(original)
    assert h3._read(directory / 'manifest.json') == original


@pytest.mark.parametrize('problem', ['stale', 'other-owner', 'missing-input', 'invalid-settings', 'attachments', 'invalid-options', 'final-stage'])
def test_invalid_base_rerun_never_launches_or_changes_source(hunter, problem):
    job, source = preview(hunter)
    base = deepcopy(hunter.configs['h3'])
    owner, expected = 'corey', 400
    if problem == 'stale':
        job['revision'] += 1
        expected = 409
    elif problem == 'other-owner':
        owner, expected = 'other', 404
    elif problem == 'missing-input':
        manifest = h3._read(source / 'manifest.json')
        from pathlib import Path
        Path(manifest['reference_videos'][0]).unlink()
    elif problem == 'invalid-settings':
        base['steps'] = 0
    elif problem == 'attachments':
        base['reference_videos'] = ['/etc/passwd']
    elif problem == 'invalid-options':
        base['seed_hunter'] = None
    else:
        base['seed_hunter']['stage'] = 'final'
    original = (source / 'manifest.json').read_bytes()
    result = reprocess(hunter, [job], base, owner=owner)
    assert result.status_code == expected, result.text
    assert len(hunter.calls) == 1
    assert (source / 'manifest.json').read_bytes() == original


@pytest.mark.parametrize('fields', [{}, {'base_config': None}, {'patch': {}, 'base_config': {}}, {'base_config': []}])
def test_rerun_requires_one_settings_source(backend, fields):
    job = create(backend)
    result = backend.client().post('/api/video/h3/queue/rerun', json={
        'jobs': [{'id': job['id'], 'revision': 0}], 'request_id': str(uuid.uuid4()), **fields})
    assert result.status_code == 400 and len(backend.calls) == 1
