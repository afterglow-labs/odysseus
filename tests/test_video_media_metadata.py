"""Seekable header metadata never reopens a browser-supplied media path."""
import io
import json
import os
import subprocess
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from src import video_media_metadata as metadata


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / 'scene.mp4'
    path.write_bytes(b'fixture')
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'f': str(tmp_path)}))
    metadata._CACHE.clear()
    info = path.stat()
    return path, {'root': 'f', 'path': path.name, 'size': info.st_size, 'mtime_ns': str(info.st_mtime_ns)}


def details(duration=6.172):
    return {'duration_seconds': duration, 'duration_estimated': False, 'video_start_offset_seconds': 0.,
            'video_codec': 'h264', 'audio_codec': 'aac', 'width': 64, 'height': 64,
            'mime_type': 'video/mp4', 'browser_compatible': True, 'ios_compatible': True}


def test_metadata_cache_tracks_current_handle_and_ignores_stale_listing(source, monkeypatch):
    calls = []
    def probe(upload):
        calls.append(upload)
        return details(len(calls))
    monkeypatch.setattr(metadata, 'probe_open_video', probe)
    path, selection = source
    first = metadata.get_server_video_metadata(selection)
    first['duration_seconds'] = 999
    assert metadata.get_server_video_metadata(selection)['duration_seconds'] == 1
    assert len(calls) == 1 and calls[0].file.closed
    info = path.stat()
    path.write_bytes(b'changed')
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert metadata.get_server_video_metadata(selection)['duration_seconds'] == 2
    assert metadata.get_server_video_metadata({**selection, 'size': 1})['duration_seconds'] == 2
    assert len(calls) == 2


def test_open_handle_remains_original_during_path_replacement(source, monkeypatch):
    original = metadata.open_server_video_preview
    path, selection = source
    def replace(value):
        upload = original(value)
        path.parent.rename(path.parent.with_name(path.parent.name + '-moved'))
        path.parent.mkdir()
        path.write_bytes(b'unrelated private data')
        return upload
    def probe(upload):
        assert upload.file.read() == b'fixture'
        return details()
    monkeypatch.setattr(metadata, 'open_server_video_preview', replace)
    monkeypatch.setattr(metadata, 'probe_open_video', probe)
    assert metadata.get_server_video_metadata(selection)['duration_seconds'] == 6.172


def test_probe_subprocess_uses_inherited_seekable_handle_with_timeout(source, monkeypatch):
    opened = []
    def run(argv, **kwargs):
        assert argv[:4] == [metadata.sys.executable, '-m', 'src.video_media_metadata', '--probe-stdin']
        assert argv[4] == '.mp4' and str(source[0]) not in argv
        assert kwargs['timeout'] == metadata.PROBE_TIMEOUT and kwargs['stdin'].seekable()
        assert 'shell' not in kwargs
        opened.append(kwargs['stdin'])
        raise subprocess.TimeoutExpired(argv, metadata.PROBE_TIMEOUT)
    monkeypatch.setattr(metadata.subprocess, 'run', run)
    with pytest.raises(HTTPException) as error:
        metadata.get_server_video_metadata(source[1])
    assert error.value.status_code == 504 and opened[0].closed


def test_invalid_metadata_is_clear_and_never_leaks_diagnostics(source, monkeypatch):
    monkeypatch.setattr(metadata.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout=b'{bad', stderr=b'/private/path'))
    with pytest.raises(HTTPException) as error:
        metadata.get_server_video_metadata(source[1])
    assert error.value.status_code == 422 and '/private' not in error.value.detail


