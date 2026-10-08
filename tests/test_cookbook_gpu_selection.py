"""GPU picker indexes must select the same card when CUDA reverses ordinals."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from starlette.requests import Request

import routes.cookbook_routes as cookbook
from routes.cookbook_helpers import ServeRequest, _validate_gpus, _validate_serve_cmd
from src.cookbook_gpu import resolve_nvidia_selection, replace_cuda_visibility


ROOT = Path(__file__).resolve().parents[1]
BLACKWELL = 'GPU-a9435034-aa17-d151-1a9d-fb1060eb0609'
ADA = 'GPU-ae65ab63-8a09-72d8-0d20-c01ae72d00f1'
INVENTORY = f'0, NVIDIA GeForce RTX 5090, {BLACKWELL}\n1, NVIDIA GeForce RTX 4090, {ADA}\n'


@pytest.mark.parametrize('selection,expected', [
    ('1', ADA), ('0', BLACKWELL), ('1,0', f'{ADA},{BLACKWELL}'), (ADA, ADA),
])
def test_visible_devices_use_selected_card_uuid_and_preserve_order(selection, expected):
    assert resolve_nvidia_selection(selection, INVENTORY) == expected
    assert _validate_gpus(expected) == expected


@pytest.mark.parametrize('selection', ['2', 'GPU-00000000-0000-0000-0000-000000000000'])
def test_missing_selected_card_cannot_fall_back_to_another_gpu(selection):
    with pytest.raises(ValueError, match='not available'):
        resolve_nvidia_selection(selection, INVENTORY)


@pytest.mark.parametrize('value', ['1;echo bad', 'GPU-not-a-uuid', "1'", '1,'])
def test_gpu_uuid_support_preserves_shell_input_validation(value):
    with pytest.raises(HTTPException):
        _validate_gpus(value)


def test_inline_cuda_selection_cannot_override_wrapper_uuid():
    command = 'CUDA_VISIBLE_DEVICES=1 llama-server --model /cache/Qwen-Q4.gguf --main-gpu 0 --tensor-split 1'
    command = replace_cuda_visibility(command, ADA)
    assert command.startswith(f"CUDA_VISIBLE_DEVICES='{ADA}' llama-server")
    assert '--main-gpu 0 --tensor-split 1' in command
    assert _validate_serve_cmd(command) == command
    assert replace_cuda_visibility('$env:CUDA_VISIBLE_DEVICES = "1"; llama-server', ADA) == f"$env:CUDA_VISIBLE_DEVICES = '{ADA}'; llama-server"


@pytest.mark.parametrize('inventory', ['', '0, NVIDIA GeForce RTX 5090, [N/A]'])
def test_unverified_identity_never_turns_into_a_cuda_ordinal(inventory):
    with pytest.raises(ValueError, match='could not be verified'):
        resolve_nvidia_selection('1', inventory)


@pytest.mark.asyncio
@pytest.mark.parametrize('host,port', [(None, None), ('remote-gpu-box', '2222')])
async def test_route_maps_picker_on_actual_launch_host_before_any_model_process(tmp_path, monkeypatch, host, port):
    monkeypatch.setattr(cookbook, 'require_admin', lambda request: None)
    monkeypatch.setattr(cookbook, 'COOKBOOK_STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(cookbook, 'load_stored_hf_token', lambda **kwargs: None)
    monkeypatch.setattr(cookbook, 'IS_WINDOWS', False)
    # Stop at the tmux availability preflight, after selection normalization.
    monkeypatch.setattr(cookbook, '_binary_available', AsyncMock(return_value=False))
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(INVENTORY.encode(), b'')))
    execute = AsyncMock(return_value=process)
    monkeypatch.setattr(cookbook.asyncio, 'create_subprocess_exec', execute)
    monkeypatch.setattr(cookbook.asyncio, 'create_subprocess_shell', execute)
    handler = next(route.endpoint for route in cookbook.setup_cookbook_routes().routes if route.path == '/api/model/serve')
    req = ServeRequest(repo_id='Qwen/Model-GGUF', cmd='CUDA_VISIBLE_DEVICES=1 llama-server --model /cache/Qwen-Q4.gguf --main-gpu 0',
                       gpus='1', remote_host=host, ssh_port=port)
    result = await handler(Request({'type': 'http'}), req)
    assert result['ok'] is False  # no tmux or server process was launched
    assert req.gpus == ADA
    assert f"CUDA_VISIBLE_DEVICES='{ADA}'" in req.cmd
    assert BLACKWELL not in req.cmd
    assert '--main-gpu 0' in req.cmd
    assert execute.await_count == 1
    arguments = execute.call_args.args
    if host:
        assert host in arguments[0] and '-p 2222' in arguments[0]
    else:
        assert arguments[0] == 'nvidia-smi'


def test_frontend_preview_selects_4090_by_uuid_with_named_host_and_card():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for command builder regression')
    source = (ROOT / 'static/js/cookbook.js').read_text()
    env = source[source.index('function _gpuEnvVarName('):source.index('export function _buildEnvPrefix(')]
    build = source[source.index('export function _buildServeCmd('):source.index('\nexport ', source.index('export function _buildServeCmd(') + 1)]
    # The builder ends before the next export; isolate exactly its balanced body.
    build = build[:build.index('\n}\n') + 3].replace('export function', 'function', 1)
    script = f"""
import assert from 'node:assert/strict';
import {{gpuVisibility, gpuButtonLabel}} from {json.dumps((ROOT / 'static/js/cookbookGpuSelection.js').as_uri())};
const ada = {json.dumps(ADA)}, blackwell = {json.dumps(BLACKWELL)};
const target = {{host:'gpu-box',serverKey:'gpu-box:2222',serverName:'Workstation'}};
const probe = {{...target,backend:'cuda',byIdx:new Map([
  [0,{{index:0,name:'NVIDIA GeForce RTX 5090',uuid:blackwell}}],
  [1,{{index:1,name:'NVIDIA GeForce RTX 4090',uuid:ada}}],
])}};
assert.equal(gpuButtonLabel(probe.byIdx.get(1)), '1 · RTX 4090');
assert.equal(gpuVisibility('1',probe,target).ids, ada);
assert.equal(gpuVisibility('0',probe,target).ids, blackwell);
assert.equal(gpuVisibility('1',probe,{{...target,host:'other-box'}}), null);
assert.equal(gpuVisibility('1',probe,{{...target,serverKey:'gpu-box:22'}}), null);
const _envState={{remoteHost:'stale-other-box'}};
const _hwfitCache={{system:{{backend:'cuda'}},_scannedHost:'stale-other-box'}};
const _venvLooksWrongForPlatform=()=>false, _venvRootFromPath=p=>p, _isWindows=()=>false;
{env}
{build}
const fields={{host:target.host,gpus:'1',ngl:'99',ctx:'8192',port:'8001',_gguf_path:'/cache/Qwen-Q4.gguf',
  llama_main_gpu:'0',llama_tensor_split:'1',gpu_visibility:gpuVisibility('1',probe,target)}};
const command=_buildServeCmd(fields,'Qwen/Model-GGUF','llamacpp');
assert.ok(command.startsWith('CUDA_VISIBLE_DEVICES='+ada+' llama-server'), command);
assert.ok(command.includes('--main-gpu 0') && command.includes('--tensor-split 1'));
assert.ok(!command.includes(blackwell));
assert.ok(!command.includes('nvfp4'));
const cpu=_buildServeCmd({{...fields,llama_mode:'cpu'}},'Qwen/Model-GGUF','llamacpp');
assert.ok(!cpu.includes('CUDA_VISIBLE_DEVICES'));
"""
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
