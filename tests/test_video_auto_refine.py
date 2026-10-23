import copy
import json
from pathlib import Path
import uuid

import pytest

from src import h3_video as h3, video_auto_refine as auto
from tests.test_video_submission import backend, send
from tests.test_h3_seed_hunter_api import hunter, preview


def settings(hunter, **patch):
    return {'project': 'My batch', 'candidates': 'all', 'existing': 'skip',
            'refinement': {'width': 960, 'height': 1728, 'steps': 4, 'denoise': .25,
                           'latent_upscaler': hunter.configs['h3']['latent_upscaler'],
                           'loras': [], 'seed_hunter': {'output_fps': 24}}, **patch}


def apply(hunter, parents, values, key=None):
    return hunter.client().post('/api/video/h3/auto-refine', json={
        'jobs': parents, 'settings': values, 'request_id': key or uuid.uuid4().hex})


def complete(manager, identity, data=b'finished video'):
    folder = manager.directory(identity)
    manifest = h3._read(folder / 'manifest.json')
    Path(manifest['output_path']).write_bytes(data)
    state = h3._read(folder / 'state.json')
    h3._write(folder / 'state.json', {**state, 'status': 'completed', 'supervisor': None})
    return folder, manifest


def test_bulk_keeps_each_parent_seed_latent_media_and_length(hunter):
    parents = []
    for n in range(2):
        hunter.configs['h3']['seed_hunter']['final_frames'] = 73 + n * 17
        job, folder = preview(hunter)
        # Deliberately differ from the preview offset algorithm. Use saved seeds.
        for i in (1, 2, 3):
            meta = h3._read(folder / f'candidate-{i}.json')
            h3._write(folder / f'candidate-{i}.json', {**meta, 'seed': 301 + n * 100 + i})
            (folder / f'candidate-{i}.safetensors').write_bytes(f'{n}:{i}'.encode())
        parents.append(job['id'])
    response = apply(hunter, parents, settings(hunter))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['created'] == 6 and not body['errors']
    manager = hunter.managers['h3']
    for n, parent in enumerate(parents):
        for row in body['jobs'][parent]:
            child = h3._read(manager.directory(row['job_id']) / 'manifest.json')
            assert child['config']['seed'] == 301 + n * 100 + row['candidate']
            assert child['config']['frames'] == 73 + n * 17
            assert child['config']['steps'] == 4 and child['config']['seed_hunter']['denoise'] == .25
            assert Path(child['seed_hunter_input']['latent']).read_bytes() == f"{n}:{row['candidate']}".encode()
            assert child['seed_hunter_source']['source_job_id'] == parent
            assert Path(child['reference_videos'][0]).read_bytes() == b'video fixture'
            assert 'auto_refine' not in child['config']['seed_hunter']


def test_retry_skip_and_force_include_existing_outputs_without_copy(hunter):
    parent, _ = preview(hunter)
    key = uuid.uuid4().hex
    options = settings(hunter, candidates=[2])
    first = apply(hunter, [parent['id']], options, key).json()
    child = first['jobs'][parent['id']][0]['job_id']
    manager = hunter.managers['h3']
    folder, manifest = complete(manager, child)
    auto.process(manager, child)
    target = next((manager.project / 'outputs/video-projects').rglob('*.mp4'))
    assert target.name == f'clip01--candidate-2--seed-2000--{child}.mp4'
    assert target.stat().st_ino == Path(manifest['output_path']).stat().st_ino
    assert apply(hunter, [parent['id']], options, key).json() == first
    skipped = apply(hunter, [parent['id']], options).json()
    assert skipped['created'] == 0 and skipped['skipped'] == 1
    assert len(hunter.calls) == 2
    forced = apply(hunter, [parent['id']], {**options, 'existing': 'rerender'}).json()
    new_id = forced['jobs'][parent['id']][0]['job_id']
    assert new_id != child
    complete(manager, new_id, b'another render'); auto.process(manager, new_id)
    assert len(list(target.parent.glob('*.mp4'))) == 2
    assert target.read_bytes() == b'finished video'
    manager.delete(child, 'corey')
    assert not target.exists()
    assert len(list(target.parent.glob('*.mp4'))) == 1


