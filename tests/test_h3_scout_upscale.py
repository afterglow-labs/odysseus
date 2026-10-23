"""Scout final output must traverse the same learned upscaler as Hunter."""
import json
from pathlib import Path

import pytest
import torch

from scripts import h3_seed_hunter as hunter, h3_seed_scout as scout, h3_video_worker as worker
from scripts.h3_seed_hunter_controls import validate_options, validate_scout_output
from tests.test_h3_seed_hunter_worker import manifest
from tests.test_video_submission import backend


def test_legacy_options_do_not_gain_unknown_keys():
    assert 'scout_upscale' not in validate_options({'stage': 'scout'})


@pytest.mark.parametrize('change', [
    {'scout_upscale': 1}, {'scout_final_width': 1001}, {'scout_final_height': float('nan')},
    {'scout_refine_steps': 0}, {'scout_refine_steps': 2.5},
])
def test_upscale_options_reject_invalid_values(change):
    with pytest.raises(ValueError):
        validate_options({'stage': 'scout', 'scout_upscale': True, **change})


def test_final_size_cannot_shrink_or_exceed_working_bounds():
    options = validate_options({'stage': 'scout', 'scout_upscale': True, 'scout_final_width': 256})
    with pytest.raises(ValueError, match='at least'):
        validate_scout_output({'width': 512, 'height': 512, 'seed_hunter': options})
    options = validate_options({'stage': 'scout', 'scout_upscale': True})
    with pytest.raises(ValueError, match='pixels'):
        validate_scout_output({'width': 2048, 'height': 2048, 'seed_hunter': options})


@pytest.mark.parametrize('mode,enabled,expected,automatic', [('interactive', True, True, False), ('final', True, True, False),
                                               ('scout', True, False, False), ('interactive', False, False, False),
                                               ('interactive', True, False, True)])
def test_selected_clean_latent_is_upscaled_then_refined_and_decoded_at_final_size(
        manifest, monkeypatch, mode, enabled, expected, automatic):
    manifest['config'].update(width=256, height=256, steps=20,
        seed_hunter={'stage': 'scout', 'scout_step': 4, 'scout_mode': mode,
                     'scout_upscale': enabled, 'scout_refine_steps': 2, 'scout_timeout': 0})
    if automatic:
        manifest['config']['seed_hunter']['auto_refine'] = {'project': 'Test batch', 'candidates': [1, 3]}
    calls = []
    positive = [[torch.ones(1), {'identity': 'saved references'}]]
    clean = {'samples': torch.tensor(12.)}
    class Runtime:
        def __init__(self, *_): pass
        def load(self, *_): return (object(),) * 4
        def cleanup_model_state(self): pass
        def release_conditioning(self): pass
        def decode(self, samples, *_):
            calls.append('decode')
            assert samples['samples'].item() == (14 if expected else 12)
            edge = 512 if expected else 256
            return torch.zeros((1, 1, 1, 3)).expand(73, edge, edge, 3), {}
    monkeypatch.setattr(hunter, 'patch_model', lambda _r, _c, model, _p: model)
    monkeypatch.setattr(hunter, 'condition', lambda *_: (positive, {'samples': torch.zeros(1)}, positive))
    def finish(_runtime, _manifest, config, *_args, base_positive=None):
        calls.append('finish chosen seed')
        assert config['steps'] == 20 and config['seed_hunter']['scout_step'] == 4
        if automatic:
            assert config['seed_hunter']['scout_mode'] == 'scout'
        return {'samples': torch.tensor(999.)}, clean, 43
    monkeypatch.setattr(scout, 'sample', finish)
    def upscale(_manifest, config, latent, _progress):
        calls.append('learned latent upscale')
        assert latent is clean
        assert (config['width'], config['height']) == (512, 512)
        assert config['latent_upscaler'] == manifest['config']['latent_upscaler']
        return {'samples': torch.tensor(13.)}
    monkeypatch.setattr(hunter, 'upscale_latent', upscale)
    def refine(_runtime, config, _model, conditioning, latent, _progress):
        calls.append('refine')
        assert conditioning is positive and latent['samples'].item() == 13
        assert config['steps'] == 2 and config['seed'] == 43
        assert config['seed_hunter']['stage'] == 'final' and config['seed_hunter']['denoise'] == .4
        return {'samples': torch.tensor(1000.)}, {'samples': torch.tensor(14.)}
    monkeypatch.setattr(hunter, 'sample_candidate', refine)
    worker.run_job(manifest, runtime_factory=Runtime, media_loader=lambda *_: {},
                   muxer=lambda path, *_: Path(path).write_bytes(b'mp4'))
    assert calls == ['finish chosen seed', *(['learned latent upscale', 'refine'] if expected else []), 'decode']
    folder = Path(manifest['output_path']).parent
    saved = json.loads((folder / 'candidate-1.json').read_text())
    assert saved['seed'] == 43 and saved['config']['width'] == (512 if expected else 256)
    if expected:
        progress = json.loads(Path(manifest['status_path']).read_text())
        assert progress['latent_upscaled'] and (progress['width'], progress['height']) == (512, 512)


def test_upscale_requires_an_actual_weight_file_before_initializing_gpu(manifest):
    manifest['config']['latent_upscaler'] = ''
    manifest['config']['seed_hunter'] = {'stage': 'scout', 'scout_upscale': True}
    with pytest.raises(ValueError, match='latent upscaler'):
        worker.run_job(manifest, runtime_factory=lambda *_: pytest.fail('GPU must not initialize'))


