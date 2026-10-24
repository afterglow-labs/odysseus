"""Isolated script-import probe; real worker/Scout entrypoint, CPU sample stand-ins."""
import functools
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

project, output = map(Path, sys.argv[1:3])
mode = sys.argv[3] if len(sys.argv) > 3 else 'interactive'
sys.path.insert(0, str(project / 'scripts'))
assert importlib.util.find_spec('scripts') is None, 'Probe must match script launch without the project package path'
import h3_video_worker as worker
import h3_seed_hunter as hunter
import h3_seed_scout as scout
import h3_attention_memory

# Import the installed inference modules in CPU mode, including the actual
# upstream Scout class. A CUDA initialization would fail this subprocess.
os.environ['CUDA_VISIBLE_DEVICES'] = ''
runtime = project / 'runtimes/minimax-h3/ComfyUI'
sys.path.insert(0, str(runtime))
import comfy.options
comfy.options.enable_args_parsing(False)
from comfy.cli_args import args
args.cpu = True
import torch
def no_cuda(*_args, **_kwargs):
    raise AssertionError('The worker import regression must not initialize CUDA')
torch.cuda._lazy_init = no_cuda
from comfy_extras.nodes_custom_sampler import BasicGuider, BasicScheduler
source = scout.load_source(runtime)
scout.load_source = lambda _runtime: source
calls = []
callbacks = []
progress_updates = []

def prepare_callback(_model, total, output):
    def callback(step, x0, _x, _total):
        callbacks.append((step, total))
        output['x0'] = x0
    return callback

source.latent_preview.prepare_callback = prepare_callback

class Guider:
    model_patcher = SimpleNamespace(model=SimpleNamespace(latent_format=None, process_latent_out=lambda value: value))

    def sample(self, noise, samples, _sampler, sigmas, *, callback, seed, **_kwargs):
        continuing = sigmas[0].item() == 17
        if continuing:
            assert noise.count_nonzero() == 0
            calls.append(['continue', seed, sigmas.tolist(), samples.tolist()])
            result = samples + 1000
            x0 = result
        else:
            calls.append(['full' if len(sigmas) == 21 else 'scout', seed, sigmas.tolist()])
            result = torch.tensor([float(seed)])
            x0 = result + 200
        for step in range(len(sigmas) - 1):
            callback(step, x0, result, len(sigmas) - 1)
        return result

BasicGuider.execute = classmethod(lambda cls, *_: (Guider(),))
BasicScheduler.execute = classmethod(lambda cls, *_: (torch.arange(20., -1., -1.),))
source.MiniMaxH3SeedScoutSampler._prepare_latent = staticmethod(lambda *_: ({}, torch.zeros(1)))
source._vae_preview_images = lambda *_: torch.zeros((1, 2, 2, 3))
source._latent2rgb_images = lambda *_: torch.zeros((1, 2, 2, 3))
atomic_json = worker.atomic_json

def record_progress(path, value):
    if Path(path).name == 'progress.json':
        progress_updates.append(value.copy())
    atomic_json(path, value)

worker.atomic_json = record_progress

class Runtime:
    def __init__(self, *_):
        pass
    def load(self, *_):
        return (object(),) * 4
    def cleanup_model_state(self):
        pass
    def release_conditioning(self):
        pass
    def decode(self, samples, *_):
        assert samples['samples'].item() == (1043 if mode == 'interactive' else 242)
        return torch.zeros((73, 2, 2, 3)), {}

hunter.patch_model = lambda _runtime, _config, model, _path: model
hunter.condition = lambda *_: ([[torch.tensor([7.]), {}]], {'samples': torch.zeros(1)}, [[torch.tensor([7.]), {}]])
worker.run_job = functools.partial(worker.run_job, runtime_factory=Runtime, media_loader=lambda *_: {},
                                  muxer=lambda path, *_: Path(path).write_bytes(b'cpu-mp4'))
output.mkdir(parents=True, exist_ok=True)
config = dict(recipe='seed_hunter', mode='ref2va', prompt='A neutral sphere rolls.',
              width=480, height=864, frames=73, steps=20, seed=42, gpu='0',
              sampler='euler', scheduler='simple', reference_size='max',
              seed_hunter={'stage': 'scout', 'scout_count': 3, 'scout_step': 3,
                           'scout_mode': 'interactive' if mode == 'automatic' else mode, 'scout_timeout': 1})
