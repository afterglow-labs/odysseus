"""Local MKV previews use private temporary copies, never a render job."""
import asyncio
import hashlib
import io
import subprocess
import time
from types import SimpleNamespace

import av
import imageio_ffmpeg
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.video_local_preview_routes import add_local_video_preview_routes
from src import local_video_previews as local
from src import video_preview_proxy as proxy
from src import video_submission
from src.h3_video import _read, _write


@pytest.fixture(scope='module')
def mkv(tmp_path_factory):
    path = tmp_path_factory.mktemp('neutral-video') / 'neutral.mkv'
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error',
                    '-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=24:duration=6',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6',
                    '-f', 'lavfi', '-i', 'sine=frequency=660:duration=6',
                    '-map', '0:v', '-map', '1:a', '-map', '2:a',
                    '-c:v', 'libx264', '-threads', '1', '-c:a', 'libvorbis', '-y', str(path)],
                   check=True, timeout=20)
    return path.read_bytes()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, 'CACHE_ROOT', tmp_path / 'cache')
    monkeypatch.delenv('ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES', raising=False)
    root = tmp_path / 'sessions'
    def owner(request):
        value = request.headers.get('x-user')
        if not value:
            raise HTTPException(401, 'Sign in')
        return value
    router = APIRouter(prefix='/api/video/h3')
    add_local_video_preview_routes(router, owner, root)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, root=root, store=local.LocalVideoPreviewStore(root))


def upload(api, content, user='corey'):
    return api.client.post('/api/video/h3/local-previews', headers={'x-user': user},
                           files={'video': ('neutral.mkv', content, 'video/x-matroska')})


def test_local_mkv_vorbis_window_is_nonblack_h264_aac_without_modifying_source(api, mkv, monkeypatch):
    monkeypatch.setenv('ODYSSEUS_CHAT_UPLOAD_MAX_BYTES', '16')
    digest = hashlib.sha256(mkv).hexdigest()
    reply = upload(api, mkv)
    assert reply.status_code == 201, reply.text
    data = reply.json()
    assert set(data) == {'id', 'metadata', 'expires_at', 'preview_url'}
    assert local.IDENTITY.fullmatch(data['id'])
    assert data['metadata']['video_codec'] == 'h264'
    assert data['metadata']['audio_codec'] == 'vorbis'
    assert data['metadata']['browser_compatible'] is False
    assert data['metadata']['duration_seconds'] == pytest.approx(6, abs=.1)
    assert time.time() + 7100 < data['expires_at'] < time.time() + 7300
    assert str(api.root) not in reply.text
    copied = api.root / data['id'] / 'source.mkv'
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == digest
    response = api.client.get(data['preview_url'], params={'start_seconds': 2, 'duration_seconds': 2},
                              headers={'x-user': 'corey'})
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'video/mp4'
    assert response.headers['x-preview-start-seconds'] == '2.0'
    with av.open(io.BytesIO(response.content)) as video:
        assert video.streams.video[0].codec_context.name == 'h264'
        assert len(video.streams.audio) == 1
        assert video.streams.audio[0].codec_context.name == 'aac'
        assert video.duration / av.time_base == pytest.approx(2, abs=.05)
        frames = list(video.decode(video=0))
        assert len(frames) == 48
        assert frames[0].to_ndarray(format='gray').std() > 10
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == digest
    assert not list(api.root.parent.rglob('state.json'))
    assert len(list(proxy.CACHE_ROOT.glob('local-' + data['id'] + '-*.mp4'))) == 1


def test_auth_ownership_phone_range_head_and_delete(api, mkv):
    assert api.client.post('/api/video/h3/local-previews', content=b'not multipart').status_code == 401
    data = upload(api, mkv).json()
    url = data['preview_url']
    for method in ('get', 'head'):
        assert getattr(api.client, method)(url).status_code == 401
        assert getattr(api.client, method)(url, headers={'x-user': 'other'}).status_code == 404
    response = api.client.get(url, params={'duration_seconds': 1}, headers={'x-user': 'corey', 'range': 'bytes=0-31'})
    assert response.status_code == 206 and len(response.content) == 32 and b'ftyp' in response.content
    assert response.headers['content-range'].startswith('bytes 0-31/')
    response = api.client.head(url, params={'duration_seconds': 1}, headers={'x-user': 'corey'})
    assert response.status_code == 200 and not response.content
    assert int(response.headers['content-length']) > 512
    delete_url = '/api/video/h3/local-previews/' + data['id']
    assert api.client.delete(delete_url, headers={'x-user': 'other'}).status_code == 404
    assert api.client.delete(delete_url, headers={'x-user': 'corey'}).json() == {'deleted': True}
    assert not (api.root / data['id']).exists()
    assert not list(proxy.CACHE_ROOT.glob('local-' + data['id'] + '-*.mp4'))
    assert api.client.get(url, headers={'x-user': 'corey'}).status_code == 404
    assert api.client.delete(delete_url, headers={'x-user': 'corey'}).status_code == 404


@pytest.mark.parametrize('params', [{'start_seconds': -1}, {'start_seconds': 'nan'}, {'duration_seconds': 31},
                                   {'duration_seconds': .1}, {'duration_seconds': 'inf'}])
def test_invalid_window_rejected_before_converter(api, mkv, monkeypatch, params):
    data = upload(api, mkv).json()
    monkeypatch.setattr(proxy, 'open_owned_video_proxy', lambda *a, **k: pytest.fail('Converter must not run'))
    assert api.client.get(data['preview_url'], params=params, headers={'x-user': 'corey'}).status_code == 400


