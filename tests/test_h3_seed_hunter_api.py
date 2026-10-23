"""Staged inference must preserve the selected latent and existing H3 behavior."""
import json
import shutil
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_video_submission import backend, send
from tests.test_h3_video import weights
from src import h3_video as h3
from src.video_queue_bulk import rerun_jobs
from src.video_workflow import portable_config


@pytest.fixture
def hunter(backend):
    inventory = backend.inventories['h3']
    path = weights(backend.managers['h3'].project / 'models/minimax_h3_latent_upscaler.safetensors')
    inventory['components'] = h3.discover_components([path.parent])
    inventory['seed_hunter'] = {'features': {'latent_upscaler': True, 'sparse_attention': True}}
    config = backend.configs['h3']
    config.update(recipe='seed_hunter', seed_hunter={'stage': 'preview', 'preview_count': 3, 'final_frames': 73},
                  width=480, height=864, frames=73, steps=6, sampler='er_sde', scheduler='beta')
    config['latent_upscaler'] = next(c['id'] for c in inventory['components'] if c['role'] == 'latent_upscaler')
    return backend


def preview(hunter, completed=True):
    response = send(hunter, 'h3')
    assert response.status_code == 201, response.text
    job = response.json()
    manager = hunter.managers['h3']
    directory = manager.directory(job['id'])
    state = h3._read(directory / 'state.json')
    if completed:
        h3._write(directory / 'state.json', {**state, 'status': 'completed'})
    for i in (1, 2, 3):
        (directory / f'candidate-{i}.mp4').write_bytes(f'video{i}'.encode())
        (directory / f'candidate-{i}.json').write_text(json.dumps({'chosen': i, 'seed': i * 1000,
            'config': h3._read(directory / 'manifest.json')['config']}))
        weights(directory / f'candidate-{i}.safetensors')
    h3._write(directory / 'candidates.json', {'version': 1, 'candidates': [
        {'id': i, 'seed': i * 1000, 'status': 'completed', 'latent_ready': True} for i in (1, 2, 3)]})
    return job, directory


def final_body(hunter, **patch):
    return {'request_id': str(uuid.uuid4()), 'candidate': 2, 'config': {
        'width': 1088, 'height': 1952, 'steps': 2, 'seed': 321,
        'sampler': 'er_sde', 'scheduler': 'linear_quadratic', 'denoise': .4,
        'latent_upscaler': hunter.configs['h3']['latent_upscaler'], **patch}}


def test_preview_detail_and_video_are_owned_and_never_enqueue(hunter):
    job, directory = preview(hunter)
    client = hunter.client()
    prefix = f"/api/video/h3/jobs/{job['id']}/seed-hunter"
    result = client.get(prefix)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['config']['seed_hunter']['preview_count'] == 3
    assert data['config']['latent_upscaler'] == hunter.configs['h3']['latent_upscaler']
    assert data['seed_hunter']['candidates'][1]['can_refine'] is True
    assert str(directory.parent) not in result.text
    assert client.get(prefix + '/candidates/2/video').content == b'video2'
    assert client.get(prefix, headers={'x-user': 'other'}).status_code == 404
    assert client.get(prefix + '/candidates/2/video', headers={'x-user': 'other'}).status_code == 404
    assert len(hunter.calls) == 1


def test_older_preview_defaults_final_to_primary_source_without_changing_preview(hunter, monkeypatch):
    from src import h3_video_length
    hunter.configs['h3']['seed_hunter'].pop('final_frames')
    monkeypatch.setattr(h3_video_length, 'probe_video', lambda *a, **k:
                        {'duration_seconds': 12.3, 'source_frames': 295})
    job, directory = preview(hunter)
    prefix = f"/api/video/h3/jobs/{job['id']}/seed-hunter"
    data = hunter.client().get(prefix).json()
    assert data['config']['frames'] == 73
    assert data['refinement_defaults']['frames'] == 311
    assert data['final_length']['regeneration_required'] is True
    assert data['final_length']['basis'] == 'source'
    body = final_body(hunter)
    body['config'].pop('seed')
    response = hunter.client().post(prefix + '/finalize', json=body)
    assert response.status_code == 201, response.text
    final = h3._read(hunter.managers['h3'].directory(response.json()['id']) / 'manifest.json')
    assert final['config']['frames'] == final['config']['seed_hunter']['final_frames'] == 311
    assert final['config']['seed'] == final['seed_hunter_source']['source_seed'] == 2000
    assert h3._read(directory / 'manifest.json')['config']['frames'] == 73


