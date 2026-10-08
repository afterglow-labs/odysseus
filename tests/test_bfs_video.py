"""BFS API boundaries and actual timestamp/audio handling, with no GPU jobs."""
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
import pytest
import torch

from src import bfs_video as bfs, h3_video as h3
from routes.bfs_video_routes import setup_bfs_video_routes
from scripts import bfs_video_worker as worker
from scripts.bfs_h3 import build_prompt


@pytest.fixture
def inventory(tmp_path):
    names = ['minimax_h3_ref2va_nvfp4.safetensors', 'qwen3vl_32b_minimax_h3_nvfp4.safetensors',
             'minimax_h3_video_vae.safetensors', 'minimax_h3_audio_vae.safetensors',
             'minimax_h3_head_swap_v1.0_r32.safetensors',
             'ltx-2-dev-transformer.safetensors', 'ltx-2.3-dev-transformer.safetensors']
    root = tmp_path / 'cache' / 'snapshots' / 'revision'
    root.mkdir(parents=True)
    for name in names:
        (root / name).write_bytes(b'fixture weights, never loaded')
    return {'components': bfs.discover_components([root]), 'runtime_ready': True, 'runtime_error': '',
            'gpus': [{'id': 'GPU-00000000-0000-0000-0000-000000000001', 'nvfp4': True},
                     {'id': 'GPU-00000000-0000-0000-0000-000000000002', 'nvfp4': False}]}


def draft(inventory):
    workflow = bfs.BY_ID['h3_head_swap']
    components = {slot['key']: next(c['id'] for c in inventory['components'] if bfs.matches(c, slot, workflow))
                  for slot in workflow['slots'] if slot['required']}
    return {'workflow_id': workflow['id'], 'components': components,
            'gpu': inventory['gpus'][0]['id'], 'prompt': 'Keep the green jacket.'}


def required_uploads():
    return {'identity_image': [object()], 'source_video': [object()]}


def png():
    stream = io.BytesIO()
    Image.new('RGB', (32, 32), 'red').save(stream, 'PNG')
    return stream.getvalue()


def test_h3_saved_prompt_is_effective_prompt_and_integral_controls_are_normalized(inventory):
    raw = {**draft(inventory), 'width': 640.0, 'height': 384.0, 'frames': 124.0, 'steps': 20.0, 'seed': 42.0}
    saved = bfs.validate_config(raw, inventory, required_uploads())
    assert saved['prompt'] == build_prompt(raw['prompt'])
    for key in ('width', 'height', 'frames', 'steps', 'seed'):
        assert type(saved[key]) is int
    assert raw['prompt'] == 'Keep the green jacket.'


@pytest.mark.parametrize('change', [{'workflow_id': []}, {'workflow_id': {}}, {'components': []},
                                    {'width': 641}, {'frames': 125}, {'steps': True},
                                    {'sampler': []}, {'start_seconds': float('nan')},
                                    {'worker_path': '/tmp/other.py'}])
def test_invalid_config_is_a_validation_error(inventory, change):
    with pytest.raises(ValueError):
        bfs.validate_config({**draft(inventory), **change}, inventory, required_uploads())


def test_roles_gpu_and_required_uploads_are_enforced(inventory):
    raw = draft(inventory)
    wrong = {**raw, 'components': {**raw['components'], 'encoder': raw['components']['audio_vae']}}
    with pytest.raises(ValueError, match='encoder'):
        bfs.validate_config(wrong, inventory, required_uploads())
    with pytest.raises(ValueError, match='Blackwell'):
        bfs.validate_config({**raw, 'gpu': inventory['gpus'][1]['id']}, inventory, required_uploads())
    with pytest.raises(ValueError, match='source video'):
        bfs.validate_config(raw, inventory, {'identity_image': [object()]})
    with pytest.raises(ValueError, match='mask video'):
        bfs.validate_config(raw, inventory, {**required_uploads(), 'mask_video': [object()]})
    with pytest.raises(ValueError, match='identity image'):
        bfs.validate_config(raw, inventory, {**required_uploads(), 'identity_image': [object(), object()]})


