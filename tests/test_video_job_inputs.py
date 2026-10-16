"""Saved source playback is lazy, owned, seekable, and never silently shortened."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess

import av
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
import imageio_ffmpeg
import pytest

from routes.video_job_input_routes import add_job_input_routes
from src.h3_video import H3JobManager, _read, _write
from src import video_job_inputs as inputs, video_preview_proxy as proxy


def make_video(path, seconds=3):
    subprocess.run([shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size=96x64:rate=24:duration={seconds}',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
        '-c:v', 'libx264', '-threads', '1', '-c:a', 'eac3', '-y', str(path)], check=True, timeout=20)


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, 'CACHE_ROOT', tmp_path / 'playback-cache')
    manager = H3JobManager(tmp_path / 'jobs', project=tmp_path)
    directory = manager.stage()
    original = directory / 'source.mkv'
    make_video(original)
    _write(directory / 'state.json', {'id': directory.name, 'owner': 'corey', 'status': 'completed', 'created_at': 1})
    _write(directory / 'manifest.json', {'config': {'recipe': 'seed_hunter', 'seed_hunter': {'stage': 'preview'},
        'frames': 107, 'reference_video_ranges': []}, 'reference_videos': [str(original)],
        'input_names': {'reference_videos': ['original scene.mkv']}, 'revision': 0})
    router = APIRouter(prefix='/api/video/h3')
    def owner(request):
        value = request.headers.get('x-user')
        if not value:
            raise HTTPException(401, 'Sign in')
        return value
    add_job_input_routes(router, manager, owner)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield manager, directory, client


def request(saved, *, method='GET', user='corey', field='reference_videos', index=0, version=0, headers=None):
    _, directory, client = saved
    return client.request(method, f'/api/video/h3/jobs/{directory.name}/input-videos/{field}/{index}/preview',
                          params={'revision': version}, headers={'x-user': user, **(headers or {})})


def test_descriptors_are_manifest_only_preserve_order_names_and_sections(saved, monkeypatch):
    manager, directory, _ = saved
    manifest = _read(directory / 'manifest.json')
    manifest.update(source_video=str(directory / 'missing-source.mkv'),
                    reference_videos=[str(directory / 'ref-1.mkv'), str(directory / 'missing-ref-2.mp4')],
                    input_names={'source_video': ['../source.mkv'], 'reference_videos': ['C:\\private\\first.mkv', 'second.mp4']})
    manifest['config']['reference_video_ranges'] = [{'start_seconds': 12, 'duration_seconds': 15},
                                                    {'start_seconds': 0, 'duration_seconds': 6}]
    _write(directory / 'manifest.json', manifest)
    monkeypatch.setattr(proxy, 'open_owned_video_proxy', lambda *a, **k: pytest.fail('History must not convert media'))
    monkeypatch.setattr(Path, 'stat', lambda *a, **k: pytest.fail('Descriptors must not stat media'))
    result = inputs.input_videos(directory.name, manifest)
    assert [item['id'] for item in result] == ['source_video:0', 'reference_videos:0', 'reference_videos:1']
    assert [item['name'] for item in result] == ['source.mkv', 'first.mkv', 'second.mp4']
    assert result[0]['playback'] == 'original' and result[0]['duration_seconds'] is None
    assert result[1]['playback'] == 'selected_section' and result[1]['start_seconds'] == 12
    assert result[1]['duration_seconds'] == 15 and result[1]['revision'] == 0
    assert str(directory) not in json.dumps(result)


def test_hunter_and_scout_views_expose_original_input_descriptors(saved):
    manager, directory, _ = saved
    for stage in ('preview', 'scout', 'final'):
        manifest = _read(directory / 'manifest.json')
        manifest['config']['seed_hunter']['stage'] = stage
        _write(directory / 'manifest.json', manifest)
        result = manager.view(directory.name, 'corey')
        assert result['input_videos'][0]['name'] == 'original scene.mkv'
        assert not proxy.CACHE_ROOT.exists()


def test_mkv_original_is_seekable_h264_aac_and_owned(saved):
    manager, directory, client = saved
    original = (directory / 'source.mkv').read_bytes()
    reply = request(saved)
    assert reply.status_code == 200, reply.text[:200] if reply.status_code != 200 else ''
    assert reply.headers['content-type'] == 'video/mp4'
    assert reply.headers['x-preview-playback'] == 'original'
    assert reply.headers['cache-control'] == 'private, no-store'
    with av.open(io.BytesIO(reply.content)) as video:
        assert video.streams.video[0].codec_context.name == 'h264'
        assert video.streams.audio[0].codec_context.name == 'aac'
        assert video.duration / av.time_base == pytest.approx(3, abs=.1)
    ranged = request(saved, headers={'Range': 'bytes=4-63'})
    assert ranged.status_code == 206 and ranged.content == reply.content[4:64]
    assert ranged.headers['accept-ranges'] == 'bytes'
    head = request(saved, method='HEAD')
    assert head.status_code == 200 and not head.content and int(head.headers['content-length']) == len(reply.content)
    assert (directory / 'source.mkv').read_bytes() == original
    assert request(saved, user='other').status_code == 404
    assert request(saved, user='').status_code == 401
    assert request(saved, headers={'Range': 'bytes=9-1'}).status_code == 416


def test_full_original_is_not_truncated_at_preview_or_thirty_second_limit(saved):
    _, directory, _ = saved
    make_video(directory / 'source.mkv', 34)
    reply = request(saved)
    assert reply.status_code == 200
    with av.open(io.BytesIO(reply.content)) as video:
        assert video.duration / av.time_base == pytest.approx(34, abs=.1)
        assert len(list(video.decode(video=0))) == 816


def test_explicit_selected_section_is_labelled_and_only_that_section_is_encoded(saved):
    _, directory, _ = saved
    manifest = _read(directory / 'manifest.json')
    manifest['config']['reference_video_ranges'] = [{'start_seconds': 1, 'duration_seconds': 1.5}]
    _write(directory / 'manifest.json', manifest)
    reply = request(saved)
    assert reply.status_code == 200 and reply.headers['x-preview-playback'] == 'selected_section'
    assert reply.headers['x-preview-start-seconds'] == '1' and reply.headers['x-preview-duration-seconds'] == '1.5'
    with av.open(io.BytesIO(reply.content)) as video:
        assert video.duration / av.time_base == pytest.approx(1.5, abs=.1)


def test_edit_revision_is_checked_before_transcoding_but_timestamp_is_not_a_precondition(saved, monkeypatch):
    _, directory, _ = saved
    manifest = _read(directory / 'manifest.json')
    manifest['revision'] = 1
    _write(directory / 'manifest.json', manifest)
    with monkeypatch.context() as patch:
        patch.setattr(proxy, 'open_owned_video_proxy', lambda *a, **k: pytest.fail('Stale revision opened media'))
        assert request(saved).status_code == 409
    os.utime(directory / 'source.mkv', None)
    assert request(saved, version=1).status_code == 200


def test_edit_during_conversion_rejects_old_reference_and_closes_handle(saved, monkeypatch):
    _, directory, _ = saved
    original = proxy.open_owned_video_proxy
    handles = []
    def edit_after_prepare(*args, **kwargs):
        upload = original(*args, **kwargs)
        handles.append(upload.file)
        manifest = _read(directory / 'manifest.json')
        _write(directory / 'manifest.json', {**manifest, 'revision': 1})
        return upload
    monkeypatch.setattr(proxy, 'open_owned_video_proxy', edit_after_prepare)
    assert request(saved).status_code == 409
    assert handles[0].closed


def test_deleted_job_cannot_republish_an_inflight_playback_copy(saved, monkeypatch):
    _, directory, _ = saved
    original_run = proxy.subprocess.run
    def delete_during_conversion(command, **kwargs):
        result = original_run(command, **kwargs)
        if '-i' in command:
            proxy.delete_owned_previews(directory.name)
        return result
    monkeypatch.setattr(proxy.subprocess, 'run', delete_during_conversion)
    reply = request(saved)
    assert reply.status_code == 404
    assert not list(proxy.CACHE_ROOT.glob('*.mp4'))
    assert (directory / 'source.mkv').exists()


@pytest.mark.parametrize('mutation', ['missing', 'symlink', 'outside', 'directory'])
def test_missing_and_escaped_saved_files_fail_without_serving_cached_private_data(saved, tmp_path, mutation):
    _, directory, _ = saved
    assert request(saved).status_code == 200
    path = directory / 'source.mkv'
    original = path.read_bytes()
    path.unlink()
    if mutation in {'symlink', 'outside'}:
        outside = tmp_path / 'private.mkv'
        outside.write_bytes(original)
        if mutation == 'symlink':
            path.symlink_to(outside)
        else:
            manifest = _read(directory / 'manifest.json')
            manifest['reference_videos'] = [str(outside)]
            _write(directory / 'manifest.json', manifest)
    elif mutation == 'directory':
        path.mkdir()
    response = request(saved)
    assert response.status_code == 404 and str(tmp_path) not in response.text


def test_oversized_playback_copy_is_removed_and_not_published(saved, monkeypatch):
    from src import video_media_metadata
    monkeypatch.setattr(video_media_metadata, 'get_open_video_metadata', lambda *a: {'duration_seconds': 3})
    monkeypatch.setattr(proxy, '_playback_ffmpeg', lambda: imageio_ffmpeg.get_ffmpeg_exe())
    def fake_convert(command, **kwargs):
        assert '-t' not in command
        assert int(command[command.index('-fs') + 1]) > proxy.MAX_CACHE_BYTES
        with Path(command[-1]).open('wb') as output:
            output.truncate(proxy.MAX_CACHE_BYTES + 1)
        return subprocess.CompletedProcess(command, 0, stdout=b'', stderr=b'')
    monkeypatch.setattr(proxy.subprocess, 'run', fake_convert)
    response = request(saved)
    assert response.status_code == 507 and 'no truncated preview' in response.text
    assert not list(proxy.CACHE_ROOT.glob('*.mp4'))