def test_expiry_removes_private_source_and_cached_windows(api, mkv):
    data = upload(api, mkv).json()
    cached = proxy.CACHE_ROOT / ('local-' + data['id'] + '-' + 'a' * 64 + '.mp4')
    cached.parent.mkdir(exist_ok=True)
    cached.write_bytes(b'cache')
    path = api.root / data['id'] / 'manifest.json'
    manifest = _read(path)
    manifest['expires_at'] = time.time() - 1
    _write(path, manifest)
    assert api.client.get(data['preview_url'], headers={'x-user': 'corey'}).status_code == 410
    assert not path.parent.exists() and not cached.exists()


def test_cleanup_sweep_skips_active_upload_lease_and_preserves_other_files(api, mkv):
    data = upload(api, mkv).json()
    directory = api.root / data['id']
    manifest = _read(directory / 'manifest.json')
    manifest.update(status='uploading', expires_at=0)
    _write(directory / 'manifest.json', manifest)
    other = api.root / 'do-not-delete.txt'
    other.write_text('unrelated')
    with api.store._lease(data['id']):
        assert api.store.prune() == 0
        assert directory.exists()
    assert api.store.prune() == 1
    assert not directory.exists() and other.read_text() == 'unrelated'


def test_background_loop_sweeps_then_is_cancellable(api, mkv, monkeypatch):
    data = upload(api, mkv).json()
    path = api.root / data['id'] / 'manifest.json'
    manifest = _read(path)
    manifest['expires_at'] = 0
    _write(path, manifest)
    monkeypatch.setattr(local, 'LocalVideoPreviewStore', lambda: api.store)
    async def run():
        task = asyncio.create_task(local.local_preview_cleanup_loop())
        for _ in range(100):
            if not path.parent.exists():
                break
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert not path.parent.exists()


def test_source_symlink_rejected(api, mkv, tmp_path):
    data = upload(api, mkv).json()
    path = api.root / data['id'] / 'source.mkv'
    path.unlink()
    outside = tmp_path / 'unrelated.mkv'
    outside.write_bytes(mkv)
    path.symlink_to(outside)
    assert api.client.get(data['preview_url'], headers={'x-user': 'corey'}).status_code == 404
    assert outside.read_bytes() == mkv


def test_upload_cap_and_low_spool_space_rejected_cleanly(api, mkv, monkeypatch):
    monkeypatch.setenv('ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES', '1024')
    assert upload(api, mkv).status_code == 413
    assert not api.root.exists() or not list(api.root.glob('*/manifest.json'))
    monkeypatch.delenv('ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES')
    monkeypatch.setattr(video_submission.shutil, 'disk_usage', lambda p: SimpleNamespace(free=512 * 1024 * 1024))
    assert upload(api, mkv).status_code == 507
    assert not api.root.exists() or not list(api.root.glob('*/manifest.json'))


def test_copy_disk_failure_cleans_partial_source(api, mkv, monkeypatch):
    async def fail(stream, directory, chunk):
        stream.write(chunk[:20])
        raise video_submission.UploadSpaceError('Not enough disk space')
    monkeypatch.setattr(local, 'store_upload_chunk', fail)
    assert upload(api, mkv).status_code == 507
    assert not list(api.root.glob('*/source.*'))
    assert not list(api.root.glob('*/manifest.json'))


def test_bad_upload_and_probe_failure_leave_no_session(api):
    headers = {'x-user': 'corey'}
    for files in ({'video': ('wrong.txt', b'test')}, {'video': ('empty.mkv', b'')},
                  {'other': ('bad.mkv', b'test')}, [('video', ('a.mkv', b'a')), ('video', ('b.mkv', b'b'))]):
        reply = api.client.post('/api/video/h3/local-previews', files=files, headers=headers)
        assert reply.status_code == 400, reply.text
    assert upload(api, b'not a video').status_code == 422
    assert not list(api.root.glob('*/manifest.json'))


def test_delete_during_conversion_blocks_republish(api, mkv, monkeypatch):
    data = upload(api, mkv).json()
    original = proxy.subprocess.run
    monkeypatch.setattr(proxy, '_playback_ffmpeg', lambda: imageio_ffmpeg.get_ffmpeg_exe())
    def run(command, **kwargs):
        result = original(command, **kwargs)
        if '-c:v' in command:
            api.store.delete(data['id'], 'corey')
        return result
    monkeypatch.setattr(proxy.subprocess, 'run', run)
    result = api.client.get(data['preview_url'], params={'duration_seconds': 1}, headers={'x-user': 'corey'})
    assert result.status_code == 404
    assert not (api.root / data['id']).exists()
    assert not list(proxy.CACHE_ROOT.glob('local-' + data['id'] + '-*.mp4'))
    assert (proxy.CACHE_ROOT / ('local-' + data['id'] + '.deleted')).exists()


def test_renamed_playlist_is_rejected_without_opening_other_files(api, monkeypatch):
    from src import video_media_metadata
    monkeypatch.setattr(video_media_metadata, 'probe_open_video',
                        lambda *args: pytest.fail('A playlist must never reach the media probe'))
    assert upload(api, b"ffconcat version 1.0\nfile '/etc/passwd'\n").status_code == 422
    assert not list(api.root.glob('*/manifest.json'))
