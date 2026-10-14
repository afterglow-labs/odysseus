"""HTTP graph submissions preserve ownership, revision and one-run retries."""
import json
import uuid

import pytest

from routes.workflow_graph_routes import setup_workflow_graph_routes
from src.workflow_graph import new_document
from src.workflow_graph_import import from_native_config
from src.workflow_graph_store import GraphDocumentStore
from src.workflow_graph_runs import GraphRunManager
from tests.test_video_submission import backend


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