if mode == 'automatic':
    config['seed_hunter']['auto_refine'] = {'candidates': 'all', 'project': 'Import regression'}
for key in ('model', 'encoder', 'video_vae', 'audio_vae'):
    path = output / f'{key}.safetensors'
    path.touch()
    config[key] = str(path)
manifest = dict(config=config, uploads={}, output_path=str(output / 'output.mp4'),
                runtime_path=str(runtime), status_path=str(output / 'progress.json'))
manifest_path = output / 'manifest.json'
manifest_path.write_text(json.dumps(manifest))
(output / 'scout-selection.json').write_text(json.dumps({'seed': 43}))
status = worker.main(['--job', str(manifest_path)])
if status:
    raise SystemExit(status)
previews = [['scout', seed, [20., 19., 18., 17.]] for seed in (42, 43, 44)]
if mode == 'interactive':
    assert calls == previews + [['continue', 43, list(range(17, -1, -1)), [43.]]]
    assert [total for _, total in callbacks] == [3] * 9 + [17] * 17
elif mode in {'scout', 'automatic'}:
    assert calls == previews
    assert [total for _, total in callbacks] == [3] * 9
else:
    assert calls == [['full', 42, list(range(20, -1, -1))]]
    assert [total for _, total in callbacks] == [20] * 20
continuation = [row for row in progress_updates if row['phase'] == 'continuing_selected_seed']
assert bool(continuation) == (mode == 'interactive')
if continuation:
    assert continuation[0]['step'] == 0 and continuation[-1]['step'] == 17
    assert [row['step'] for row in continuation if row['step']] == list(range(1, 18))
    assert [row['schedule_step'] for row in continuation if row['step']] == list(range(4, 21))
assert all(row['total_steps'] == 17 and row['seed'] == 43 for row in continuation)
assert all(row['schedule_total_steps'] == 20 for row in continuation)
chosen = 43 if mode == 'interactive' else 42
completed_steps = 3 if mode in {'scout', 'automatic'} else 20
saved = json.loads((output / 'candidate-1.json').read_text())
assert saved['seed'] == chosen
assert json.loads((output / 'scout.json').read_text())['selected_seed'] == chosen
assert json.loads((output / 'progress.json').read_text())['phase'] == 'completed'
assert json.loads((output / 'progress.json').read_text())['seed'] == chosen
assert json.loads((output / 'progress.json').read_text())['step'] == completed_steps
assert json.loads((output / 'progress.json').read_text())['total_steps'] == completed_steps
assert (output / 'output.mp4').read_bytes() == b'cpu-mp4'
print(f'isolated Scout entrypoint completed chosen seed {chosen}; no CUDA initialization')

if mode != 'final':
    # The losing candidate resumes from its own persisted noisy partial, never
    # its denoised preview and never a fresh 20-step pass.
    import copy
    child = output / 'child'
    child.mkdir()
    child_config = copy.deepcopy(config)
    child_config['seed'] = 44
    child_config['seed_hunter'].update(scout_mode='final', scout_count=1)
    child_config['seed_hunter'].pop('auto_refine', None)
    child_manifest = {**manifest, 'config': child_config, 'output_path':str(child/'output.mp4'),
        'status_path':str(child/'progress.json'), 'seed_hunter_input':{
            'candidate':44, 'latent':str(output/'scout-44.safetensors'), 'metadata':str(output/'scout-44.json')}}
    child_path = child/'manifest.json'
    child_path.write_text(json.dumps(child_manifest))
    def decode_resumed(self, samples, *_):
        assert samples['samples'].item() == 1044
        return torch.zeros((73,2,2,3)), {}
    Runtime.decode = decode_resumed
    hunter.condition = lambda *_: (_ for _ in ()).throw(AssertionError('Resume must reuse saved conditioning'))
    calls.clear()
    assert worker.main(['--job',str(child_path)]) == 0
    assert calls == [['continue',44,list(range(17,-1,-1)),[44.]]]
    assert (output/'output.mp4').read_bytes() == b'cpu-mp4'
    print('Losing seed 44 resumed its own exact 17 remaining updates')