def test_discovery_deduplicates_snapshot_links_and_ltx2_cannot_select_ltx23(tmp_path):
    target = tmp_path / 'blobs' / 'hash'
    target.parent.mkdir()
    target.write_bytes(b'weights with enough bytes')
    first = tmp_path / 'snapshots' / 'a'
    second = tmp_path / 'snapshots' / 'b'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    for directory in (first, second):
        (directory / 'ltx-2.3-dev-transformer.safetensors').symlink_to(target)
    (first / 'ltx-2.3-dev-transformer.gguf').write_bytes(b'GGUF')
    components = bfs.discover_components([tmp_path])
    assert len(components) == 1
    workflow = bfs.BY_ID['ltx2_v1']
    model_slot = next(s for s in workflow['slots'] if s['key'] == 'model')
    assert not bfs.matches(components[0], model_slot, workflow)


@pytest.fixture
def api(tmp_path, monkeypatch, inventory):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    manager = bfs.BFSJobManager(tmp_path / 'bfs', gallery_directory=tmp_path / 'gallery')
    monkeypatch.setattr(manager, 'inventory', lambda: inventory)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user in {'corey', 'other'})

    @app.middleware('http')
    async def identity(request, call_next):
        request.state.current_user = request.headers.get('x-user', 'corey')
        return await call_next(request)

    app.include_router(setup_bfs_video_routes(manager))
    return TestClient(app, raise_server_exceptions=False), manager, inventory


def test_routes_reject_nonadmin_before_any_job_or_inventory_access(api):
    client, manager, _ = api
    for method, path in [('get', '/inventory'), ('get', '/jobs'), ('post', '/jobs'),
                         ('delete', '/jobs/' + 'a' * 32), ('get', '/jobs/' + 'a' * 32 + '/video')]:
        assert getattr(client, method)('/api/video/bfs' + path, headers={'x-user': 'reader'}).status_code == 403
    assert not manager.root.exists()


def test_upload_stages_safe_paths_keeps_prompt_and_rejects_bad_or_duplicate_media(api, monkeypatch):
    client, manager, inventory = api
    captured = []

    def launch(directory, owner, config, uploads):
        captured.append((directory, config, uploads))
        assert all(Path(value).parent == directory for value in uploads.values())
        assert all(Path(value).stat().st_mode & 0o777 == 0o600 for value in uploads.values())
        return {'id': directory.name, 'prompt': config['prompt'], 'status': 'queued'}

    monkeypatch.setattr(manager, 'launch', launch)
    files = [('identity_image', ('../../escape.png', png(), 'image/png')),
             ('source_video', ('body.mp4', b'video fixture', 'video/mp4'))]
    response = client.post('/api/video/bfs/jobs', data={'config': json.dumps(draft(inventory))}, files=files)
    assert response.status_code == 201, response.text
    assert response.json()['prompt'] == build_prompt('Keep the green jacket.')
    assert not (manager.root.parent / 'escape.png').exists()
    assert not captured[0][0].exists()  # No published job receipt => staging cleaned up.
    files[0] = ('identity_image', ('fake.png', b'not an image', 'image/png'))
    response = client.post('/api/video/bfs/jobs', data={'config': json.dumps(draft(inventory))}, files=files)
    assert response.status_code == 400
    files[0] = ('identity_image', ('face.png', png(), 'image/png'))
    response = client.post('/api/video/bfs/jobs', data={'config': json.dumps(draft(inventory))}, files=files + files[:1])
    assert response.status_code == 400
    response = client.post('/api/video/bfs/jobs', data={'config': json.dumps({**draft(inventory), 'workflow_id': []})}, files=files)
    assert response.status_code == 400
    assert len(captured) == 1


def test_stream_cap_rejects_false_small_content_length_before_staging(api, monkeypatch):
    client, manager, inventory = api
    monkeypatch.setenv('ODYSSEUS_CHAT_UPLOAD_MAX_BYTES', '16')
    response = client.post('/api/video/bfs/jobs', headers={'Content-Length': '0'},
                           data={'config': json.dumps(draft(inventory))},
                           files=[('identity_image', ('face.png', png(), 'image/png')),
                                  ('source_video', ('body.mp4', b'x' * 70000, 'video/mp4'))])
    assert response.status_code == 413
    assert not manager.root.exists()


