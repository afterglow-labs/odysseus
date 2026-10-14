import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from scripts.h3_seed_scout import wait_for_selection
from src import h3_video as h3
from src import h3_seed_scout as scout
from tests.test_video_submission import backend, send


def test_selection_waits_for_explicit_candidate_and_never_defaults(tmp_path):
    phases, slept = [], []
    def sleep(_):
        slept.append(True)
        (tmp_path / 'scout-selection.json').write_text(json.dumps({'seed': 999 if len(slept) == 1 else 102}))
    result = wait_for_selection(tmp_path, [101, 102], 0, check_cancel=lambda: None, notify=phases.append, sleep=sleep)
    assert result == 102 and len(slept) == 2
    assert phases == ['waiting_for_selection']


def test_selection_timeout_and_cancel_never_choose_first(tmp_path):
    clock = iter([0, 2])
    with pytest.raises(TimeoutError, match='No seed was chosen'):
        wait_for_selection(tmp_path, [1, 2], 1, check_cancel=lambda: None, notify=lambda _: None, clock=lambda: next(clock))
    def cancel():
        raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        wait_for_selection(tmp_path, [1], 0, check_cancel=cancel, notify=lambda _: None)


def test_preview_is_owned_and_selection_is_explicit_atomic_idempotent(backend):
    result = send(backend, 'h3')
    assert result.status_code == 201, result.text
    job = result.json()['id']
    directory = backend.managers['h3'].directory(job)
    state = h3._read(directory / 'state.json')
    h3._write(directory / 'state.json', {**state, 'status': 'running'})
    h3._write(directory / 'scout.json', {'version': 1, 'mode': 'interactive', 'status': 'waiting_for_selection',
        'seeds': [10, 11], 'scout_step': 3, 'total_steps': 20,
        'previews': [{'seed': 10, 'filename': '../../private'}, {'seed': 11}]})
    (directory / 'scout-10.webp').write_bytes(b'preview')
    outside = directory.parent / 'private.webp'
    outside.write_bytes(b'private')
    (directory / 'scout-11.webp').symlink_to(outside)
    client = backend.client()
    url = f'/api/video/h3/jobs/{job}/scout'
    assert client.get(url).json()['status'] == 'waiting_for_selection'
    assert client.get(url + '/previews/10').content == b'preview'
    assert client.get(url + '/previews/11').status_code == 404
    assert client.get(url, headers={'x-user': 'other'}).status_code == 404
    assert not (directory / 'scout-selection.json').exists()
    assert client.post(url + '/select', json={'seed': 10}, headers={'x-user':'other'}).status_code == 404
    assert client.post(url + '/select', json={'seed': True}).status_code == 400
    assert client.post(url + '/select', json={'seed': 999}).status_code == 400
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: backend.client().post(url + '/select', json={'seed':10}), range(2)))
    assert [r.status_code for r in replies] == [200, 200]
    assert replies[0].json()['status'] == 'continuing'
    assert client.post(url + '/select', json={'seed':11}).status_code == 409
    assert h3._read(directory / 'scout-selection.json')['seed'] == 10
    assert len(backend.calls) == 1


def test_stopped_scout_cannot_be_continued(backend):
    job = send(backend, 'h3').json()['id']
    directory = backend.managers['h3'].directory(job)
    h3._write(directory / 'scout.json', {'status':'waiting_for_selection','seeds':[1]})
    state = h3._read(directory / 'state.json')
    h3._write(directory / 'state.json', {**state, 'status':'stopped'})
    client = backend.client()
    url = f'/api/video/h3/jobs/{job}/scout'
    assert client.get(url).json()['status'] == 'stopped'
    assert client.post(url + '/select', json={'seed':1}).status_code == 409


def test_installed_scout_splits_one_schedule_and_continues_exact_selected_partial(tmp_path):
    # Execute the installed upstream scouting algorithm with CPU sample/decode
    # stand-ins. This tests its schedule/latent selection, not GPU image quality.
    import ast
    import logging
    import time
    from pathlib import Path
    import torch
    source = Path(__file__).parents[1] / 'runtimes/minimax-h3/seed-scout/nodes.py'
    if not source.is_file():
        pytest.skip('Pinned SeedScout source is not installed')
    module = ast.parse(source.read_text())
    original = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name=='MiniMaxH3SeedScoutSampler')
    method = next(n for n in original.body if isinstance(n, ast.FunctionDef) and n.name=='_run_scout')
    preview = tmp_path / 'preview.webp'
    preview.write_bytes(b'webp')
    ns = {'torch':torch,'time':time,'LOG':logging.getLogger('scout-test'), '_PIL_OK':True,
          'EVT_PREVIEW':'preview','EVT_DONE':'done','_get_tae_decoder':lambda _:None,
          '_video_stream':lambda value:value,'_to_cpu':lambda value:value.cpu(),
          '_latent2rgb_images':lambda *args:torch.zeros((1,2,2,3)),
          '_vae_preview_images':lambda *args:torch.zeros((1,2,2,3)),
          '_downscale_images':lambda value,*args:value,'_encode_webp_b64':lambda *_:'d2VicA==',
          '_send':lambda *args:True,'_save_preview':lambda *_:({'filename':'preview.webp'},False,str(preview),'image/webp'),
          '_session_close':lambda *_:None,
          'comfy':SimpleNamespace(model_management=SimpleNamespace(throw_exception_if_processing_interrupted=lambda:None,
                       soft_empty_cache=lambda:None,InterruptProcessingException=InterruptedError))}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),ns)
    calls=[]
    class Sampler:
        _run_scout=ns['_run_scout']
        def _prepare_latent(self,*args): return {},torch.zeros(1)
        def _sample_seed(self,guider,sampler,sigmas,latent,samples,seed):
            calls.append(('scout',seed,sigmas.tolist()))
            return {'samples':torch.tensor([seed])},{'samples':torch.tensor([seed+100])},torch.tensor([seed+200])
        def _await_selection(self,*args): return 102,'explicit selection'
        def _sample_continue(self,guider,sampler,sigmas,latent,partial,seed):
            calls.append(('continue',seed,sigmas.tolist(),partial.tolist()))
            return {'samples':partial},{'samples':partial},None
    guider=SimpleNamespace(model_patcher=SimpleNamespace(model=SimpleNamespace(latent_format=None)))
    result=Sampler()._run_scout(guider,None,torch.tensor([10.,8.,6.,4.,2.,0.]),{},101,3,1,2,
             'vae',1,24,512,'preview',object(),True,0,'job')
    assert calls[:3]==[('scout',seed,[10.,8.,6.]) for seed in (101,102,103)]
    assert calls[3]==('continue',102,[6.,4.,2.,0.],[102])
    assert result['result'][2]==102
