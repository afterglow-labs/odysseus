"""File transfers must finish even when they exceed the generic API deadline."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse

from src.request_timeout import is_file_transfer
from tests.test_video_auto_refine_timeout import timeout_middleware


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    ('POST', '/api/video/h3/jobs'),
    ('POST', '/api/video/bfs/jobs'),
    ('PATCH', '/api/video/h3/jobs/saved-job'),
    ('PATCH', '/api/video/bfs/jobs/saved-job'),
    ('POST', '/api/video/h3/local-previews'),
    ('POST', '/api/video/bfs/workflow/export'),
    ('POST', '/api/workflow-graphs/runs'),
    ('POST', '/api/email/compose-upload'),
])
async def test_slow_multipart_upload_keeps_every_byte_and_submits_once(method, path):
    app = FastAPI()
    received = []

    @app.api_route(path, methods=[method])
    async def upload(request: Request):
        async with request.form() as form:
            received.append(await form['video'].read())
        # Input copying/probing after reception is part of the submission too.
        await asyncio.sleep(.02)
        return {'accepted': len(received[-1])}

    app.add_middleware(timeout_middleware(.005))
    payload = b'complete video contents' * 128
    boundary = b'file-boundary'
    body = (b'--' + boundary + b'\r\nContent-Disposition: form-data; name="video"; filename="clip.mkv"'
            b'\r\nContent-Type: video/x-matroska\r\n\r\n' + payload + b'\r\n--' + boundary + b'--\r\n')
    async def slow_body():
        for offset in range(0, len(body), 512):
            await asyncio.sleep(.01)
            yield body[offset:offset + 512]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.request(method, path, content=slow_body(),
                                        headers={'Content-Type': 'multipart/form-data; boundary=file-boundary'})
    assert response.status_code == 200, response.text
    assert response.json() == {'accepted': len(payload)}
    assert received == [payload], 'A slow upload must not be cancelled or retried halfway through'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/video/h3/workflow/import', '/api/video/bfs/workflow/import',
                                  '/api/video/h3/lora-library/upload'])
async def test_slow_raw_file_upload_is_not_cut_off(path):
    app = FastAPI()
    @app.post(path)
    async def upload(request: Request):
        return {'size': len(await request.body())}
    app.add_middleware(timeout_middleware(.005))
    async def chunks():
        yield b'first'
        await asyncio.sleep(.04)
        yield b'last'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.post(path, content=chunks(), headers={'Content-Type': 'application/octet-stream'})
    assert response.status_code == 200 and response.json() == {'size': 9}


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/video/h3/jobs', '/api/video/bfs/jobs',
                                  '/api/video/h3/workflow/export', '/api/video/bfs/workflow/export'])
async def test_copying_server_selected_files_and_preparing_export_has_no_45_second_cutoff(path):
    app = FastAPI()
    @app.post(path)
    async def copy(request: Request):
        form = await request.form()
        await asyncio.sleep(.04)
        return {'selected': form['server_files']}
    app.add_middleware(timeout_middleware(.005))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.post(path, data={'server_files': '["existing-server-video"]'})
    assert response.status_code == 200 and response.json()['selected'] == '["existing-server-video"]'


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    ('GET', '/api/video/h3/jobs/example/video'),
    ('GET', '/api/video/bfs/jobs/example/workflow'),
    ('GET', '/api/video/h3/workflow/exports/ticket'),
    ('GET', '/api/video/bfs/workflow/imports/ticket/attachments/0'),
    ('GET', '/api/video/h3/server-files/preview'),
    ('HEAD', '/api/video/h3/server-files/video'),
    ('GET', '/api/video/h3/local-previews/example/video'),
    ('GET', '/api/video/h3/jobs/example/input-videos/reference_videos/0/preview'),
    ('GET', '/api/video/h3/jobs/example/seed-hunter/candidates/0/video'),
    ('GET', '/api/video/h3/jobs/example/seed-hunter/candidates/0/audio'),
    ('GET', '/api/video/h3/jobs/example/scout/previews/123'),
    ('GET', '/api/workflow-graphs/runs/run/outputs/output'),
    ('GET', '/api/email/attachments-download/123'),
    ('POST', '/api/gallery/download-zip'),
    ('POST', '/api/documents/export-zip'),
    ('GET', '/api/document/example/export-pdf'),
])
async def test_slow_download_preparation_and_body_both_finish(method, path):
    app = FastAPI()
    calls = []
    @app.api_route(path, methods=[method])
    async def download():
        calls.append(path)
        # Archives/network files may take time even before sending headers.
        await asyncio.sleep(.04)
        async def body():
            for chunk in (b'begin', b'middle', b'end'):
                await asyncio.sleep(.01)
                yield chunk
        return StreamingResponse(body(), media_type='application/zip',
                                 headers={'Content-Disposition': 'attachment; filename="workflow.zip"'})
    app.add_middleware(timeout_middleware(.005))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.request(method, path)
    assert response.status_code == 200
    assert response.headers['content-disposition'] == 'attachment; filename="workflow.zip"'
    assert response.content == (b'' if method == 'HEAD' else b'beginmiddleend')
    assert calls == [path]


@pytest.mark.parametrize('content_type', ['Multipart/Form-Data; boundary=foo', 'application/octet-stream',
                                         'application/zip', 'image/png', 'audio/flac', 'video/x-matroska'])
@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH'])
def test_file_body_classification_does_not_depend_on_endpoint_name(method, content_type):
    assert is_file_transfer(method, '/api/some-file-recipient', content_type)


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/video/h3/inventory'), ('GET', '/api/video/h3/jobs'),
    ('GET', '/api/video/h3/jobs/example'), ('DELETE', '/api/video/h3/jobs/example'),
    ('POST', '/api/video/h3/jobs/example/cancel'), ('POST', '/api/video/h3/jobs-extra'),
    ('POST', '/api/video/h3/workflow/import/extra'), ('GET', '/api/video/h3/queue'),
    ('POST', '/api/video/h3/queue/edit'), ('GET', '/api/video/h3/server-files'),
    ('GET', '/api/video/h3/server-files/metadata'), ('GET', '/api/gallery/photos'),
    ('GET', '/api/video/h3/jobs/example/video-extra'), ('GET', '/api/email/attachments/example'),
])
def test_nontransfer_routes_are_not_misidentified(method, path):
    assert not is_file_transfer(method, path, 'application/json')


@pytest.mark.asyncio
async def test_upload_exemption_does_not_bypass_route_authentication():
    from fastapi import HTTPException
    app = FastAPI()
    @app.post('/api/video/h3/jobs')
    async def guarded_upload(request: Request):
        raise HTTPException(403, 'Sign in')
    app.add_middleware(timeout_middleware(.005))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.post('/api/video/h3/jobs', content=b'private',
                                     headers={'Content-Type': 'application/octet-stream'})
    assert response.status_code == 403
