"""Unsupported source containers get bounded, seekable mobile previews."""
import hashlib
import json
import shutil
import subprocess

import av
import imageio_ffmpeg
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from src import video_preview_proxy as proxy
from routes.video_server_file_routes import add_server_video_file_routes


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / 'drive'
    root.mkdir()
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'f': str(root)}))
    monkeypatch.setattr(proxy, 'CACHE_ROOT', tmp_path / 'previews')
    path = root / 'unsupported.mkv'
    subprocess.run([shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=96x64:rate=24:duration=6', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=6', '-c:v', 'libx264', '-threads', '1',
                    '-c:a', 'eac3', '-y', str(path)], check=True, timeout=20)
    stat = path.stat()
    return path, {'root': 'f', 'path': path.name, 'size': stat.st_size, 'mtime_ns': str(stat.st_mtime_ns)}


def test_mkv_eac3_preview_is_mp4_h264_aac_and_source_is_unchanged(source):
    path, selection = source
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    result = proxy.get_server_video_preview(selection, start_seconds=2.5, duration_seconds=2)
    with av.open(str(result)) as container:
        assert container.streams.video[0].codec_context.name == 'h264'
        assert container.streams.audio[0].codec_context.name == 'aac'
        assert container.duration / av.time_base == pytest.approx(2, abs=.05)
        assert container.streams.video[0].width <= 640 and container.streams.video[0].height <= 360
        assert len(list(container.decode(video=0))) == 48
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def test_preview_cache_reuses_only_unchanged_source_and_selected_window(source, monkeypatch):
    path, selection = source
    first = proxy.get_server_video_preview(selection, duration_seconds=2)
    stamp = first.stat().st_mtime_ns
    assert proxy.get_server_video_preview(selection, duration_seconds=2) == first
    assert first.stat().st_mtime_ns == stamp
    other = proxy.get_server_video_preview(selection, start_seconds=2, duration_seconds=2)
    assert other != first
    path.write_bytes(path.read_bytes() + b'changed')
    current = proxy.get_server_video_preview(selection, duration_seconds=2)
    assert current != first and current.is_file()


@pytest.mark.parametrize('start,length', [(-1, 3), (float('nan'), 3), (0, 31), (0, .1)])
def test_preview_duration_bound_rejected_before_source_open(start, length):
    with pytest.raises(HTTPException) as raised:
        proxy.get_server_video_preview({}, start_seconds=start, duration_seconds=length)
    assert raised.value.status_code == 400


def test_compatible_preview_requires_auth_and_supports_phone_byte_ranges(source):
    path, selection = source
    router = APIRouter(prefix='/api/video/h3')
    def owner(request):
        if request.headers.get('x-user') != 'corey':
            raise HTTPException(401, 'Sign in')
    add_server_video_file_routes(router, owner)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as client:
        url = '/api/video/h3/server-files/preview'
        params = {**selection, 'start_seconds': 1, 'duration_seconds': 2}
        assert client.get(url, params=params).status_code == 401
        reply = client.get(url, params=params, headers={'x-user': 'corey', 'range': 'bytes=0-31'})
        assert reply.status_code == 206 and len(reply.content) == 32
        assert reply.headers['content-type'] == 'video/mp4'
        assert reply.headers['x-preview-start-seconds'] == '1.0'
        assert b'ftyp' in reply.content
        head = client.head(url, params=params, headers={'x-user': 'corey'})
        assert head.status_code == 200 and not head.content


def test_open_preview_survives_cache_eviction_before_response_starts(source, monkeypatch):
    _, selection = source
    handles = []
    original = proxy.open_server_video_proxy
    def evict_after_open(value, **options):
        upload = original(value, **options)
        handles.append(upload.file)
        for path in proxy.CACHE_ROOT.glob('*.mp4'):
            path.unlink()
        return upload
    monkeypatch.setattr(proxy, 'open_server_video_proxy', evict_after_open)
    router = APIRouter(prefix='/api/video/h3')
    add_server_video_file_routes(router, lambda request: 'corey')
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        result = client.get('/api/video/h3/server-files/preview',
                            params={**selection, 'duration_seconds': 2}, headers={'range': 'bytes=0-31'})
        assert result.status_code == 206 and b'ftyp' in result.content
        assert result.headers['content-length'] == '32'
    assert len(handles) == 1 and handles[0].closed


def test_proxy_timeout_closes_source_releases_slot_and_removes_partial(source, monkeypatch):
    from src import video_media_metadata
    monkeypatch.setattr(video_media_metadata, 'get_open_video_metadata',
                        lambda *args: {'duration_seconds': 6, 'video_start_offset_seconds': 0})
    monkeypatch.setattr(proxy, '_playback_ffmpeg', lambda: imageio_ffmpeg.get_ffmpeg_exe())
    handles = []
    original = proxy.open_server_video_preview
    def record(selection):
        upload = original(selection)
        handles.append(upload.file)
        return upload
    def run(command, **kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(b'partial converter output')
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(proxy, 'open_server_video_preview', record)
    monkeypatch.setattr(proxy.subprocess, 'run', run)
    with pytest.raises(HTTPException) as raised:
        proxy.get_server_video_preview(source[1], duration_seconds=2)
    assert raised.value.status_code == 504
    assert handles[0].closed and not list(proxy.CACHE_ROOT.glob('*.partial.mp4'))
    first = proxy._ENCODERS.acquire(blocking=False)
    second = proxy._ENCODERS.acquire(blocking=False)
    if first:
        proxy._ENCODERS.release()
    if second:
        proxy._ENCODERS.release()
    assert first and second


def test_playback_prefers_installed_codec_capable_binary_over_app_ffmpeg(tmp_path, monkeypatch):
    bundled, application = tmp_path / 'bundled-ffmpeg', tmp_path / 'app-ffmpeg'
    bundled.touch(); application.touch()
    calls = []
    monkeypatch.setattr(imageio_ffmpeg, 'get_ffmpeg_exe', lambda: str(bundled))
    monkeypatch.setattr(proxy.shutil, 'which', lambda _: str(application))
    def encoders(command, **kwargs):
        calls.append(command[0])
        output = b' V..... libx264 H.264\n A..... aac AAC\n' if command[0] == str(bundled) else b' V..... hevc HEVC\n'
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b'')
    monkeypatch.setattr(proxy.subprocess, 'run', encoders)
    assert proxy._playback_ffmpeg() == str(bundled)
    assert proxy._playback_ffmpeg() == str(bundled)
    assert calls == [str(bundled)]


def test_playback_fallback_checks_required_codecs_and_fails_clearly(tmp_path, monkeypatch):
    executable = tmp_path / 'ffmpeg'
    executable.touch()
    monkeypatch.setattr(imageio_ffmpeg, 'get_ffmpeg_exe', lambda: str(tmp_path / 'missing'))
    monkeypatch.setattr(proxy.shutil, 'which', lambda _: str(executable))
    monkeypatch.setattr(proxy.subprocess, 'run', lambda command, **kwargs:
        subprocess.CompletedProcess(command, 0, stdout=b' A..... aac AAC\n', stderr=b''))
    with pytest.raises(HTTPException) as error:
        proxy._playback_ffmpeg()
    assert error.value.status_code == 503 and 'libx264' in error.value.detail
