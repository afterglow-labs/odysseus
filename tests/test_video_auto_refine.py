import copy
import errno
import json
import os
from pathlib import Path
import uuid

import pytest

from src import h3_video as h3, video_auto_refine as auto
from tests.test_video_submission import backend, send
from tests.test_h3_seed_hunter_api import hunter, preview
from tests.test_h3_gpu_delete import db

@pytest.fixture(autouse=True)
def isolated_gallery(db):
    pass



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
    saved = h3._read(folder / 'state.json')
    target = Path(saved['project_output_paths'][0])
    source_target = Path(saved['project_source_paths'][0])
    assert target.name == f'clip01--candidate-2--seed-2000--{child}.mp4'
    assert target.stat().st_ino == Path(manifest['output_path']).stat().st_ino
    assert source_target.name == f'clip01--reference-video-1--{child}.mp4'
    assert source_target.read_bytes() == b'video fixture'
    assert source_target.samefile(manifest['reference_videos'][0])
    assert apply(hunter, [parent['id']], options, key).json() == first
    # An old project export may already have the render but no context video.
    source_target.unlink()
    saved.pop('project_source_paths')
    h3._write(folder / 'state.json', saved)
    skipped = apply(hunter, [parent['id']], options).json()
    assert skipped['created'] == 0 and skipped['skipped'] == 1
    assert source_target.read_bytes() == b'video fixture'
    assert len(hunter.calls) == 2
    forced = apply(hunter, [parent['id']], {**options, 'existing': 'rerender'}).json()
    new_id = forced['jobs'][parent['id']][0]['job_id']
    assert new_id != child
    complete(manager, new_id, b'another render'); auto.process(manager, new_id)
    assert len(list(target.parent.glob('*--candidate-*.mp4'))) == 2
    assert len(list(target.parent.glob('*--reference-video-*.mp4'))) == 2
    assert target.read_bytes() == b'finished video'
    manager.delete(child, 'corey')
    assert not target.exists()
    assert not source_target.exists()
    assert len(list(target.parent.glob('*--candidate-*.mp4'))) == 1
    assert len(list(target.parent.glob('*--reference-video-*.mp4'))) == 1
    assert Path(h3._read(manager.directory(new_id) / 'manifest.json')['reference_videos'][0]).read_bytes() == b'video fixture'


def completed_refinement(hunter):
    parent, _ = preview(hunter)
    response = apply(hunter, [parent['id']], settings(hunter, candidates=[1])).json()
    identity = response['jobs'][parent['id']][0]['job_id']
    manager = hunter.managers['h3']
    folder, manifest = complete(manager, identity)
    return manager, folder, manifest


def test_project_includes_all_saved_videos_with_stable_safe_names(hunter):
    manager, folder, manifest = completed_refinement(hunter)
    second, source = folder / 'second.mkv', folder / 'source.mov'
    second.write_bytes(b'second reference'); source.write_bytes(b'continuation source')
    manifest['reference_videos'].append(str(second))
    manifest['source_video'] = str(source)
    manifest['input_names'] = {'reference_videos': ['same.mp4', 'C:\\clips\\same.mkv'],
                               'source_video': ['../' + '映像' * 150 + '.mov']}
    h3._write(folder / 'manifest.json', manifest)
    auto.publish_output(folder)
    state = h3._read(folder / 'state.json')
    targets = [Path(value) for value in state['project_source_paths']]
    assert len(targets) == 3 and len(set(targets)) == 3
    assert {p.read_bytes() for p in targets} == {b'video fixture', b'second reference', b'continuation source'}
    assert all(p.parent == Path(state['project_output_paths'][0]).parent and len(p.name.encode()) <= 255 for p in targets)
    assert any(p.name == f'same--reference-video-2--{folder.name}.mkv' for p in targets)
    assert all(not p.is_symlink() for p in targets)
    before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in targets}
    auto.publish_output(folder)
    assert before == {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in targets}
    assert auto.summary(folder, manifest, h3._read(folder / 'state.json'))['source_paths'] == state['project_source_paths']
    # A hardlink is a complete file, so deleting a snapshot name cannot break it.
    second.unlink()
    assert next(p for p in targets if '--reference-video-2--' in p.name).read_bytes() == b'second reference'
    auto.delete_output(manifest, state)
    assert not any(p.exists() for p in targets)


@pytest.mark.parametrize('kind', ['outside', 'symlink', 'missing', 'directory'])
def test_project_source_must_be_a_regular_saved_snapshot(hunter, tmp_path, kind):
    manager, folder, manifest = completed_refinement(hunter)
    outside = tmp_path / 'private.mkv'; outside.write_bytes(b'not job media')
    chosen = folder / 'source.mkv'
    if kind == 'outside':
        chosen = outside
    elif kind == 'symlink':
        chosen.symlink_to(outside)
    elif kind == 'directory':
        chosen.mkdir()
    manifest['source_video'] = str(chosen)
    h3._write(folder / 'manifest.json', manifest)
    with pytest.raises(ValueError, match='saved source video'):
        auto.publish_output(folder)
    assert not (manager.project / 'outputs/video-projects').exists()
    assert outside.read_bytes() == b'not job media'