def test_unreadable_legacy_source_requires_explicit_final_length(hunter, monkeypatch):
    from src import h3_video_length
    hunter.configs['h3']['seed_hunter'].pop('final_frames')
    def unavailable(*a, **k):
        raise ValueError('private source unreadable')
    monkeypatch.setattr(h3_video_length, 'probe_video', unavailable)
    job, _ = preview(hunter)
    prefix = f"/api/video/h3/jobs/{job['id']}/seed-hunter"
    data = hunter.client().get(prefix).json()
    assert data['final_length']['requires_manual'] is True
    response = hunter.client().post(prefix + '/finalize', json=final_body(hunter))
    assert response.status_code == 400 and 'manually' in response.text
    response = hunter.client().post(prefix + '/finalize', json=final_body(hunter, frames=311))
    assert response.status_code == 201, response.text


def test_final_length_survives_parent_deletion_queued_edit_and_rerun(hunter):
    job, source = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize",
                                   json=final_body(hunter, frames=311))
    assert response.status_code == 201, response.text
    identifier = response.json()['id']
    manager = hunter.managers['h3']
    directory = manager.directory(identifier)
    shutil.rmtree(source)
    url = f'/api/video/h3/jobs/{identifier}'
    draft = hunter.client().get(url + '/edit').json()['config']
    assert draft['seed_hunter']['final_frames'] == 311
    draft['frames'] = 277
    response = hunter.client().patch(url, data={'config': json.dumps(draft), 'revision': '0'})
    assert response.status_code == 200, response.text
    saved = h3._read(directory / 'manifest.json')
    assert saved['config']['seed_hunter']['final_frames'] == 277
    rerun_jobs(manager, 'corey', 'h3', [{'id': identifier, 'revision': 1}],
                       {'frames': 345}, str(uuid.uuid4()))
    # The rerun owns source inputs and the original short candidate after deletion.
    rerun = next(path for path in manager.root.glob('*/manifest.json')
                 if path.parent.name != identifier)
    manifest = h3._read(rerun)
    assert manifest['config']['frames'] == manifest['config']['seed_hunter']['final_frames'] == 345
    assert h3._read(manifest['seed_hunter_input']['metadata'])['config']['frames'] == 73
    assert all(Path(path).is_file() for path in manifest['reference_videos'])


@pytest.mark.parametrize('frames', [None, 73, 311, 362])
def test_final_target_is_portable_and_nullable(hunter, frames):
    config = {**hunter.configs['h3'], 'seed_hunter': {'stage': 'preview', 'final_frames': frames}}
    # Component IDs are omitted by the portable workflow settings interface.
    from src.video_workflow import specification
    settings, _, _ = specification('h3', config)
    portable = {key: value for key, value in config.items() if key in settings}
    assert portable_config(portable, 'h3')['seed_hunter']['final_frames'] == frames


def test_finalize_is_idempotent_copies_exact_candidate_and_survives_parent_deletion(hunter):
    job, directory = preview(hunter)
    body = final_body(hunter)
    url = f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize"
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: hunter.client().post(url, json=body), range(2)))
    assert all(r.status_code == 201 for r in replies), [r.text for r in replies]
    assert replies[0].json()['id'] == replies[1].json()['id']
    target = hunter.managers['h3'].directory(replies[0].json()['id'])
    manifest = h3._read(target / 'manifest.json')
    assert manifest['config']['seed_hunter']['stage'] == 'final'
    assert manifest['config']['prompt'] == hunter.configs['h3']['prompt']
    assert manifest['config']['width'] == 1088
    assert manifest['seed_hunter_input']['candidate'] == 2
    assert json.loads((target / 'source-latent.json').read_text())['chosen'] == 2
    assert manifest['seed_hunter_source']['source_job_id'] == job['id']
    shutil.rmtree(directory)
    assert (target / 'source-latent.safetensors').is_file()
    assert hunter.client().post(url, json=body).json()['id'] == target.name
    assert len(hunter.calls) == 2