def test_global_probe_limit_returns_retryable_error_without_leaking_handle(source, monkeypatch):
    opened = []
    original = metadata.open_server_video_preview
    def record(selection):
        upload = original(selection)
        opened.append(upload)
        return upload
    monkeypatch.setattr(metadata, 'open_server_video_preview', record)
    monkeypatch.setattr(metadata, '_PROBES', SimpleNamespace(acquire=lambda **kwargs: False))
    with pytest.raises(HTTPException) as error:
        metadata.get_server_video_metadata(source[1])
    assert error.value.status_code == 429 and error.value.headers['Retry-After'] == '1'
    assert opened[0].file.closed


def test_header_read_budget_is_cumulative_even_with_seeks():
    data = metadata._BudgetReader(io.BytesIO(b'x' * 100), limit=12)
    assert len(data.read(10)) == 10
    assert data.seek(50) == 50
    assert len(data.read(100)) == 2
    with pytest.raises(OSError, match='bounded header read'):
        data.read(1)


def test_header_inspection_never_iterates_packets_or_decodes_frames(monkeypatch):
    video = SimpleNamespace(duration=1200, time_base=.01, start_time=200,
                            codec_context=SimpleNamespace(name='h264', width=64, height=64))
    class Source:
        streams = SimpleNamespace(video=[video], audio=[])
        start_time = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def demux(self, *args):
            pytest.fail('metadata must not scan packets')
        def decode(self, *args):
            pytest.fail('metadata must not decode frames')
    monkeypatch.setitem(metadata.sys.modules, 'av', SimpleNamespace(open=lambda *args, **kwargs: Source(), time_base=1000000))
    result = metadata._probe_stream(io.BytesIO(), '.mp4')
    assert result['duration_seconds'] == 12 and not result['duration_estimated']
    assert result['video_start_offset_seconds'] == 2 and result['ios_compatible']


@pytest.fixture(scope='module')
def real_videos(tmp_path_factory):
    import imageio_ffmpeg
    folder = tmp_path_factory.mktemp('server-metadata-video')
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    mp4, mkv = folder / 'movie.mp4', folder / 'movie.mkv'
    subprocess.run([executable, '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'color=c=red:s=64x64:r=24:d=6.125', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=32000:duration=6.125', '-c:v', 'libx264',
                    '-threads', '2', '-c:a', 'aac', str(mp4)], check=True, capture_output=True, timeout=30)
    subprocess.run([executable, '-nostdin', '-loglevel', 'error', '-i', str(mp4),
                    '-c:v', 'copy', '-c:a', 'eac3', str(mkv)], check=True, capture_output=True, timeout=30)
    return mp4, mkv


@pytest.mark.parametrize('index,codec,mime,compatible', [
    (0, 'aac', 'video/mp4', True), (1, 'eac3', 'video/x-matroska', False),
])
def test_real_duration_and_ios_codec_compatibility(real_videos, monkeypatch, index, codec, mime, compatible):
    path = real_videos[index]
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'f': str(path.parent)}))
    info = path.stat()
    selection = {'root': 'f', 'path': path.name, 'size': info.st_size, 'mtime_ns': str(info.st_mtime_ns)}
    result = metadata.get_server_video_metadata(selection)
    assert result['duration_seconds'] == pytest.approx(6.125, abs=.08)
    assert result['video_codec'] == 'h264' and result['audio_codec'] == codec
    assert result['mime_type'] == mime and result['ios_compatible'] is compatible
    assert result['width'] == result['height'] == 64


def test_metadata_on_caller_owned_handle_preserves_position_and_lifetime(real_videos, monkeypatch):
    path = real_videos[0]
    metadata._CACHE.clear()
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'f': str(path.parent)}))
    info = path.stat()
    selection = {'root': 'f', 'path': path.name, 'size': info.st_size, 'mtime_ns': str(info.st_mtime_ns)}
    upload = metadata.open_server_video_preview(selection)
    try:
        upload.file.seek(5)
        result = metadata.get_open_video_metadata(upload, selection)
        assert result['duration_seconds'] == pytest.approx(6.125, abs=.08)
        assert upload.file.tell() == 5 and not upload.file.closed
    finally:
        upload.file.close()
