import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.cookbook_gpu import process_start_time, wsl_gpu_processes
from routes.cookbook_helpers import _parse_serve_phase, _diagnose_serve_output
import routes.cookbook_routes as cookbook


def test_wsl_discovers_linux_device_holders_without_nvml(tmp_path):
    def process(pid, name, parent=1, gpu=True):
        directory = tmp_path / str(pid)
        (directory / 'fd').mkdir(parents=True)
        (directory / 'comm').write_text(name)
        (directory / 'status').write_text(f'PPid:\t{parent}\n')
        (directory / 'stat').write_text(f'{pid} ({name}) S ' + ' '.join(['0'] * 18 + ['123456']))
        (directory / 'fd' / '3').symlink_to('/dev/dxg' if gpu else '/dev/null')
    process(200, 'odysseus', 100)
    process(100, 'python')
    process(300, 'llama-server')
    process(400, 'python', gpu=False)
    process(500, 'nvidia-smi')
    found = wsl_gpu_processes(tmp_path, exclude_pid=200)
    assert [p['pid'] for p in found] == [300]
    assert found[0]['used_mb'] is None
    assert found[0]['start_time'] == process_start_time(300, tmp_path) == '123456'


@pytest.mark.asyncio
async def test_wsl_gpu_probe_rejects_host_pids_and_keeps_na_memory(tmp_path, monkeypatch):
    import src.cookbook_gpu as gpu
    monkeypatch.setattr(cookbook, 'require_admin', lambda request: None)
    monkeypatch.setattr(cookbook, 'COOKBOOK_STATE_FILE', str(tmp_path / 'state.json'))
    original_exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda p: True if str(p) == '/dev/dxg' else original_exists(p))
    monkeypatch.setattr(gpu, 'wsl_gpu_processes', lambda: [
        {'pid': 300, 'name': 'llama-server', 'used_mb': None, 'start_time': '111'},
        {'pid': 301, 'name': 'python', 'used_mb': None, 'start_time': '222'},
    ])
    async def execute(*args, **kwargs):
        class Process:
            returncode = 0
            async def communicate(self):
                text = '0, Test GPU, 1000, 2000, 1000, 0, GPU-test\n'
                if 'compute-apps' in ' '.join(args):
                    text = '300, GPU-test, llama-server, [N/A]\n999, GPU-test, Windows.exe, 100\n'
                return text.encode(), b''
        return Process()
    monkeypatch.setattr(cookbook.asyncio, 'create_subprocess_exec', execute)
    handler = next(r.endpoint for r in cookbook.setup_cookbook_routes().routes if r.path == '/api/cookbook/gpus')
    result = await handler(Request({'type': 'http'}), host=None, ssh_port=None)
    assert [p['pid'] for p in result['gpus'][0]['processes']] == [300]
    assert [p['pid'] for p in result['unassigned_processes']] == [301]


def test_gpu_cleanup_refuses_reused_pids_and_app_pid(tmp_path, monkeypatch):
    import src.cookbook_gpu as gpu
    monkeypatch.setattr(cookbook, 'require_admin', lambda request: None)
    monkeypatch.setattr(cookbook, 'COOKBOOK_STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(cookbook, 'IS_WINDOWS', False)
    monkeypatch.setattr(gpu, 'process_start_time', lambda pid: 'new-process')
    app = FastAPI(); app.include_router(cookbook.setup_cookbook_routes())
    client = TestClient(app)
    result = client.post('/api/cookbook/kill-pid', json={'pid': 12345, 'start_time': 'old-process', 'gpu_only': True})
    assert result.json()['ok'] is False
    assert client.post('/api/cookbook/kill-pid', json={'pid': os.getpid()}).status_code == 400


WARNING = '0.00.253.161 W common_fit_params: failed to fit params to free device memory: llama_params_fit is not implemented for SPLIT_MODE_TENSOR, abort\n'
ROUNDING = '1.26.492.169 W llama_context: n_ctx is not divisible by n_seq_max - rounding down to 20480\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('ready', [False, True])
async def test_native_warning_does_not_stop_live_model_status(tmp_path, monkeypatch, ready):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'tasks': [{'sessionId': 'serve-native-test', 'type': 'serve', 'status': 'error', 'output': WARNING}]}))
    monkeypatch.setattr(cookbook, 'COOKBOOK_STATE_FILE', str(state))
    monkeypatch.setattr(cookbook, 'require_admin', lambda request: None)
    monkeypatch.setattr(cookbook, 'IS_WINDOWS', False)
    snapshot = WARNING + ROUNDING + 'load_model: loading model test.gguf\n'
    if ready: snapshot += '1.31.085.520 I srv llama_server: listening on http://0.0.0.0:8001\n'
    def run(args, **kwargs):
        if 'has-session' in args: return SimpleNamespace(returncode=0, stdout='', stderr='')
        if 'capture-pane' in args: return SimpleNamespace(returncode=0, stdout=snapshot, stderr='')
        return SimpleNamespace(returncode=1, stdout='', stderr='')
    monkeypatch.setattr(cookbook.subprocess, 'run', run)
    handler = next(r.endpoint for r in cookbook.setup_cookbook_routes().routes if r.path == '/api/cookbook/tasks/status')
    result = await handler(Request({'type': 'http'}))
    task = result['tasks'][0]
    assert task['status'] == ('ready' if ready else 'running')
    assert task['diagnosis'] is None
    assert _diagnose_serve_output(snapshot) is None
    assert _parse_serve_phase(snapshot)['status'] == task['status']
