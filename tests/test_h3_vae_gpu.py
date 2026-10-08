"""CPU checks for safe H3 component placement across two visible CUDA devices."""
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from scripts import h3_video_worker as worker

PRIMARY = 'GPU-00000000-0000-0000-0000-000000000001'
SECONDARY = 'GPU-00000000-0000-0000-0000-000000000002'


@pytest.fixture
def manifest(tmp_path):
    config = {'gpu': PRIMARY, 'vae_gpu': SECONDARY, 'mode': 't2va', 'prompt': 'A red ball.',
              'width': 64, 'height': 32, 'frames': 124, 'steps': 4}
    for name in ('model', 'encoder', 'video_vae', 'audio_vae'):
        path = tmp_path / (name + '.safetensors')
        path.touch()
        config[name] = str(path)
    return {'config': config, 'uploads': {}, 'runtime_path': str(tmp_path / 'runtime'),
            'output_path': str(tmp_path / 'out.mp4'), 'status_path': str(tmp_path / 'status.json')}


@pytest.mark.parametrize('vae,expected', [('', (PRIMARY,)), (None, (PRIMARY,)),
                                          (PRIMARY, (PRIMARY,)), (SECONDARY, (PRIMARY, SECONDARY))])
def test_visible_order_deduplicates_same_card_and_preserves_default(manifest, vae, expected):
    manifest['config']['vae_gpu'] = vae
    config, _ = worker.validate_job(manifest)
    assert worker.visible_gpu_ids(config) == expected
    assert config['vae_gpu'] == (expected[1] if len(expected) > 1 else '')
    assert worker.visible_gpu_ids({'gpu': '0'}) == ('0',)  # Existing standalone manifests remain valid.


@pytest.mark.parametrize('vae', [0, '0', [], {}, '0,1', PRIMARY + ',' + SECONDARY, 'GPU-not-a-uuid'])
def test_invalid_secondary_selector_fails_before_runtime(manifest, vae):
    manifest['config']['vae_gpu'] = vae
    with pytest.raises(ValueError, match='VAE GPU'):
        worker.run_job(manifest, runtime_factory=lambda _: pytest.fail('Must reject before runtime import'))


def test_worker_replaces_inherited_primary_only_visibility_before_initialization(manifest, monkeypatch):
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', PRIMARY)
    events = []

    class Runtime:
        def __init__(self, path):
            assert os.environ['CUDA_VISIBLE_DEVICES'] == PRIMARY + ',' + SECONDARY
            events.append('init')
        def load(self, config, progress):
            assert config['gpu'] == PRIMARY and config['vae_gpu'] == SECONDARY
            events.append('load')
            return 'model', 'clip', 'video-vae', 'audio-vae'
        def condition(self, *args):
            return 'positive', 'latent'
        def generate(self, *args):
            return 'samples'
        def decode(self, *args):
            return [0] * 124, 'audio'

    def prepare(*args):
        assert events == ['init']
        return 'media'

    worker.run_job(manifest, runtime_factory=Runtime, media_loader=prepare,
                   muxer=lambda path, *_: Path(path).write_bytes(b'fixture-MP4'))
    assert events == ['init', 'load']
    assert json.loads(Path(manifest['status_path']).read_text())['phase'] == 'completed'


@pytest.fixture
def runtime(monkeypatch):
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    calls = []
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: True)
    monkeypatch.setattr(torch.cuda, 'device_count', lambda: 2)
    monkeypatch.setattr(torch.cuda, 'get_device_capability', lambda device: (12, 0) if device.index == 0 else (8, 9))
    monkeypatch.setattr(torch.cuda, 'get_device_name', lambda device: 'RTX 5090' if device.index == 0 else 'RTX 4090')

    def diffusion(path, **kwargs):
        calls.append(('model', kwargs))
        return SimpleNamespace(model=type('MiniMaxH3', (), {})(), patches={})

    def clip(paths, **kwargs):
        calls.append(('encoder', kwargs))
        return SimpleNamespace(patcher=SimpleNamespace(load_device=torch.device('cuda:0')))

    class VAE:
        def __init__(self, **kwargs):
            calls.append(('vae', kwargs))
            self.device = kwargs['device']
            self.output_device = torch.device('cpu')
            # Offloading remains recoverable; pinning this to cuda:1 would
            # prevent the core from freeing second-card VRAM under pressure.
            self.patcher = SimpleNamespace(load_device=self.device, offload_device=torch.device('cpu'))
        def throw_exception_if_invalid(self):
            pass

    runtime.sd = SimpleNamespace(load_diffusion_model=diffusion, load_clip=clip, VAE=VAE,
                                 CLIPType=SimpleNamespace(MINIMAX='minimax'))
    runtime.utils = SimpleNamespace(load_torch_file=lambda path, **kw: ({'path': path}, {'kind': 'fixture'}))
    return runtime, calls