def test_owner_scoped_history_playback_and_cancel_waits_for_confirmed_stop(api, monkeypatch):
    client, manager, _ = api
    directory = manager.stage()
    h3._write(directory / 'state.json', {'id': directory.name, 'owner': 'corey', 'status': 'running',
                                        'phase': 'sampling', 'steps': 20, 'created_at': time.time(),
                                        'supervisor': h3._identity(os.getpid())})
    h3._write(directory / 'manifest.json', {'config': {'workflow_id': 'h3_head_swap',
                                                      'workflow_label': 'H3 head swap', 'prompt': 'saved prompt'}})
    stopped = []
    monkeypatch.setattr(h3, 'stop_workers', lambda *a, **kw: stopped.append(a) or False)
    base = '/api/video/bfs/jobs/' + directory.name
    other_jobs = client.get('/api/video/bfs/jobs', headers={'x-user': 'other'}).json()
    assert other_jobs['jobs'] == []
    assert other_jobs['queue']['total_queued'] == other_jobs['queue']['total_running'] == 0
    assert client.get(base + '/video', headers={'x-user': 'other'}).status_code == 404
    assert client.delete(base, headers={'x-user': 'other'}).status_code == 404
    assert stopped == []
    assert client.delete(base).status_code == 409
    assert h3._read(directory / 'state.json')['status'] == 'running'
    monkeypatch.setattr(h3, 'stop_workers', lambda *a, **kw: True)
    response = client.delete(base)
    assert response.status_code == 200 and response.json()['status'] == 'stopped'
    assert response.json()['prompt'] == 'saved prompt'
    state = h3._read(directory / 'state.json')
    state.update(status='completed', filename='result.mp4')
    h3._write(directory / 'state.json', state)
    (directory / 'output.mp4').write_bytes(b'fixture-MP4')
    assert manager.view(directory.name, 'corey')['url'] == base + '/video'
    assert client.get(base + '/video').content == b'fixture-MP4'


def video_fixture(path, *, delayed_audio=False):
    av = pytest.importorskip('av')
    with av.open(str(path), 'w') as output:
        video = output.add_stream('libx264', rate=30)
        video.width, video.height, video.pix_fmt = 64, 32, 'yuv420p'
        audio = output.add_stream('aac', rate=32000) if delayed_audio else None
        if audio:
            audio.layout = 'stereo'
        for index in range(60):
            frame = av.VideoFrame.from_ndarray(np.full((32, 64, 3), index * 4, np.uint8), format='rgb24')
            frame.pts, frame.time_base = index, Fraction(1, 30)
            for packet in video.encode(frame):
                output.mux(packet)
        for packet in video.encode():
            output.mux(packet)
        if audio:
            wave = (.2 * np.sin(np.arange(32000) * 2 * np.pi * 440 / 32000)).astype(np.float32)
            for index in range(0, 32000, 1024):
                frame = av.AudioFrame.from_ndarray(np.stack([wave[index:index + 1024]] * 2), format='fltp', layout='stereo')
                frame.sample_rate, frame.pts, frame.time_base = 32000, 16000 + index, Fraction(1, 32000)
                for packet in audio.encode(frame):
                    output.mux(packet)
            for packet in audio.encode():
                output.mux(packet)


def test_real_video_segment_resamples_30fps_and_uses_requested_start(tmp_path):
    path = tmp_path / 'source.mp4'
    video_fixture(path)
    config = {'fps': 24, 'frames': 24, 'start_seconds': .5, 'width': 64, 'height': 32, 'resize_mode': 'contain'}
    frames = worker.read_segment(path, config)
    assert frames.shape == (24, 32, 64, 3)
    assert 56 <= frames[0].mean().item() * 255 <= 64
    assert 168 <= frames[-1].mean().item() * 255 <= 180
    assert worker.read_source_audio(path, .5, 1) is None


def test_real_source_audio_preserves_delayed_track_against_video_timeline(tmp_path):
    path = tmp_path / 'delayed.mp4'
    video_fixture(path, delayed_audio=True)
    audio = worker.read_source_audio(path, 0, 2)
    waveform = audio['waveform'].numpy()
    assert waveform.shape == (1, 2, 64000)
    # Source audio begins at 0.5s. Normalizing against audio.start_time would
    # incorrectly shift it to the beginning and desynchronize the head motion.
    assert np.abs(waveform[..., :12000]).max() < .005
    assert np.abs(waveform[..., 19000:25000]).mean() > .05
    trimmed = worker.read_source_audio(path, .75, .5)['waveform'].numpy()
    assert np.abs(trimmed[..., :8000]).mean() > .05