def test_api_persists_upscaler_and_final_controls_in_saved_job_and_portable_workflow(backend, tmp_path):
    from tests.test_h3_seed_scout_backend import setup
    from tests.test_h3_video import weights
    from src.video_workflow import make_recipe, export_options
    config = setup(backend)
    path = weights(tmp_path / 'minimax_h3_latent_upscaler.safetensors')
    inventory = backend.inventories['h3']
    inventory['components'].append({'id': 'upscaler', 'role': 'latent_upscaler', 'name': path.name, 'path': str(path)})
    inventory['seed_hunter']['features'].update(latent_upscaler=True, scout_upscale=True)
    config['latent_upscaler'] = 'upscaler'
    config['seed_hunter'].update(scout_upscale=True, scout_final_width=1920, scout_final_height=1088,
                                 scout_refine_steps=3, scout_step=4)
    reply = backend.client().post('/api/video/h3/jobs', data={'config': json.dumps(config)})
    assert reply.status_code == 201, reply.text
    folder = backend.managers['h3'].directory(reply.json()['id'])
    saved = json.loads((folder / 'manifest.json').read_text())['config']
    assert saved['latent_upscaler'] == str(path)
    assert saved['seed_hunter']['scout_step'] == 4 and saved['seed_hunter']['scout_refine_steps'] == 3
    portable = make_recipe(saved, {}, {}, 'h3', export_options({}))[0]['config']
    assert portable['seed_hunter']['scout_final_width'] == 1920
    assert portable['seed_hunter']['scout_upscale'] is True
    edit = backend.client().post('/api/video/h3/queue/edit', json={
        'jobs': [{'id': reply.json()['id'], 'revision': 0}],
        'patch': {'latent_upscaler': 'upscaler', 'seed_hunter': {
            'scout_final_width': None, 'scout_final_height': None, 'scout_refine_steps': 2}}})
    assert edit.status_code == 200, edit.text
    edited = json.loads((folder / 'manifest.json').read_text())['config']
    assert edited['seed_hunter']['scout_step'] == 4
    assert edited['seed_hunter']['scout_upscale'] is True
    assert edited['seed_hunter']['scout_final_width'] is None
    assert edited['seed_hunter']['scout_final_height'] is None
    assert edited['seed_hunter']['scout_refine_steps'] == 2
    assert edited['latent_upscaler'] == str(path)
    assert edited['frames'] == saved['frames'] and edited['prompt'] == saved['prompt']
    config['seed_hunter']['scout_final_width'] = 256
    failed = backend.client().post('/api/video/h3/jobs', data={'config': json.dumps(config)})
    assert failed.status_code == 400 and 'at least' in failed.text


def test_bulk_scout_finishes_its_own_partial_before_shared_refinement(manifest, monkeypatch):
    from copy import deepcopy
    manifest['config'].update(width=256, height=256, steps=20, seed=911,
        seed_hunter={'stage':'scout', 'scout_step':4, 'scout_mode':'final', 'scout_count':1})
    config, _ = worker.validate_job(manifest)
    manifest['scout_refinement'] = {**deepcopy(config), 'width':512, 'height':512, 'steps':7,
        'sampler':'er_sde', 'scheduler':'linear_quadratic',
        'seed_hunter':{**config['seed_hunter'], 'stage':'final', 'denoise':.2}}
    manifest['seed_hunter_input'] = {'latent':'private partial'}
    conditioned = [[torch.ones(1), {'own refs':'source 911'}]]
    partial = {'samples':torch.tensor(11.)}; finished = {'samples':torch.tensor(12.)}
    calls=[]
    class Runtime:
        def __init__(self, *_): pass
        def load(self, cfg, *_):
            assert cfg['steps']==20 and cfg['seed']==911
            return (object(),)*4
        def cleanup_model_state(self): pass
        def release_conditioning(self): pass
        def decode(self, samples, *_):
            assert samples['samples'].item()==14
            calls.append('decode');return torch.zeros((1,1,1,3)).expand(73,512,512,3),{}
    monkeypatch.setattr(hunter,'patch_model',lambda _r,_c,model,_p:model)
    monkeypatch.setattr(hunter,'load_snapshot',lambda *_:({'sampling':conditioned,'refinement':conditioned},partial))
    def resume(_r, _m, cfg, _model, positive, latent, *_args, **_kwargs):
        assert cfg['steps']==20 and cfg['seed_hunter']['scout_step']==4 and cfg['seed']==911
        assert latent is partial and positive is conditioned
        calls.append('remaining 16 steps');return finished,finished,911
    monkeypatch.setattr(scout,'resume',resume)
    monkeypatch.setattr(scout,'sample',lambda *_a,**_kw:pytest.fail('Never restart the scouting schedule'))
    def upscale(_m,cfg,latent,_p):
        assert latent is finished and cfg['width']==512
        calls.append('upscale');return {'samples':torch.tensor(13.)}
    monkeypatch.setattr(hunter,'upscale_latent',upscale)
    def refine(_r,cfg,_model,positive,latent,_p):
        assert cfg['steps']==7 and cfg['seed']==911 and cfg['scheduler']=='linear_quadratic'
        assert cfg['seed_hunter']['denoise']==.2 and positive is conditioned and latent['samples'].item()==13
        calls.append('shared refinement');return {},{'samples':torch.tensor(14.)}
    monkeypatch.setattr(hunter,'sample_candidate',refine)
    worker.run_job(manifest,runtime_factory=Runtime,media_loader=lambda *_:pytest.fail('Saved references must be reused'),
        muxer=lambda path,*_:Path(path).write_bytes(b'finished'))
    assert calls==['remaining 16 steps','upscale','shared refinement','decode']
    progress = json.loads(Path(manifest['status_path']).read_text())
    assert progress['total_steps'] == 7 and progress['step'] == 7