@pytest.mark.parametrize('patch', [{'prompt': ''}, {'frames': 74}, {'encoder': '/etc/passwd'},
                                  {'seed_hunter': {'stage': 'preview'}}, {'latent_upscaler': '/etc/passwd'}])
def test_final_rejects_replacing_saved_conditioning_or_path_injection(hunter, patch):
    job, _ = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter, **patch))
    assert response.status_code == 400, response.text
    assert len(hunter.calls) == 1


@pytest.mark.parametrize('status', ['running', 'failed', 'stopped'])
def test_finished_candidates_remain_refinable_while_other_candidates_are_unfinished(hunter, status):
    job, directory = preview(hunter)
    state = {**h3._read(directory / 'state.json'), 'status': status}
    h3._write(directory / 'state.json', state)
    candidates = h3._read(directory / 'candidates.json')
    candidates['candidates'][2].update(status='running', latent_ready=False)
    h3._write(directory / 'candidates.json', candidates)
    # Even if its files already exist, an unpublished candidate is not ready.
    saved = {name: (directory / name).read_bytes() for name in
             ['state.json', 'manifest.json', 'candidates.json', 'candidate-1.json', 'candidate-2.json']}
    client = hunter.client()
    url = f"/api/video/h3/jobs/{job['id']}"
    for endpoint in [url, url + '/seed-hunter']:
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        rows = response.json()['seed_hunter']['candidates']
        assert [row['index'] for row in rows] == [1, 2]
        assert all(row['can_refine'] for row in rows)
    body = final_body(hunter)
    response = client.post(url + '/seed-hunter/finalize', json={**body, 'candidate': 3})
    assert response.status_code == 400, response.text
    assert len(hunter.calls) == 1
    response = client.post(url + '/seed-hunter/finalize', json=body, headers={'x-user': 'other'})
    assert response.status_code == 404
    response = client.post(url + '/seed-hunter/finalize', json=body)
    assert response.status_code == 201, response.text
    assert response.json()['status'] == 'queued'
    target = hunter.managers['h3'].directory(response.json()['id'])
    assert (target / 'source-latent.safetensors').read_bytes() == (directory / 'candidate-2.safetensors').read_bytes()
    assert h3._read(target / 'source-latent.json')['chosen'] == 2
    assert client.post(url + '/seed-hunter/finalize', json=body).json()['id'] == target.name
    another = client.post(url + '/seed-hunter/finalize', json={**final_body(hunter), 'candidate': 1})
    assert another.status_code == 201, another.text
    assert another.json()['id'] != target.name
    assert len(hunter.calls) == 3
    assert {name: (directory / name).read_bytes() for name in saved} == saved
    # Finishing a sibling never consumes or replaces the earlier previews.
    candidates['candidates'][2].update(status='completed', latent_ready=True)
    h3._write(directory / 'candidates.json', candidates)
    rows = client.get(url + '/seed-hunter').json()['seed_hunter']['candidates']
    assert [row['index'] for row in rows] == [1, 2, 3]
    assert all(row['can_refine'] for row in rows)


@pytest.mark.parametrize('suffix', ['json', 'safetensors'])
@pytest.mark.parametrize('damage', ['missing', 'empty', 'symlink'])
def test_running_preview_requires_complete_saved_artifacts(hunter, suffix, damage):
    job, directory = preview(hunter)
    h3._write(directory / 'state.json', {**h3._read(directory / 'state.json'), 'status': 'running'})
    artifact = directory / f'candidate-2.{suffix}'
    artifact.unlink()
    if damage == 'empty':
        artifact.touch()
    elif damage == 'symlink':
        artifact.symlink_to(directory / f'candidate-1.{suffix}')
    client = hunter.client()
    url = f"/api/video/h3/jobs/{job['id']}/seed-hunter"
    candidates = client.get(url).json()['seed_hunter']['candidates']
    assert candidates[0]['can_refine'] is True
    assert candidates[1]['can_refine'] is False
    response = client.post(url + '/finalize', json=final_body(hunter))
    assert response.status_code == 400, response.text
    assert len(hunter.calls) == 1