def test_automatic_parent_completion_is_recoverable_and_idempotent(hunter, monkeypatch):
    hunter.configs['h3']['seed_hunter']['auto_refine'] = settings(hunter, candidates=[1, 3])
    parent, folder = preview(hunter)
    manager = hunter.managers['h3']
    original = auto._queue_candidate
    def interrupted(*args, **kwargs):
        if args[4] == 3:
            raise OSError('temporary interruption')
        return original(*args, **kwargs)
    monkeypatch.setattr(auto, '_queue_candidate', interrupted)
    auto.process(manager, parent['id'])
    assert len(hunter.calls) == 2
    assert (manager.root / '.automatic' / (parent['id'] + '.json')).exists()
    monkeypatch.setattr(auto, '_queue_candidate', original)
    auto.process(manager, parent['id']); auto.process(manager, parent['id'])
    assert len(hunter.calls) == 3
    receipt = h3._read(folder / 'auto-refine.json')
    assert [r['candidate'] for r in receipt['jobs']] == [1, 3]
    assert not (manager.root / '.automatic' / (parent['id'] + '.json')).exists()


def test_scout_bulk_resumes_distinct_partials_then_applies_shared_final_config(hunter):
    config = hunter.configs['h3']
    config.update(seed=71, steps=20)
    config['seed_hunter'].update(stage='scout', scout_count=3, scout_step=4, scout_mode='scout')
    hunter.inventories['h3']['seed_hunter']['features']['seed_scout'] = True
    parent = send(hunter, 'h3').json()['id']
    manager = hunter.managers['h3']
    folder, original = complete(manager, parent)
    h3._write(folder / 'scout.json', {'mode': 'scout', 'seeds': [71, 72, 73], 'previews': [{'seed': s} for s in (71, 72, 73)]})
    for seed in (71, 72, 73):
        (folder / f'scout-{seed}.webp').write_bytes(b'preview')
        (folder / f'scout-{seed}.safetensors').write_bytes(str(seed).encode())
        h3._write(folder / f'scout-{seed}.json', {'config': original['config']})
    response = apply(hunter, [parent], settings(hunter))
    assert response.status_code == 200, response.text
    body = response.json(); assert not body['errors'], body
    for row in body['jobs'][parent]:
        child = h3._read(manager.directory(row['job_id']) / 'manifest.json')
        seed = 70 + row['candidate']
        assert child['config']['seed'] == child['scout_refinement']['seed'] == seed
        assert child['config']['steps'] == 20 and child['config']['seed_hunter']['scout_step'] == 4
        assert child['scout_refinement']['steps'] == 4 and child['scout_refinement']['width'] == 960
        assert child['seed_hunter_source']['strategy'] == 'resume'
        assert Path(child['seed_hunter_input']['latent']).read_bytes() == str(seed).encode()


@pytest.mark.parametrize('patch', [ {'refinement': {'seed': 1}}, {'project': '../escape'},
    {'candidates': [1, 1]}, {'candidates': [4]}, {'refinement': {'seed_hunter': {'stage': 'single'}}} ])
def test_invalid_automation_rejected_before_parent_launch(hunter, patch):
    hunter.configs['h3']['seed_hunter']['auto_refine'] = settings(hunter, **patch)
    reply = send(hunter, 'h3')
    assert reply.status_code == 400, reply.text
    assert not hunter.calls


def test_ownership_idempotency_conflict_and_output_symlink(hunter, tmp_path):
    parent, folder = preview(hunter)
    body = {'jobs': [parent['id']], 'settings': settings(hunter, candidates=[1]), 'request_id': uuid.uuid4().hex}
    client = hunter.client()
    assert client.post('/api/video/h3/auto-refine', json=body, headers={'x-user': 'other'}).status_code == 404
    response = client.post('/api/video/h3/auto-refine', json=body)
    assert response.status_code == 200
    assert client.post('/api/video/h3/auto-refine', json={**body, 'settings': settings(hunter)}).status_code == 409
    manager = hunter.managers['h3']
    child = response.json()['jobs'][parent['id']][0]['job_id']
    complete(manager, child)
    (manager.project / 'outputs').symlink_to(tmp_path / 'elsewhere')
    auto.process(manager, child)
    assert h3._read(manager.directory(child) / 'state.json')['status'] == 'completed'
    assert h3._read(manager.directory(child) / 'auto-refine.json')['status'] == 'retrying'
