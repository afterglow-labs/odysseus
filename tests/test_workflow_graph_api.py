"""HTTP graph submissions preserve ownership, revision and one-run retries."""
import json
from pathlib import Path
import uuid

import pytest

from routes.workflow_graph_routes import setup_workflow_graph_routes
from src.workflow_graph import new_document
from src.workflow_graph_import import from_native_config
from src.workflow_graph_store import GraphDocumentStore
from src.workflow_graph_runs import GraphRunManager
from tests.test_video_submission import backend, image
from tests.test_workflow_graph_runs import wait_for, step


@pytest.fixture
def graph_api(backend, tmp_path):
    store = GraphDocumentStore(tmp_path / 'documents')
    runs = GraphRunManager(tmp_path / 'runs', backend.managers, poll_interval=.03)
    client = backend.client()
    client.app.include_router(setup_workflow_graph_routes(store, runs, backend.managers))
    yield client, runs, backend
    runs.close()


def test_saved_graph_ownership_revision_and_catalog(graph_api):
    client, _, backend = graph_api
    prefix = '/api/workflow-graphs'
    catalog = client.get(prefix+'/catalog')
    assert catalog.status_code == 200, catalog.text
    assert all('document' in row for row in catalog.json()['templates'])
    assert client.get(prefix+'/catalog', headers={'x-user':'reader'}).status_code == 403
    doc = new_document(template='media')
    saved = client.post(prefix+'/documents', json={'document':doc}).json()
    assert client.get(prefix+'/documents/'+saved['id']).json()['document'] == doc
    assert client.get(prefix+'/documents/'+saved['id'], headers={'x-user':'other'}).status_code == 404
    assert client.post(prefix+'/documents', json={**saved,'document':doc}).status_code == 400  # unknown metadata is not a write contract
    assert client.post(prefix+'/documents', json={'id':saved['id'],'revision':0,'document':doc}).status_code == 409
    response = client.post(prefix+'/from-draft', json={'config':backend.configs['h3'],'family':'h3','input_counts':{'reference_videos':2}})
    assert response.status_code == 200, response.text
    graph = response.json()['document']
    assert len([n for n in graph['nodes'] if n['type']=='input.video']) == 2


def test_submission_retries_retain_one_snapshot_and_reject_untrusted_media(graph_api):
    client, runs, backend = graph_api
    prefix = '/api/workflow-graphs'
    doc = from_native_config(backend.configs['h3'], input_counts={'reference_videos':1})
    key = str(uuid.uuid4())
    body = {'document':json.dumps(doc),'bindings':json.dumps({'reference_videos__0':[{'upload':'clip'}]}),'request_id':key}
    response = client.post(prefix+'/runs', data=body, files={'clip':('source.mp4', b'fixture', 'video/mp4')})
    assert response.status_code == 201, response.text
    run = response.json()
    retry = client.post(prefix+'/runs', headers={'X-Odysseus-Submission-Id':key}, content=b'ignored retry body')
    assert retry.status_code == 201 and retry.json()['id'] == run['id']
    assert client.get(prefix+'/runs/'+run['id'],headers={'x-user':'other'}).status_code == 404
    assert client.post(prefix+'/runs/'+run['id']+'/cancel').status_code == 200
    unsafe = client.post(prefix+'/runs',json={'document':doc,'request_id':str(uuid.uuid4()),'bindings':{'reference_videos__0':[{'path':'/etc/passwd'}]}})
    assert unsafe.status_code == 400
    assert len(list(runs.root.glob('*/manifest.json'))) == 1


def test_invalid_graph_does_not_stage_or_start_jobs(graph_api):
    client, runs, backend = graph_api
    doc = new_document(template='blank')
    response = client.post('/api/workflow-graphs/runs',json={'document':doc,'request_id':str(uuid.uuid4())})
    assert response.status_code == 422
    assert not backend.calls
    assert not list(runs.root.glob('*/manifest.json'))


@pytest.mark.parametrize('scout', [False, True])
def test_reference_bundle_keeps_uploads_and_both_server_drives_in_one_job(
        graph_api, tmp_path, monkeypatch, scout):
    client, runs, backend = graph_api
    roots, server_bindings = {}, {}
    for drive in ('e', 'f'):
        root = tmp_path / drive
        root.mkdir()
        path = root / f'{drive}-reference.mp4'
        path.write_bytes(f'{drive} reference video'.encode())
        roots[drive] = str(root)
        info = path.stat()
        server_bindings[f'reference_videos__{len(server_bindings)}'] = [{'server': {
            'root': drive, 'path': path.name, 'size': info.st_size,
            'mtime_ns': str(info.st_mtime_ns),
        }}]
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps(roots))
    config = dict(backend.configs['h3'])
    if scout:
        backend.inventories['h3']['seed_hunter'] = {'features': {'seed_scout': True}}
        config.update(recipe='seed_hunter', seed_hunter={'stage': 'scout'})
    document = from_native_config(config, input_counts={
        'reference_videos': 2, 'reference_images': 1, 'reference_audio': 1,
    })
    bindings = {**server_bindings, 'reference_images__0': [{'upload': 'photo'}],
                'reference_audio__0': [{'upload': 'sound'}]}
    response = client.post('/api/workflow-graphs/runs', data={
        'document': json.dumps(document), 'bindings': json.dumps(bindings),
        'request_id': str(uuid.uuid4()),
    }, files={'photo': ('reference.png', image(), 'image/png'),
              'sound': ('reference.wav', b'audio reference', 'audio/wav')})
    assert response.status_code == 201, response.text
    run = wait_for(runs, response.json()['id'],
                   lambda value: step(value, 'generate').get('submission_confirmed'))
    assert len(run['steps']) == 1
    assert len(backend.calls) == 1
    directory = backend.managers['h3'].directory(step(run, 'generate')['job_id'])
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['input_names']['reference_videos'] == ['e-reference.mp4', 'f-reference.mp4']
    assert [Path(path).read_bytes() for path in manifest['reference_videos']] == [
        b'e reference video', b'f reference video',
    ]
    assert Path(manifest['reference_images'][0]).read_bytes() == image()
    assert Path(manifest['reference_audio'][0]).read_bytes() == b'audio reference'
    if scout:
        assert manifest['config']['seed_hunter']['stage'] == 'scout'