def test_refinement_rejects_unstarted_preview_and_direct_final_submission(hunter):
    job, _ = preview(hunter, completed=False)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter))
    assert response.status_code == 409
    hunter.configs['h3']['seed_hunter']['stage'] = 'final'
    response = send(hunter, 'h3')
    assert response.status_code == 400, response.text
    assert len(hunter.calls) == 1


def test_partial_bulk_edit_preserves_options_and_rejects_stage_change(hunter):
    job, directory = preview(hunter, completed=False)
    url = '/api/video/h3/queue/edit'
    response = hunter.client().post(url, json={'jobs': [{'id': job['id'], 'revision': 0}],
                                               'patch': {'seed_hunter': {'preview_count': 2}}})
    assert response.status_code == 200, response.text
    config = h3._read(directory / 'manifest.json')['config']
    assert config['seed_hunter']['preview_count'] == 2 and config['seed_hunter']['stage'] == 'preview'
    response = hunter.client().post(url, json={'jobs': [{'id': job['id'], 'revision': 1}],
                                               'patch': {'seed_hunter': {'stage': 'dialogue'}}})
    assert response.status_code == 400, response.text


def test_final_rerun_preserves_latent_and_applies_prompt_edits(hunter):
    job, directory = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter))
    assert response.status_code == 201, response.text
    final = response.json()
    manager = hunter.managers['h3']
    selected = [{'id': final['id'], 'revision': 0}]
    rerun_jobs(manager, 'corey', 'h3', selected,
               {'prompt': 'A revised refinement prompt', 'steps': 4, 'seed_hunter': {'denoise': .3}}, str(uuid.uuid4()))
    assert len(hunter.calls) == 3
    reruns = [m for p in manager.root.iterdir() if p.is_dir() and (m := h3._read(p/'manifest.json')).get('config', {}).get('steps') == 4]
    assert len(reruns) == 1
    saved = reruns[0]
    assert saved['seed_hunter_input']['candidate'] == 2
    assert saved['config']['seed_hunter']['denoise'] == .3
    assert saved['config']['prompt'] == 'A revised refinement prompt'
    assert h3._read(Path(saved['seed_hunter_input']['metadata']))['config']['prompt'] == hunter.configs['h3']['prompt']


def test_refinement_prompt_create_single_edit_and_bulk_edit_reopen_with_same_inputs(hunter):
    job, source = preview(hunter)
    client = hunter.client()
    response = client.post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize",
                           json=final_body(hunter, prompt='Initial refinement prompt'))
    assert response.status_code == 201, response.text
    final = response.json()
    directory = hunter.managers['h3'].directory(final['id'])
    before = h3._read(directory / 'manifest.json')
    snapshot = (directory / 'source-latent.json').read_bytes()
    endpoint = f"/api/video/h3/jobs/{final['id']}"
    draft = client.get(endpoint + '/edit').json()['config']
    assert draft['prompt'] == 'Initial refinement prompt'
    draft['prompt'] = 'Use a purple jacket. Keep the background unchanged.'
    response = client.patch(endpoint, data={'config': json.dumps(draft), 'revision': '0'})
    assert response.status_code == 200, response.text
    assert client.get(endpoint + '/edit').json()['config']['prompt'] == draft['prompt']
    response = client.post('/api/video/h3/queue/edit', json={
        'jobs': [{'id': final['id'], 'revision': 1}], 'patch': {'prompt': 'Use a green jacket instead.'}})
    assert response.status_code == 200, response.text
    reopened = client.get(endpoint + '/edit').json()
    assert reopened['config']['prompt'] == 'Use a green jacket instead.' and reopened['revision'] == 2
    after = h3._read(directory / 'manifest.json')
    for key in ('seed_hunter_input', 'seed_hunter_source', 'reference_videos', 'reference_images', 'reference_audio'):
        assert after.get(key) == before.get(key)
    assert (directory / 'source-latent.json').read_bytes() == snapshot
    assert h3._read(source / 'manifest.json')['config']['prompt'] == hunter.configs['h3']['prompt']
    assert len(hunter.calls) == 2  # Saving edits does not enqueue another render.


