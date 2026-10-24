"""Small tensor fixtures exercise data-only adapter installation and saved profiles."""
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from routes.h3_lora_library_routes import add_lora_library_routes
from scripts.h3_lora_controls import read_profile, validate_lora_profile, write_profile
from scripts.h3_video_worker import validate_loras
from src.h3_lora_library import LoraLibrary, download_source
from src import h3_video as h3
from src.video_queue_bulk import _patched
from tests.test_h3_video import weights, inventory, config


def profile(**changes):
    return {"display_name": "A custom adapter", "variant": "shared", "trigger_words": "style_token",
            "trigger_position": "prefix", "strength_control": {"kind": "custom", "min": -8,
            "max": 8, "step": .25, "default": 1}, "recommended_settings": {"steps": 4}, **changes}


def test_example_prompt_roundtrips_without_becoming_a_trigger_and_survives_older_edit(tmp_path):
    example = 'subject_definitions:\n<Subject 1> comes from <Picture 1>.\n\nsummary:\nA camera follows.'
    path = weights(tmp_path / 'minimax_h3_third_person.safetensors', True)
    saved = write_profile(path, profile(prompt_example=example))
    assert saved['prompt_example'] == example and saved['trigger_words'] == 'style_token'
    write_profile(path, profile(notes='Edited from an older client'), overwrite=True)
    assert read_profile(path)['prompt_example'] == example
    component = h3.discover_components([tmp_path])[0]
    assert component['lora_profile']['prompt_example'] == example
    write_profile(path, profile(prompt_example=''), overwrite=True)
    assert read_profile(path)['prompt_example'] == ''
    with pytest.raises(ValueError, match='Example prompt'):
        validate_lora_profile(profile(prompt_example='x' * 16001))


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(h3, "cache_roots", lambda project: [project / "models"])
    manager = h3.H3JobManager(root=tmp_path / "data/video_jobs/h3", project=tmp_path)
    return LoraLibrary(manager)


@pytest.fixture
def api(service):
    app = FastAPI()
    router = APIRouter(prefix='/api/video/h3')
    def owner(request):
        if not request.headers.get('x-owner'):
            raise HTTPException(403, 'Sign in')
        return request.headers['x-owner']
    library = add_lora_library_routes(router, service.manager, owner)
    app.include_router(router)
    return TestClient(app), library


def test_plain_filename_profile_inventory_and_immutable_filename(service):
    path = weights(service.manager.project / 'models' / 'motion.safetensors', True)
    assert service.components() == []
    saved = write_profile(path, profile())
    row, = service.components()
    assert row['name'] == 'motion.safetensors' and row['display_name'] == 'A custom adapter'
    assert row['lora_profile'] == saved and row['strength_control']['min'] == -8
    assert write_profile(path, profile(display_name='do not replace')) == saved
    row = service.profile(row['id'], profile(display_name='Changed'))
    assert row['display_name'] == 'Changed'
    assert read_profile(path)['display_name'] == 'Changed'


@pytest.mark.parametrize('suffix', ['', '_ffp'])
def test_vfx_profile_defaults_and_explicit_user_overrides(service, suffix):
    path = weights(service.manager.project / 'models' / f'minimax_h3_vfx_edit_v1.0_r128{suffix}.safetensors', True)
    row, = service.components()
    assert row['recipe'] == 'vfx_edit' and row['variant'] == 'ref2va'
    assert row['lora_profile']['trigger_words'] == 'vfx_edit:'
    assert row['lora_profile']['trigger_position'] == 'prefix'
    assert row['lora_profile']['recommended_settings'] == {}
    assert read_profile(path) is None, 'Discovery must not write or impersonate a saved profile'
    saved = write_profile(path, {'display_name': 'My VFX profile', 'variant': 'ref2va', 'trigger_words': ''})
    row, = service.components()
    assert row['lora_profile'] == saved
    assert row['lora_profile']['trigger_words'] == '', 'An intentionally empty saved field overrides the default'
    assert row['display_name'] == 'My VFX profile'


def test_saved_custom_bounds_survive_library_edits_and_worker(inventory):
    row = next(row for row in inventory['components'] if row['role'] == 'lora')
    write_profile(row['path'], profile(), overwrite=True)
    row['lora_profile'] = validate_lora_profile(profile())
    saved = h3.validate_config({**config(inventory), 'loras': [{'id': row['id'], 'strength': -8}]},
                              inventory['components'], inventory['gpus'], {})
    assert saved['prompt'] == config(inventory)['prompt'] and saved['steps'] == h3.DEFAULTS['steps']
    frozen = copy.deepcopy(saved['lora_profiles'])
    narrow = profile(strength_control={'kind':'custom', 'min':-2, 'max':2, 'step':.1, 'default':1})
    write_profile(row['path'], narrow, overwrite=True)
    row['lora_profile'] = validate_lora_profile(narrow)
    validate_loras(saved)
    changed = _patched(saved, {'steps': 4}, inventory, 'h3', {})
    assert changed['loras'][0]['strength'] == -8 and changed['lora_profiles'] == frozen
    with pytest.raises(ValueError, match='strength'):
        h3.validate_config({**config(inventory), 'loras':[{'id':row['id'], 'strength':-8}]}, inventory['components'], inventory['gpus'], {})
    with pytest.raises(ValueError, match='Unknown generation'):
        h3.validate_config({**config(inventory), 'lora_profiles': frozen}, inventory['components'], inventory['gpus'], {})


