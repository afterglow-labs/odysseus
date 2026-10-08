"""Route ownership, disk failures, and disconnected streamed downloads."""
import asyncio
import io
import json
from pathlib import Path
import time
import threading
from types import SimpleNamespace
import zipfile

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.requests import ClientDisconnect

from src.h3_video import H3JobManager
from routes import video_workflow_routes as routes


@pytest.fixture
def api(tmp_path):
    manager = H3JobManager(tmp_path/'jobs', project=tmp_path/'private-project')
    manager.inventory = lambda: {'components': [], 'gpus': [], 'runtime_ready': False, 'defaults': {}}
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda name: name in {'owner', 'other'})
    @app.middleware('http')
    async def identity(request, call_next):
        request.state.current_user = request.headers.get('x-user', 'owner')
        return await call_next(request)
    router = APIRouter(prefix='/api/video/h3')
    routes.add_workflow_routes(router, manager, family='h3')
    app.include_router(router)
    return TestClient(app), tmp_path


def document():
    return {'format': 'odysseus-video-workflow', 'version': 1, 'family': 'h3',
            'config': {'mode': 't2va', 'prompt': 'A portable scene.'}, 'components': {}, 'attachments': []}


def test_auth_before_transfer_staging_and_ticket_owner_check(api):
    client, root = api
    assert client.post('/api/video/h3/workflow/import', content=b'{}', headers={'x-user': 'reader'}).status_code == 403
    assert not (root/'workflow_transfers').exists()
    response = client.post('/api/video/h3/workflow/export', data={'config': json.dumps({'mode':'t2va','prompt':'scene','gpu':'private-machine-id'}), 'options':'{}'})
    assert response.status_code == 200, response.text
    url = response.json()['download_url']
    assert client.get(url, headers={'x-user':'other'}).status_code == 404
    exported = client.get(url)
    assert exported.status_code == 200 and 'attachment' in exported.headers['content-disposition']
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.namelist() == ['workflow.json']
        recipe = json.loads(archive.read('workflow.json'))
        assert 'gpu' not in recipe['config'] and 'private-machine-id' not in json.dumps(recipe)
    imported = client.post('/api/video/h3/workflow/import', content=json.dumps(document()), headers={'content-type':'application/octet-stream'})
    assert imported.status_code == 200, imported.text
    assert imported.json()['workflow'] == {**document(), 'input_layout': 'source-guide-v1'}
    assert imported.json()['attachments'] == []
    assert not list(root.rglob('upload.zip'))


def test_failed_import_or_media_export_cleans_staging(api, monkeypatch):
    client, root = api
    bad = client.post('/api/video/h3/workflow/import', content=b'not-a-zip')
    assert bad.status_code == 400, bad.text
    assert not list((root/'workflow_transfers').rglob('receipt.json'))
    bad = client.post('/api/video/h3/workflow/export', data={'config':'{"mode":"fl2va"}', 'options':'{"include_attachments":true}'}, files={'first_frame':('execute.py',b'bad','text/plain')})
    assert bad.status_code == 400
    assert not list((root/'workflow_transfers').rglob('receipt.json'))
    monkeypatch.setattr(routes, 'free_space', lambda *_: (_ for _ in ()).throw(OSError('Not enough disk space')))
    bad = client.post('/api/video/h3/workflow/import', content=json.dumps(document()))
    assert bad.status_code == 507
    assert not list((root/'workflow_transfers').rglob('receipt.json'))


@pytest.mark.parametrize('disconnect_at', ['headers', 'body'])
def test_download_disconnect_closes_sources_even_before_first_body(disconnect_at):
    source = io.BytesIO(b'x' * (8 * 1024 * 1024))
    asset = {'archive_path':'weights/model/tensor.safetensors', 'size':len(source.getbuffer())}
    response = routes.WorkflowDownload(document(), [(asset, source)], 'test.zip')
    async def send(message):
        if message['type'] == ('http.response.start' if disconnect_at == 'headers' else 'http.response.body'):
            raise OSError('client disconnected')
    async def receive():
        await asyncio.sleep(60)
    async def run():
        try:
            await response({'type':'http','asgi':{'spec_version':'2.4'}}, receive, send)
        except (OSError, ClientDisconnect):
            pass
    asyncio.run(run())
    assert source.closed


def test_cancel_while_opening_download_closes_late_acquired_handles():
    started, release = threading.Event(), threading.Event()
    source = io.BytesIO(b'weights')
    def acquire():
        started.set()
        assert release.wait(3)
        return document(), [({}, source)]
    async def run():
        task = asyncio.create_task(routes._acquire_download(acquire))
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert source.closed
