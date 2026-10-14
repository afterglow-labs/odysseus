"""Package discovery is explicit; media fields retain distinct owned bindings."""
import asyncio
import io
import json

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import FormData, UploadFile

from routes import comfy_node_package_routes as packages
from routes.workflow_graph_routes import json_body, stage_bindings
from src.comfy_node_manifest import convert_object_info


def test_package_discovery_requires_admin_and_an_available_candidate(tmp_path, monkeypatch):
    calls = []
    candidate = {'id': 'local-id', 'name': 'Example', 'path': tmp_path / 'private-package'}
    manifest = convert_object_info({'Example': {'input': {'required': {}}, 'output': [], 'output_node': True}})
    manifest.update(package_id='example-id', execution_status='metadata_only')
    manifest['source'].update(id='example-id', kind='python_package', package_path=str(candidate['path']), files=['private'])
    def discover(path, **kwargs):
        calls.append(path)
        return manifest
    monkeypatch.setattr(packages, 'candidates', lambda: {'local-id': candidate})
    monkeypatch.setattr(packages, 'model_paths', lambda: {})
    monkeypatch.setattr(packages, 'discover_node_package', discover)
    def owner(request):
        if request.headers.get('x-admin') != 'yes':
            raise HTTPException(403, 'Administrator required')
    app, router = FastAPI(), APIRouter()
    packages.add_comfy_node_package_routes(router, owner, json_body, directory=tmp_path / 'manifests')
    app.include_router(router)
    client = TestClient(app)
    assert client.post('/packages/discover', json={'id':'local-id'}).status_code == 403
    assert not calls
    headers = {'x-admin':'yes'}
    assert client.post('/packages/discover', headers=headers, content='{broken').status_code == 400
    assert client.post('/packages/discover', headers=headers, json={'path':'/tmp/other.py'}).status_code == 400
    assert client.post('/packages/discover', headers=headers, json={'id':'unknown'}).status_code == 404
    result = client.post('/packages/discover', headers=headers, json={'id':'local-id'})
    assert result.status_code == 200, result.text
    assert calls == [candidate['path']]
    assert 'package_path' not in result.json()['source'] and 'files' not in result.json()['source']
    assert json.loads((tmp_path / 'manifests/example-id.json').read_text())['source']['package_path'] == str(candidate['path'])
    assert client.post('/packages/object-info', headers=headers, content='[]').status_code == 400
    imported = client.post('/packages/object-info', headers=headers, json={'object_info':{'RemoteOnly': {'input':{},'output':['IMAGE']}}})
    assert imported.status_code == 200, imported.text
    assert imported.json()['execution_status'] == 'unbound'


def test_dynamic_media_fields_are_independent_and_cannot_target_arbitrary_parameters(tmp_path, monkeypatch):
    from src import workflow_graph
    monkeypatch.setattr(workflow_graph, 'node_registry', lambda: {'comfy.TwoImages': {
        'execution':'comfy', 'params':[{'id':key,'editor':'media','media_kind':'image'} for key in ('first','last')]}})
    document = {'nodes':[{'id':'loader','type':'comfy.TwoImages'}]}
    form = FormData({'a':UploadFile(io.BytesIO(b'first'),filename='first.png'),
                     'b':UploadFile(io.BytesIO(b'last'),filename='last.png')})
    bindings = {'loader::first':[{'upload':'a'}],'loader::last':[{'upload':'b'}]}
    async def check():
        async with stage_bindings(tmp_path, document, bindings, form, 1000) as media:
            from pathlib import Path
            assert Path(media['loader::first'][0]['path']).read_bytes() == b'first'
            assert Path(media['loader::last'][0]['path']).read_bytes() == b'last'
            assert media['loader::first'][0]['path'] != media['loader::last'][0]['path']
        for invalid in ({'loader::undeclared':[{'upload':'a'}]}, {'loader::first':[{'upload':'a'},{'upload':'b'}]}):
            with pytest.raises(ValueError):
                async with stage_bindings(tmp_path, document, invalid, form, 1000):
                    pytest.fail('Invalid binding was accepted')
        assert not list((tmp_path / '.uploads').iterdir())
    asyncio.run(check())