@pytest.mark.parametrize('update', [
    {'strength_control': {'kind':[], 'min':-8,'max':8,'step':1,'default':1}},
    {'strength_control': {'kind':'age', 'min':-8,'max':8,'step':1,'default':1}},
    {'strength_control': {'min':-1000,'max':1000,'step':1,'default':1}},
    {'strength_control': {'min':-8,'max':8,'step':float('nan'),'default':1}},
    {'strength_control': {'min':0,'max':10**500,'step':1,'default':1}},
    {'recommended_settings': {'shell':'echo broken'}},
    {'recommended_settings': {'width':257}},
    {'recommended_settings': {'frames':125}},
    {'display_name': 'x'*161}, {'trigger_words':'x'*4001}, {'notes':'x'*8001},
])
def test_invalid_profiles_rejected(update):
    with pytest.raises(ValueError):
        validate_lora_profile(profile(**update))


def test_model_label_does_not_rename_model(service):
    path = weights(service.manager.project/'models/coolthor/minimax_h3_ref2va.safetensors')
    (path.parent/'.odysseus-h3-labels.json').write_text(json.dumps({path.name:'coolthor · H3'}))
    component, = h3.discover_components([path.parent])
    assert component['display_name'] == 'coolthor · H3' and component['name'] == path.name


def test_upload_auth_integrity_and_profile_update(api, tmp_path):
    client, library = api
    body = weights(tmp_path/'source.safetensors', True).read_bytes()
    endpoint = '/api/video/h3/lora-library/upload?filename=motion.safetensors'
    assert client.post(endpoint, content=body, headers={'content-type':'application/octet-stream'}).status_code == 403
    assert not library.root.exists()
    headers = {'x-owner':'corey', 'content-type':'application/octet-stream'}
    result = client.post(endpoint, content=body, headers=headers)
    assert result.status_code == 200, result.text
    component = result.json()['component']
    assert Path(component['path']).read_bytes() == body
    assert component['name'] == 'motion.safetensors'
    saved = client.put('/api/video/h3/lora-library/'+component['id'], json=profile(), headers={'x-owner':'corey'})
    assert saved.status_code == 200 and saved.json()['component']['strength_control']['max'] == 8
    listing = client.get('/api/video/h3/lora-library', headers={'x-owner':'corey'})
    assert listing.json()['components'][0]['display_name'] == 'A custom adapter'
    assert listing.headers['cache-control'] == 'private, no-store'
    assert client.post(endpoint, content=b'not weights', headers=headers).status_code == 400
    assert not list(library.root.glob('.upload-*'))
    assert client.post(endpoint.replace('motion.safetensors','..%2Foutside.safetensors'), content=body, headers=headers).status_code == 400
    assert client.put('/api/video/h3/lora-library/'+component['id'], json=profile(), headers={}).status_code == 403


def test_upload_rejects_base_checkpoint_and_full_disk(api, tmp_path, monkeypatch):
    client, library = api
    headers = {'x-owner':'corey','content-type':'application/octet-stream'}
    endpoint = '/api/video/h3/lora-library/upload?filename=base.safetensors'
    body = weights(tmp_path/'base.safetensors').read_bytes()
    assert client.post(endpoint, content=body, headers=headers).status_code == 400
    monkeypatch.setattr('src.h3_lora_library.shutil.disk_usage', lambda path: SimpleNamespace(free=1))
    assert client.post(endpoint, content=body, headers=headers).status_code == 507
    assert not list(library.root.glob('.upload-*'))


@pytest.mark.parametrize('source', [
    {'url':'https://evil.test/a/b/blob/main/model.safetensors'},
    {'url':'http://huggingface.co/a/b/blob/main/model.safetensors'},
    {'url':'https://huggingface.co/a/b/blob/main/../model.safetensors'},
    {'repository':'a/b','filename':'/tmp/model.safetensors'},
    {'repository':'a/b','filename':'model.py'},
    {'repository':'a/b','filename':'model.safetensors','revision':'../bad'},
])
def test_hf_exact_source_validation(source):
    with pytest.raises(ValueError):
        download_source(source)