def test_validation_reports_incompatible_references_before_upload(graph_api):
    client, runs, backend = graph_api
    config = dict(backend.configs['h3'])
    doc = from_native_config(config, input_counts={'first_frame': 1})
    result = client.post('/api/workflow-graphs/validate', json={'document': doc})
    assert result.status_code == 200, result.text
    data = result.json()
    assert not data['valid']
    assert any(issue['node_id'] == 'generate' and issue['code'] == 'generation_settings'
               for issue in data['issues'])
    assert not backend.calls
    assert not list(runs.root.glob('*/manifest.json'))
    doc = from_native_config(config, input_counts={'reference_videos': 1})
    result = client.post('/api/workflow-graphs/validate', json={'document': doc})
    assert result.json()['valid'], result.text


def test_unused_bindings_cannot_block_connected_reference_submission(graph_api):
    from src.workflow_graph import make_node
    client, runs, backend = graph_api
    doc = from_native_config(backend.configs['h3'], input_counts={'reference_videos': 1})
    doc['nodes'].append(make_node('unused', 'input.image'))
    response = client.post('/api/workflow-graphs/runs', data={
        'document': json.dumps(doc), 'request_id': str(uuid.uuid4()),
        'bindings': json.dumps({'reference_videos__0': [{'upload': 'clip'}],
                               'unused': [{'upload': 'missing'}]}),
    }, files={'clip': ('source.mp4', b'connected video', 'video/mp4')})
    assert response.status_code == 201, response.text
    run = wait_for(runs, response.json()['id'],
                   lambda value: step(value, 'generate').get('submission_confirmed'))
    assert len(backend.calls) == 1
    manifest = json.loads((runs.root / run['id'] / 'manifest.json').read_text())
    assert set(manifest['media']) == {'reference_videos__0'}


def test_seed_scout_frame_guides_can_be_configured_before_submission(graph_api):
    client, _, backend = graph_api
    backend.inventories['h3']['seed_hunter'] = {'features': {'seed_scout': True}}
    doc = from_native_config({**backend.configs['h3'], 'recipe': 'seed_hunter',
                             'seed_hunter': {'stage': 'scout', 'first_frame_guide': True}},
                            input_counts={'first_frame': 1})
    result = client.post('/api/workflow-graphs/validate', json={'document': doc})
    assert result.status_code == 200, result.text
    assert result.json()['valid'], result.text
    assert result.json()['plan'][0]['config']['seed_hunter']['first_frame_guide'] is True


def test_media_workflow_rejects_multiple_files_in_single_video_input(graph_api):
    client, runs, backend = graph_api
    response = client.post('/api/workflow-graphs/runs', data={
        'document': json.dumps(new_document(template='media')), 'request_id': str(uuid.uuid4()),
        'bindings': json.dumps({'video': [{'upload': 'first'}, {'upload': 'second'}]}),
    }, files={'first': ('first.mp4', b'first video', 'video/mp4'),
              'second': ('second.mp4', b'second video', 'video/mp4')})
    assert response.status_code == 400, response.text
    assert 'exactly one video' in response.text
    assert not backend.calls
    assert not list(runs.root.glob('*/manifest.json'))


def test_catalog_and_import_preconfigure_installed_components_without_changing_settings(graph_api):
    client, _, backend = graph_api
    templates = client.get('/api/workflow-graphs/catalog').json()['templates']
    scout = next(row for row in templates if row['id'] == 'seed_scout')
    nodes = {node['id']: node for node in scout['document']['nodes']}
    for role in ('model', 'encoder', 'video_vae', 'audio_vae'):
        assert nodes[role]['params']['component'] == backend.configs['h3'][role]
    assert nodes['generate']['params']['gpu'] == backend.configs['h3']['gpu']
    assert scout['setup_notes'] == scout['document']['metadata']['setup_notes']
    document = from_native_config({**backend.configs['h3'], 'steps': 8, 'seed': 7654})
    next(node for node in document['nodes'] if node['id'] == 'encoder')['params'] = {
        'component': '', 'name': 'missing-qwen3vl-minimax-h3-encoder.safetensors',
    }
    response = client.post('/api/workflow-graphs/import', json={'workflow': document})
    assert response.status_code == 200, response.text
    imported = response.json()
    updated = {node['id']: node for node in imported['document']['nodes']}
    assert updated['encoder']['params']['component'] == backend.configs['h3']['encoder']
    assert updated['generate']['params']['steps'] == 8
    assert updated['generate']['params']['seed'] == 7654
    assert updated['prompt']['params']['text'] == backend.configs['h3']['prompt']
    assert imported['setup_notes']
    assert any(note.get('node_id') == 'encoder' for note in imported['setup_notes'])
    assert document['nodes'] != imported['document']['nodes']