def test_prepare_media_uses_one_grid_trim_for_video_audio_and_mask(monkeypatch):
    config = {'workflow_id': 'ltx2_v2', 'frames': 121, 'fps': 24, 'start_seconds': 3.5, 'audio_mode': 'source'}
    paths = {'source_video': 'source', 'identity_image': 'face', 'mask_video': 'mask'}
    monkeypatch.setattr(worker, 'read_segment', lambda path, cfg: torch.ones((50, 32, 64, 3)))
    monkeypatch.setattr(worker, 'read_image', lambda *a: 'identity')
    calls = []
    monkeypatch.setattr(worker, 'read_source_audio', lambda *a: calls.append(a) or 'source-audio')
    media = worker.prepare_media(paths, config)
    assert config['frames'] == 49
    assert len(media['source_video']) == len(media['mask_video']) == 49
    assert calls == [('source', 3.5, 49 / 24)]
    config['audio_mode'] = 'silent'
    calls.clear()
    assert worker.prepare_media(paths, config)['source_audio'] is None and calls == []


def test_h3_and_bfs_accept_jobs_behind_legacy_running_render(tmp_path):
    managers = [h3.H3JobManager(tmp_path / 'video_jobs' / 'h3'),
                bfs.BFSJobManager(tmp_path / 'video_jobs' / 'bfs')]
    for active, incoming in (managers, managers[::-1]):
        directory = active.stage()
        h3._write(directory / 'state.json', {'id': directory.name, 'owner': 'corey', 'status': 'running',
                                            'created_at': time.time(), 'supervisor': h3._identity(os.getpid())})
        waiting = incoming.stage()
        try:
            job = incoming.launch(waiting, 'other', {'steps': 20}, {})
            assert job['status'] == 'queued' and job['queue_position'] == 1
            time.sleep(.6)
            assert h3._read(waiting / 'state.json')['status'] == 'queued'
            assert not (waiting / 'processes.json').exists()
        finally:
            incoming.cancel(waiting.name, 'other')
        state = h3._read(directory / 'state.json')
        state['status'] = 'stopped'
        h3._write(directory / 'state.json', state)


def test_shared_worker_initializes_before_media_and_muxes_duration_matched_silence(inventory, tmp_path):
    saved = bfs.validate_config({**draft(inventory), 'frames': 5}, inventory, required_uploads())
    identity = tmp_path / 'identity.png'
    identity.write_bytes(png())
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'no decode in this dispatch test')
    manifest = {'config': saved, 'identity_image': str(identity), 'source_video': str(source),
                'runtime_path': str(tmp_path / 'runtime'), 'output_path': str(tmp_path / 'result.mp4'),
                'status_path': str(tmp_path / 'progress.json')}
    events = []
    runtime = object()

    def initialize(path):
        assert os.environ['CUDA_VISIBLE_DEVICES'] == saved['gpu']
        events.append('init')
        return runtime

    def load_media(paths, config):
        assert events == ['init']
        events.append('media')
        return 'media'

    def generate(config, media, progress, **kwargs):
        assert kwargs['runtime'] is runtime and media == 'media'
        assert config['prompt'] == saved['prompt']
        events.append('generate')
        return torch.zeros((5, 32, 64, 3)), None

    def mux(path, frames, audio, fps):
        assert fps == 24 and len(frames) == 5
        assert audio['sample_rate'] == 32000
        assert audio['waveform'].shape == (1, 2, round(5 / 24 * 32000))
        assert audio['waveform'].count_nonzero() == 0
        events.append('mux')
        Path(path).write_bytes(b'fixture-MP4')

    worker.run_job(manifest, runtime_factory=initialize, media_loader=load_media, generator=generate, muxer=mux)
    assert events == ['init', 'media', 'generate', 'mux']
    assert h3._read(manifest['status_path'])['phase'] == 'completed'
