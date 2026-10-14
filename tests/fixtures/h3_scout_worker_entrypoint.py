"""Isolated script-import probe; real worker/Scout entrypoint, CPU sample stand-ins."""
import functools
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

project, output = map(Path, sys.argv[1:])
sys.path.insert(0, str(project / 'scripts'))
assert importlib.util.find_spec('scripts') is None, 'Probe must match script launch without the project package path'
import h3_video_worker as worker
import h3_seed_hunter as hunter
import h3_seed_scout as scout

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
guider = SimpleNamespace(model_patcher=SimpleNamespace(model=SimpleNamespace(latent_format=None)))
BasicGuider.execute = classmethod(lambda cls, *_: (guider,))
BasicScheduler.execute = classmethod(lambda cls, *_: (torch.tensor([6., 4., 2., 0.]),))

calls = []
source.MiniMaxH3SeedScoutSampler._prepare_latent = staticmethod(lambda *_: ({}, torch.zeros(1)))
def sample_seed(self, _guider, _sampler, sigmas, _latent, _samples, seed):
    calls.append(['scout', seed, sigmas.tolist()])
    return {'samples': torch.tensor([float(seed)])}, {'samples': torch.tensor([float(seed + 100)])}, torch.tensor([float(seed + 200)])
def sample_continue(self, _guider, _sampler, sigmas, _latent, partial, seed):
    calls.append(['continue', seed, sigmas.tolist(), partial.tolist()])
    return {'samples': partial}, {'samples': partial + 1000}, None
source.MiniMaxH3SeedScoutSampler._sample_seed = sample_seed
source.MiniMaxH3SeedScoutSampler._sample_continue = sample_continue
source._vae_preview_images = lambda *_: torch.zeros((1, 2, 2, 3))
source._latent2rgb_images = lambda *_: torch.zeros((1, 2, 2, 3))

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
        assert samples['samples'].item() == 1043
        return torch.zeros((73, 2, 2, 3)), {}

hunter.patch_model = lambda _runtime, _config, model, _path: model
hunter.condition = lambda *_: ([[torch.tensor([7.]), {}]], {'samples': torch.zeros(1)}, [[torch.tensor([7.]), {}]])
worker.run_job = functools.partial(worker.run_job, runtime_factory=Runtime, media_loader=lambda *_: {},
                                  muxer=lambda path, *_: Path(path).write_bytes(b'cpu-mp4'))
output.mkdir(parents=True, exist_ok=True)
config = dict(recipe='seed_hunter', mode='ref2va', prompt='A neutral sphere rolls.',
              width=480, height=864, frames=73, steps=3, seed=42, gpu='0',
              sampler='euler', scheduler='simple', reference_size='max',
              seed_hunter={'stage': 'scout', 'scout_count': 3, 'scout_step': 1,
                           'scout_mode': 'interactive', 'scout_timeout': 1})
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
assert calls == [['scout', seed, [6., 4.]] for seed in (42, 43, 44)] + [['continue', 43, [4., 2., 0.], [43.]]]
saved = json.loads((output / 'candidate-1.json').read_text())
assert saved['seed'] == 43
assert json.loads((output / 'scout.json').read_text())['selected_seed'] == 43
assert json.loads((output / 'progress.json').read_text())['phase'] == 'completed'
assert (output / 'output.mp4').read_bytes() == b'cpu-mp4'
print('isolated Scout entrypoint completed chosen seed 43; no CUDA initialization')
