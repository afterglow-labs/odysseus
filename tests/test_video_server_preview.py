"""Authenticated, seekable previews use the same validated handles as uploads."""
import asyncio
import json

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from routes import video_server_file_routes as routes


@pytest.fixture
def preview(tmp_path, monkeypatch):
    root = tmp_path / 'media'
    root.mkdir()
    video = root / 'scene with spaces.mp4'
    video.write_bytes(bytes(range(256)) * 8192)
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'f': str(root)}))
    router = APIRouter(prefix='/api/video/h3')
    def owner(request):
        if request.headers.get('x-user') != 'owner':
            raise HTTPException(403, 'Authentication required')
        return 'owner'
    routes.add_server_video_file_routes(router, owner)
    app = FastAPI()
    app.include_router(router)
    info = video.stat()
    selection = {'root': 'f', 'path': video.name, 'size': info.st_size, 'mtime_ns': str(info.st_mtime_ns)}
    with TestClient(app) as client:
        yield client, video, selection


def get(preview, headers=None, method='GET', **selection):
    client, _, descriptor = preview
    return client.request(method, '/api/video/h3/server-files/video', params={**descriptor, **selection},
                          headers={'x-user': 'owner', **(headers or {})})


def test_preview_auth_before_open(preview, monkeypatch):
    def forbidden(_):
        pytest.fail('Anonymous request opened a file')
    monkeypatch.setattr(routes, 'open_server_video_preview', forbidden)
    assert get(preview, {'x-user': 'stranger'}).status_code == 403


@pytest.mark.parametrize('range_header,start,end', [
    ('bytes=0-1', 0, 1), ('bytes=103-600', 103, 600),
    ('bytes=-16', 2097136, 2097151), ('bytes=2097140-', 2097140, 2097151),
    ('bytes=2097140-9999999', 2097140, 2097151),
])
def test_browser_seek_returns_exact_requested_bytes(preview, range_header, start, end):
    response = get(preview, {'Range': range_header})
    assert response.status_code == 206
    assert response.content == preview[1].read_bytes()[start:end + 1]
    assert response.headers['content-range'] == f'bytes {start}-{end}/2097152'
    assert response.headers['content-length'] == str(end - start + 1)
    assert response.headers['accept-ranges'] == 'bytes'
    assert response.headers['content-type'] == 'video/mp4'
    assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('header', ['bytes=2097152-', 'bytes=9-3', 'bytes=-0', 'bytes=-',
                                    'bytes=1-2,3-4', 'bytes=' + '9' * 100 + '-', 'invalid'])
def test_invalid_range_does_not_stream_entire_file(preview, header):
    response = get(preview, {'Range': header})
    assert response.status_code == 416
    assert response.headers['content-range'] == 'bytes */2097152'


def test_full_get_and_head_close_validated_handle(preview, monkeypatch):
    handles = []
    original = routes.open_server_video_preview
    def record(selection):
        upload = original(selection)
        handles.append(upload.file)
        return upload
    monkeypatch.setattr(routes, 'open_server_video_preview', record)
    assert get(preview).content == preview[1].read_bytes()
    head = get(preview, method='HEAD')
    assert head.status_code == 200 and not head.content
    assert head.headers['content-length'] == str(preview[1].stat().st_size)
    assert get(preview, {'Range': 'bytes=9-3'}).status_code == 416
    assert len(handles) == 3 and all(handle.closed for handle in handles)


def test_preview_uses_current_file_but_rejects_escaped_path(preview):
    assert get(preview, path='../outside.mp4').status_code == 400
    assert get(preview, path='password.txt').status_code == 400
    response = get(preview, size=1, mtime_ns='1')
    assert response.status_code == 200 and response.content == preview[1].read_bytes()
    video = preview[1]
    target = video.with_name('other.mp4')
    target.write_bytes(video.read_bytes())
    video.unlink()
    video.symlink_to(target)
    assert get(preview).status_code == 400


def test_open_handle_cannot_be_redirected_after_validation(preview, monkeypatch):
    original = routes.open_server_video_preview
    expected = preview[1].read_bytes()[:32]
    def replace_path(selection):
        upload = original(selection)
        root = preview[1].parent
        root.rename(root.with_name('previous-directory'))
        root.mkdir()
        preview[1].write_bytes(b'unrelated private data')
        return upload
    monkeypatch.setattr(routes, 'open_server_video_preview', replace_path)
    response = get(preview, {'Range': 'bytes=0-31'})
    assert response.status_code == 206 and response.content == expected


def test_preview_rechecks_after_open_before_headers_and_closes(preview, monkeypatch):
    handles = []
    original = routes.open_server_video_preview
    def mutate_after_open(selection):
        upload = original(selection)
        handles.append(upload.file)
        preview[1].write_bytes(b'changed after its descriptor was checked')
        return upload
    monkeypatch.setattr(routes, 'open_server_video_preview', mutate_after_open)
    response = get(preview)
    assert response.status_code == 409
    assert handles[0].closed


def test_cancelled_stream_closes_validated_file(preview):
    upload = routes.open_server_video_preview(preview[2])
    async def run():
        started = asyncio.Event()
        response = routes._VideoPreviewResponse(upload, start=100, end=500, status_code=206)
        async def send(message):
            if message['type'] == 'http.response.body':
                started.set()
                await asyncio.Event().wait()
        async def receive():
            await asyncio.Event().wait()
        task = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upload.file.closed
    asyncio.run(run())