def test_portable_recipe_retains_stages_and_refuses_final_without_latent():
    raw = {'recipe': 'seed_hunter', 'seed_hunter': {'stage': 'preview', 'preview_count': 3}}
    assert portable_config(raw, 'h3') == raw
    with pytest.raises(ValueError, match='source preview'):
        portable_config({'recipe': 'seed_hunter', 'seed_hunter': {'stage': 'final'}}, 'h3')


@pytest.mark.parametrize('width', [None, '1088', [], {}, True])
def test_malformed_refinement_dimensions_return_validation_error(hunter, width):
    job, _ = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize",
                                   json=final_body(hunter, width=width))
    assert response.status_code == 400, response.text
    assert len(hunter.calls) == 1


def test_final_edit_and_rerun_validate_against_original_preview_not_previous_final_size(hunter):
    job, _ = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter))
    assert response.status_code == 201, response.text
    final = response.json()
    manager = hunter.managers['h3']
    directory = manager.directory(final['id'])
    before = (directory / 'manifest.json').read_bytes()
    endpoint = f"/api/video/h3/jobs/{final['id']}"
    draft = hunter.client().get(endpoint + '/edit').json()['config']
    invalid = {**draft, 'width': 256, 'height': 864}
    response = hunter.client().patch(endpoint, data={'config': json.dumps(invalid), 'revision': '0'})
    assert response.status_code == 400 and 'original preview' in response.text
    assert (directory / 'manifest.json').read_bytes() == before
    with pytest.raises(ValueError, match='original preview'):
        rerun_jobs(manager, 'corey', 'h3', [{'id': final['id'], 'revision': 0}],
                   {'width': 256, 'height': 864}, str(uuid.uuid4()))
    # 960x1728 is smaller than the previous 1088x1952 final, but is valid
    # because its actual retained preview is only 480x864.
    snapshot = (directory / 'source-latent.json').read_bytes()
    valid = {**draft, 'width': 960, 'height': 1728}
    response = hunter.client().patch(endpoint, data={'config': json.dumps(valid), 'revision': '0'})
    assert response.status_code == 200, response.text
    assert (directory / 'source-latent.json').read_bytes() == snapshot
    assert len(hunter.calls) == 2


def test_saved_refinement_cannot_reorder_original_references(hunter):
    job, source = preview(hunter)
    manifest = h3._read(source / 'manifest.json')
    second = source / 'second.mp4'
    second.write_bytes(b'second video fixture')
    manifest['reference_videos'].append(str(second))
    manifest['input_names']['reference_videos'].append('second.mp4')
    h3._write(source / 'manifest.json', manifest)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter))
    assert response.status_code == 201, response.text
    final = response.json()
    directory = hunter.managers['h3'].directory(final['id'])
    endpoint = f"/api/video/h3/jobs/{final['id']}"
    draft = hunter.client().get(endpoint + '/edit').json()['config']
    before = (directory / 'manifest.json').read_bytes()
    response = hunter.client().patch(endpoint, data={'config': json.dumps(draft), 'revision': '0',
        'retain_inputs': json.dumps({'reference_videos': [1, 0]})})
    assert response.status_code == 400 and 'original inputs' in response.text
    assert (directory / 'manifest.json').read_bytes() == before


def test_final_explicit_legacy_lora_selection_is_not_shadowed_by_previous_stack(hunter):
    from tests.test_h3_video import weights
    manager = hunter.managers['h3']
    adapter = weights(manager.project / 'models/minimax_h3_ref2va_lora_new.safetensors', lora=True)
    inventory = hunter.inventories['h3']
    inventory['components'] = h3.discover_components([adapter.parent])
    adapter_id = next(item['id'] for item in inventory['components'] if item['name'] == adapter.name)
    job, _ = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize",
                                   json=final_body(hunter, lora=adapter_id, lora_scale=.7))
    assert response.status_code == 201, response.text
    saved = h3._read(manager.directory(response.json()['id']) / 'manifest.json')['config']
    assert saved['loras'] == [{'path': str(adapter), 'strength': .7}]