def test_transformer_encoder_stay_primary_both_vaes_use_second_with_cpu_handoff(runtime, manifest, caplog):
    rt, calls = runtime
    config, _ = worker.validate_job(manifest)
    with caplog.at_level(logging.INFO):
        _, _, video, audio = rt.load(config, lambda *a: None)
    assert calls[0] == ('model', {'model_options': {'load_device': torch.device('cuda:0')}})
    assert calls[1] == ('encoder', {'clip_type': 'minimax', 'model_options': {'load_device': torch.device('cuda:0')}})
    assert [item[1]['device'] for item in calls[2:]] == [torch.device('cuda:1')] * 2
    for vae in (video, audio):
        assert vae.device == vae.patcher.load_device == torch.device('cuda:1')
        assert vae.output_device == vae.patcher.offload_device == torch.device('cpu')
    assert 'Transformer and Qwen encoder: cuda:0 (RTX 5090)' in caplog.text
    assert 'Video and audio VAEs: cuda:1 (RTX 4090)' in caplog.text


def test_default_keeps_all_components_on_primary(runtime, manifest):
    rt, calls = runtime
    manifest['config'].pop('vae_gpu')
    config, _ = worker.validate_job(manifest)
    _, _, video, audio = rt.load(config, lambda *a: None)
    assert video.device == audio.device == torch.device('cuda:0')
    assert rt._gpu_devices == (torch.device('cuda:0'),)


def test_missing_second_device_does_not_silently_run_vaes_on_primary(runtime, manifest, monkeypatch):
    rt, calls = runtime
    monkeypatch.setattr(torch.cuda, 'device_count', lambda: 1)
    config, _ = worker.validate_job(manifest)
    with pytest.raises(ValueError, match='not visible'):
        rt.load(config, lambda *a: None)
    assert calls == []


@pytest.mark.parametrize('nvfp4_component', ['model', 'encoder'])
def test_nvfp4_guard_applies_to_primary_even_if_secondary_is_blackwell(runtime, manifest, monkeypatch, nvfp4_component):
    rt, calls = runtime
    config, _ = worker.validate_job(manifest)
    config[nvfp4_component] = '/unused/weights_nvfp4.safetensors'
    checked = []
    def capability(device):
        checked.append(device.index)
        return (8, 9) if device.index == 0 else (12, 0)
    monkeypatch.setattr(torch.cuda, 'get_device_capability', capability)
    with pytest.raises(ValueError, match='Blackwell primary'):
        rt.load(config, lambda *a: None)
    assert checked == [0] and calls == []


def test_diagnostics_report_both_logical_devices_without_loading_more_weights(runtime, manifest, monkeypatch, caplog):
    rt, calls = runtime
    config, _ = worker.validate_job(manifest)
    rt.load(config, lambda *a: None)
    loaded_count = len(calls)
    checked = []
    def memory(device):
        checked.append(device.index)
        return 20 * 1024**3, 24 * 1024**3
    monkeypatch.setattr(torch.cuda, 'mem_get_info', memory)
    monkeypatch.setattr(torch.cuda, 'memory_allocated', lambda device: 2 * 1024**3)
    monkeypatch.setattr(torch.cuda, 'memory_reserved', lambda device: 3 * 1024**3)
    with caplog.at_level(logging.INFO):
        rt.log_gpu_memory('after conditioning')
    assert checked == [0, 1] and len(calls) == loaded_count
    assert 'cuda:0 (RTX 5090)' in caplog.text and 'cuda:1 (RTX 4090)' in caplog.text
    assert 'allocated 2048 MiB, reserved 3072 MiB, free 20480 / 24576 MiB' in caplog.text
