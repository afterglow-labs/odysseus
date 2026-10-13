"""Staged inference must preserve the selected latent and existing H3 behavior."""
import json
import shutil
import uuid
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
    config.update(recipe='seed_hunter', seed_hunter={'stage': 'preview', 'preview_count': 3},
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
        (directory / f'candidate-{i}.json').write_text(json.dumps({'chosen': i,
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


@pytest.mark.parametrize('patch', [{'prompt': 'change it'}, {'frames': 362}, {'encoder': '/etc/passwd'},
                                  {'seed_hunter': {'stage': 'preview'}}, {'latent_upscaler': '/etc/passwd'}])
def test_final_rejects_replacing_saved_conditioning_or_path_injection(hunter, patch):
    job, _ = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter, **patch))
    assert response.status_code == 400, response.text
    assert len(hunter.calls) == 1


def test_refinement_rejects_live_preview_and_direct_final_submission(hunter):
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


def test_final_rerun_preserves_latent_and_rejects_prompt_edits(hunter):
    job, directory = preview(hunter)
    response = hunter.client().post(f"/api/video/h3/jobs/{job['id']}/seed-hunter/finalize", json=final_body(hunter))
    assert response.status_code == 201, response.text
    final = response.json()
    manager = hunter.managers['h3']
    selected = [{'id': final['id'], 'revision': 0}]
    with pytest.raises(ValueError, match='source prompt'):
        rerun_jobs(manager, 'corey', 'h3', selected, {'prompt': 'Nope'}, str(uuid.uuid4()))
    result = rerun_jobs(manager, 'corey', 'h3', selected, {'steps': 4, 'seed_hunter': {'denoise': .3}}, str(uuid.uuid4()))
    assert len(hunter.calls) == 3
    reruns = [m for p in manager.root.iterdir() if p.is_dir() and (m := h3._read(p/'manifest.json')).get('config', {}).get('steps') == 4]
    assert len(reruns) == 1
    saved = reruns[0]
    assert saved['seed_hunter_input']['candidate'] == 2
    assert saved['config']['seed_hunter']['denoise'] == .3


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