def mock_hf(monkeypatch, data, *, wait=None, checksum=None):
    monkeypatch.setattr('huggingface_hub.get_hf_file_metadata', lambda *a, **k: SimpleNamespace(
        size=len(data), commit_hash='a'*40, etag=checksum or hashlib.sha256(data).hexdigest()))
    monkeypatch.setattr('routes.cookbook_helpers.load_stored_hf_token', lambda **k: 'hf_testtoken')
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def raise_for_status(self): pass
        def iter_content(self, chunk_size):
            if wait:
                wait.wait(3)
            yield data[:len(data)//2]
            yield data[len(data)//2:]
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    monkeypatch.setattr('src.h3_lora_library.requests.get', get)
    return calls


def finished(service, identity):
    for _ in range(200):
        state = service.status(identity, 'corey')
        if state['status'] in {'completed','failed','cancelled'} and identity not in service.active:
            return state
        time.sleep(.01)
    pytest.fail('Download did not finish')


def test_hf_download_pins_hash_preserves_metadata_and_owner(service, monkeypatch, tmp_path):
    data = weights(tmp_path/'fixture.safetensors', True).read_bytes()
    calls = mock_hf(monkeypatch, data)
    receipt = service.start('corey', {'url':'https://huggingface.co/author/repo/blob/main/family/adapter.safetensors', 'profile':profile()})
    with pytest.raises(FileNotFoundError):
        service.status(receipt['id'], 'other')
    result = finished(service, receipt['id'])
    assert result['status'] == 'completed', result
    assert result['downloaded_bytes'] == result['total_bytes'] == len(data)
    assert result['repository'] == 'author/repo' and result['filename'] == 'family/adapter.safetensors'
    assert '/resolve/'+'a'*40+'/' in calls[0][0]
    assert calls[0][1]['headers']['Authorization'] == 'Bearer hf_testtoken'
    assert 'hf_testtoken' not in json.dumps(result)
    path = Path(result['component']['path'])
    assert read_profile(path)['trigger_words'] == 'style_token'
    assert result['component']['name'] == 'adapter.safetensors'


def test_hf_cancel_and_checksum_failure_do_not_install(service, monkeypatch, tmp_path):
    data = weights(tmp_path/'fixture.safetensors', True).read_bytes()
    gate = threading.Event()
    mock_hf(monkeypatch, data, wait=gate)
    receipt = service.start('corey', {'repository':'a/b', 'filename':'adapter.safetensors'})
    service.cancel(receipt['id'], 'corey')
    gate.set()
    assert finished(service, receipt['id'])['status'] == 'cancelled'
    assert not service.components()
    mock_hf(monkeypatch, data, checksum='b'*64)
    receipt = service.start('corey', {'repository':'a/b', 'filename':'adapter.safetensors'})
    result = finished(service, receipt['id'])
    assert result['status'] == 'failed' and 'checksum' in result['error']
    assert not service.components() and not list(service.root.glob('.upload-*'))


def test_hf_disk_preflight_before_network_body(service, monkeypatch, tmp_path):
    data = weights(tmp_path/'fixture.safetensors', True).read_bytes()
    calls = mock_hf(monkeypatch, data)
    monkeypatch.setattr('src.h3_lora_library.shutil.disk_usage', lambda path: SimpleNamespace(free=1))
    receipt = service.start('corey', {'repository':'a/b', 'filename':'adapter.safetensors'})
    assert finished(service, receipt['id'])['status'] == 'failed'
    assert calls == []


def test_frozen_components_are_path_free_and_keep_prior_editor_bounds(inventory):
    from src.h3_loras import frozen_components
    row = next(row for row in inventory['components'] if row['role'] == 'lora')
    row['lora_profile'] = validate_lora_profile(profile())
    saved = h3.validate_config({**config(inventory), 'loras':[{'id':row['id'], 'strength':8}]},
                              inventory['components'], inventory['gpus'], {})
    row['lora_profile'] = validate_lora_profile(profile(strength_control=None))
    frozen, = frozen_components(saved, inventory)
    assert 'path' not in frozen and frozen['id'] == row['id']
    assert frozen['strength_control']['max'] == 8
    from src.h3_video import _write
    from src.video_queue_bulk import _light
    folder = Path(row['path']).parent / 'saved-job'
    folder.mkdir()
    _write(folder/'manifest.json', {'config':saved})
    item = _light(folder, {'id':'a'*32}, inventory, 'h3')
    assert item['lora_components'] == [frozen]


def test_generic_job_freezes_bounds_before_new_profile_registration(inventory):
    row = next(row for row in inventory['components'] if row['role'] == 'lora')
    saved = h3.validate_config({**config(inventory), 'loras':[{'id':row['id'],'strength':-1}]},
                              inventory['components'], inventory['gpus'], {})
    changed = validate_lora_profile(profile(strength_control={'min':0,'max':8,'step':1,'default':1}))
    write_profile(row['path'], changed, overwrite=True)
    row['lora_profile'] = changed
    row['strength_control'] = changed['strength_control']
    updated = _patched(saved, {'steps':4}, inventory, 'h3', {})
    assert updated['loras'][0]['strength'] == -1
    validate_loras(updated)
    from src.h3_loras import frozen_components
    assert 'strength_control' not in frozen_components(updated, inventory)[0]


def test_interrupted_download_marks_failed_and_cleans_only_its_staging(service):
    service._prepare()
    path = service.stage('adapter.safetensors')
    path.write_bytes(b'partial')
    state = {'id':'a'*32, 'status':'downloading', 'owner':'corey', 'staging_name':path.parent.name}
    service._write(state)
    result = service.status(state['id'], 'corey')
    assert result['status'] == 'failed' and 'staging_name' not in result
    assert not path.parent.exists()