def test_source_export_falls_back_to_copy_on_another_filesystem(hunter, monkeypatch):
    manager, folder, manifest = completed_refinement(hunter)
    def other_volume(*args, **kwargs):
        raise OSError(errno.EXDEV, 'Other filesystem')
    monkeypatch.setattr(os, 'link', other_volume)
    auto.publish_output(folder)
    target = Path(h3._read(folder / 'state.json')['project_source_paths'][0])
    assert target.read_bytes() == Path(manifest['reference_videos'][0]).read_bytes()
    assert not target.samefile(manifest['reference_videos'][0])
    assert not list(target.parent.glob('*.tmp'))


@pytest.mark.parametrize('kind', ['directory', 'symlink'])
def test_source_export_rejects_unsafe_existing_destination(hunter, tmp_path, kind):
    manager, folder, manifest = completed_refinement(hunter)
    auto.publish_output(folder)
    target = Path(h3._read(folder / 'state.json')['project_source_paths'][0])
    target.unlink()
    outside = tmp_path / 'outside.mkv'; outside.write_bytes(b'leave unchanged')
    if kind == 'symlink':
        target.symlink_to(outside)
    else:
        target.mkdir()
    with pytest.raises(ValueError, match='Project'):
        auto.publish_output(folder)
    assert outside.read_bytes() == b'leave unchanged'
    assert Path(manifest['reference_videos'][0]).read_bytes() == b'video fixture'


def test_source_export_retry_preserves_render_and_successful_inputs(hunter, monkeypatch):
    from src import video_job_storage
    manager, folder, manifest = completed_refinement(hunter)
    second = folder / 'another.mkv'; second.write_bytes(b'another reference')
    manifest['reference_videos'].append(str(second))
    h3._write(folder / 'manifest.json', manifest)
    publish = video_job_storage.publish_gallery_video
    def interrupted(source, target):
        if source == second:
            raise OSError(errno.ENOSPC, 'No space')
        return publish(source, target)
    monkeypatch.setattr(video_job_storage, 'publish_gallery_video', interrupted)
    auto.process(manager, folder.name)
    assert h3._read(folder / 'state.json')['status'] == 'completed'
    assert h3._read(folder / 'auto-refine.json')['status'] == 'retrying'
    published = {p: p.stat().st_ino for p in (manager.project / 'outputs/video-projects').rglob('*.mp4')}
    assert len(published) == 2
    monkeypatch.setattr(video_job_storage, 'publish_gallery_video', publish)
    auto.process(manager, folder.name)
    state = h3._read(folder / 'state.json')
    assert len(state['project_source_paths']) == 2
    assert published == {p: p.stat().st_ino for p in published}
    assert not (manager.root / '.automatic' / (folder.name + '.json')).exists()


def test_invalid_legacy_input_does_not_block_owned_job_deletion(hunter):
    manager, folder, manifest = completed_refinement(hunter)
    auto.publish_output(folder)
    state = h3._read(folder / 'state.json')
    manifest['reference_videos'].append(17)
    h3._write(folder / 'manifest.json', manifest)
    manager.delete(folder.name, 'corey')
    assert not folder.exists()
    assert not any(Path(path).exists() for path in state['project_output_paths'] + state['project_source_paths'])


def test_legacy_automatic_completion_never_creates_refinements(hunter, monkeypatch):
    hunter.configs['h3']['seed_hunter']['auto_refine'] = settings(hunter, candidates=[1, 3])
    parent, folder = preview(hunter)
    manager = hunter.managers['h3']
    manifest = h3._read(folder / 'manifest.json')
    assert 'auto_refine' not in manifest['config']['seed_hunter']
    # Also cover already-persisted jobs/tickets from an older server.
    manifest['config']['seed_hunter']['auto_refine'] = settings(hunter)
    h3._write(folder / 'manifest.json', manifest)
    ticket = manager.root / '.automatic' / (parent['id'] + '.json')
    ticket.parent.mkdir(exist_ok=True)
    h3._write(ticket, {'id': parent['id']})
    monkeypatch.setattr(auto, '_queue_candidate', lambda *a, **kw: pytest.fail('unrequested refinement'))
    auto.process(manager, parent['id']); auto.process(manager, parent['id'])
    assert len(hunter.calls) == 1
    assert not ticket.exists()


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
def test_legacy_automation_is_ignored_on_new_preview_submissions(hunter, patch):
    hunter.configs['h3']['seed_hunter']['auto_refine'] = settings(hunter, **patch)
    reply = send(hunter, 'h3')
    assert reply.status_code == 201, reply.text
    assert len(hunter.calls) == 1
    assert "auto_refine" not in h3._read(hunter.managers["h3"].directory(reply.json()["id"]) / "manifest.json")["config"]["seed_hunter"]


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
