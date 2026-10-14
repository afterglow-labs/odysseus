"""Standalone Scout settings, batch submissions, saved workflows, and failures."""
import copy
import json
import uuid

import pytest

from src import h3_seed_scout as scout, h3_seed_hunter as hunter, h3_video as h3
from src.video_submission import HEADER
from src.video_workflow import export_options, make_recipe
from tests.test_video_submission import backend, image
from tests.test_video_workflow import transfer


def setup(backend):
    inventory = backend.inventories['h3']
    defaults = scout.defaults(inventory['components'], inventory['gpus'])
    inventory['seed_hunter'] = {'scout_defaults': defaults, 'features': {'seed_scout': True}}
    return {**defaults, 'prompt': 'A neutral sphere rolls.'}


def test_standalone_scout_capabilities_use_full_schedule_no_adapters_and_compatible_components(backend, tmp_path):
    inventory = backend.inventories['h3']
    before = copy.deepcopy(inventory)
    capability = hunter.capabilities(tmp_path, tmp_path / 'ComfyUI', inventory['components'], inventory['gpus'])
    raw = {**capability['scout_defaults'], 'prompt': 'A neutral sphere rolls.'}
    config = h3.validate_config(raw, inventory['components'], inventory['gpus'], {})
    assert config['steps'] == 20 and config['sampler'] == 'euler' and config['scheduler'] == 'simple'
    assert config['loras'] == [] and config['vae_gpu'] == ''
    assert config['seed_hunter']['stage'] == 'scout' and config['seed_hunter']['preview_count'] == 1
    assert {key: config['seed_hunter'][key] for key in ('scout_count', 'scout_step', 'scout_mode', 'scout_timeout')} == {
        'scout_count': 3, 'scout_step': 3, 'scout_mode': 'interactive', 'scout_timeout': 0}
    assert capability['defaults']['seed_hunter']['stage'] == 'preview'
    assert inventory == before


@pytest.mark.parametrize('field,filename,content', [
    ('reference_videos', 'reference.mp4', b'video'),
    ('reference_images', 'reference.png', None),
    ('reference_audio', 'reference.wav', b'audio'),
    (None, None, None),
])
def test_scout_varying_media_or_copies_keep_settings_and_idempotent_receipts(backend, field, filename, content):
    config = setup(backend)
    config['seed_hunter'].update(scout_count=4, scout_step=2, scout_timeout=30)
    keys = [uuid.uuid4().hex, uuid.uuid4().hex]
    jobs = []
    for index, key in enumerate(keys):
        files = {field: (f'{index}-{filename}', image() if content is None else content)} if field else {}
        reply = backend.client().post('/api/video/h3/jobs', data={'config': json.dumps(config)}, files=files, headers={HEADER: key})
        assert reply.status_code == 201, reply.text
        job = reply.json(); jobs.append(job['id'])
        manifest = h3._read(backend.managers['h3'].directory(job['id']) / 'manifest.json')
        assert manifest['config']['seed_hunter']['stage'] == 'scout'
        assert manifest['config']['seed_hunter']['scout_count'] == 4
        assert manifest['config']['seed_hunter']['scout_step'] == 2
        assert manifest['config']['seed_hunter']['scout_timeout'] == 30
        assert manifest['config']['loras'] == [] and manifest['config']['frames'] == 124
        if field:
            assert len(manifest[field]) == 1
        retry = backend.client().post('/api/video/h3/jobs', headers={HEADER: key})
        assert retry.status_code == 201 and retry.json()['id'] == job['id']
    assert len(set(jobs)) == len(backend.calls) == 2


def test_scout_export_import_preserves_mode_schedule_and_own_options(backend, tmp_path):
    config = setup(backend)
    config['seed_hunter'].update(scout_count=5, scout_step=4, scout_timeout=90, scout_preview_mode='latent2rgb')
    rows = backend.inventories['h3']['components']
    selected = {role: next(row['path'] for row in rows if row['id'] == config[role])
                for role in ('model', 'encoder', 'video_vae', 'audio_vae')}
    document, assets = make_recipe(config, selected, {}, 'h3', export_options({}))
    assert assets == []
    service, directory = transfer(tmp_path)
    path = directory / 'scout.json'; path.write_text(json.dumps(document))
    restored = service.import_file(directory, 'corey', path)['workflow']['config']
    assert restored['seed_hunter'] == config['seed_hunter']
    assert restored['steps'] == 20 and restored['sampler'] == 'euler' and restored['scheduler'] == 'simple'
    assert restored['lora_strengths'] == []


@pytest.mark.parametrize('receipt', [False, True])
def test_failed_scout_detail_reports_error_before_or_after_preview_and_stops_waiting(backend, receipt):
    config = setup(backend)
    reply = backend.client().post('/api/video/h3/jobs', data={'config': json.dumps(config)})
    assert reply.status_code == 201, reply.text
    identity = reply.json()['id']; directory = backend.managers['h3'].directory(identity)
    state = h3._read(directory / 'state.json')
    h3._write(directory / 'state.json', {**state, 'status': 'failed'})
    h3._write(directory / 'progress.json', {'phase': 'failed', 'error': 'RuntimeError: synthetic sampler failure'})
    if receipt:
        h3._write(directory / 'scout.json', {'version': 1, 'mode': 'interactive', 'status': 'waiting_for_selection', 'seeds': [42, 43]})
    url = f'/api/video/h3/jobs/{identity}/scout'
    response = backend.client().get(url)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'failed' and response.json()['job_id'] == identity
    assert 'synthetic sampler failure' in response.json()['error']
    assert backend.client().post(url + '/select', json={'seed': 42}).status_code == 409
    assert backend.client().get(url, headers={'x-user': 'other'}).status_code == 404


def test_queued_scout_summary_uses_its_configured_seeds_before_worker_initializes(backend):
    config=setup(backend)
    config['seed']=710
    config['seed_hunter']['scout_count']=4
    response=backend.client().post('/api/video/h3/jobs',data={'config':json.dumps(config)},headers={HEADER:uuid.uuid4().hex})
    assert response.status_code==201,response.text
    job=response.json()
    directory=backend.managers['h3'].directory(job['id'])
    assert not (directory/'scout.json').exists()
    reply=backend.client().get(f'/api/video/h3/jobs/{job["id"]}/scout')
    assert reply.status_code==200,reply.text
    assert reply.json()['seeds']==[710,711,712,713]
    assert reply.json()['previews']==[] and reply.json()['selected_seed'] is None
