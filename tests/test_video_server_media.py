"""Image/audio server selections use the same safe, immutable upload boundary."""
import asyncio
import io
import json
import os
from fractions import Fraction
from pathlib import Path

from fastapi import HTTPException
import pytest
from starlette.datastructures import UploadFile

from src import h3_video as h3, video_server_files as media
from tests.test_video_server_files import drive, descriptor, inputs, make_file
from tests.test_video_submission import backend, image


@pytest.mark.parametrize('kind', ['video', 'image', 'audio'])
def test_browser_media_kind_filters_files_but_keeps_folders_and_versions(drive, kind):
    (drive / 'Folder').mkdir()
    for category, extensions in media.MEDIA_KINDS.items():
        for extension in extensions:
            make_file(drive, category + extension.upper())
    make_file(drive, 'private.txt')
    make_file(drive, 'adapter.safetensors')
    listing = media.list_server_videos(kind=kind)
    assert listing['entries'][0]['kind'] == 'directory'
    files = listing['entries'][1:]
    assert len(files) == len(media.MEDIA_KINDS[kind])
    assert all(entry['kind'] == kind for entry in files)
    for entry in files:
        assert entry['selection'] == {key: value for key, value in
            descriptor(drive / entry['name'], drive).items() if key != 'index'}
    assert str(drive) not in json.dumps(listing)
    page = media.list_server_videos(kind=kind, search=kind, limit=1)
    assert page['total'] == len(files) and page['has_more']
    assert len(page['entries']) == 1 and page['entries'][0]['kind'] == kind


@pytest.mark.parametrize('family', ['h3', 'bfs'])
@pytest.mark.parametrize('kind,filename', [('image', 'portrait.PNG'), ('audio', 'voice.FLAC')])
def test_authenticated_listing_route_forwards_media_kind(backend, drive, family, kind, filename):
    make_file(drive, filename)
    make_file(drive, 'clip.mp4')
    url = f'/api/video/{family}/server-files'
    client = backend.client()
    response = client.get(url, params={'kind': kind})
    assert response.status_code == 200, response.text
    assert [(row['kind'], row['name']) for row in response.json()['entries']] == [(kind, filename)]
    assert response.headers['cache-control'] == 'private, no-store'
    assert client.get(url).json()['entries'][0]['kind'] == 'video'
    assert client.get(url, params={'kind': 'any'}).status_code == 400
    assert client.get(url, params={'kind': kind}, headers={'x-user': 'reader'}).status_code == 403


@pytest.mark.parametrize('kind', ['', 'all', 'IMAGE', '../video', None, ['audio']])
def test_browser_rejects_unknown_media_kinds(drive, kind):
    with pytest.raises(HTTPException) as error:
        media.list_server_videos(kind=kind)
    assert error.value.status_code == 400


@pytest.mark.parametrize('field', list(h3.UPLOAD_EXTENSIONS))
def test_every_h3_attachment_field_accepts_its_declared_server_extensions(drive, field):
    selections = []
    for index, extension in enumerate(sorted(h3.UPLOAD_EXTENSIONS[field])):
        selections.append(descriptor(make_file(drive, f'file-{index}{extension}'), drive, index))
    opened = []
    async def run():
        async with media.server_video_inputs(inputs({field: selections}), {}, h3.UPLOAD_EXTENSIONS) as uploads:
            opened.extend(uploads[field])
            assert [item.filename for item in opened] == [item['path'] for item in selections]
            assert all([await item.read() == b'video-content' for item in opened])
    asyncio.run(run())
    assert all(item.file.closed for item in opened)


@pytest.mark.parametrize('field,filename', [('reference_images', 'portrait.png'), ('reference_audio', 'voice.wav')])
def test_mixed_local_and_server_companions_keep_per_field_order_and_close_handles(drive, field, filename):
    server = make_file(drive, filename, b'server')
    local = UploadFile(io.BytesIO(b'phone'), filename='phone' + server.suffix, size=5)
    opened = []
    async def run():
        async with media.server_video_inputs(inputs({field: [descriptor(server, drive, 1)]}),
                                             {field: [local]}, h3.UPLOAD_EXTENSIONS) as uploads:
            assert [await item.read() for item in uploads[field]] == [b'phone', b'server']
            opened.append(uploads[field][1])
    asyncio.run(run())
    assert opened[0].file.closed and not local.file.closed


@pytest.mark.parametrize('field,filename', [
    ('reference_videos', 'voice.wav'), ('reference_images', 'voice.wav'),
    ('reference_videos', 'voice.mka'), ('reference_images', 'voice.mka'),
    ('reference_audio', 'face.png'), ('first_frame', 'video.mp4'),
    ('reference_mods', 'image.png'), ('unknown', 'voice.wav'),
])
def test_server_media_cannot_cross_attachment_field_types(drive, field, filename):
    source = make_file(drive, filename)
    async def run():
        async with media.server_video_inputs(inputs({field: [descriptor(source, drive)]}), {}, h3.UPLOAD_EXTENSIONS):
            pytest.fail('Wrong attachment type accepted')
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 400


@pytest.mark.parametrize('field,filename', [('reference_images', 'image.webp'), ('reference_audio', 'audio.opus')])
@pytest.mark.parametrize('change', ['symlink', 'traversal'])
def test_nonvideo_selections_keep_safe_path_checks(drive, tmp_path, field, filename, change):
    source = make_file(drive, filename)
    selected = descriptor(source, drive)
    if change == 'symlink':
        outside = tmp_path / 'outside'
        outside.write_bytes(b'video-content')
        source.unlink()
        source.symlink_to(outside)
    else:
        selected['path'] = '../' + filename
    async def run():
        async with media.server_video_inputs(inputs({field: [selected]}), {}, h3.UPLOAD_EXTENSIONS):
            pytest.fail('Escaped media accepted')
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 400


@pytest.mark.parametrize('field,filename', [('reference_images', 'image.webp'), ('reference_audio', 'audio.opus'),
                                           ('reference_audio', 'voice.mka')])
def test_nonvideo_selections_accept_current_file_after_listing_metadata_changes(drive, field, filename):
    source = make_file(drive, filename)
    selected = descriptor(source, drive)
    source.write_bytes(b'updated media bytes')
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 10_000_000))
    async def run():
        async with media.server_video_inputs(inputs({field: [selected]}), {}, h3.UPLOAD_EXTENSIONS) as uploads:
            assert uploads[field][0].size == len(b'updated media bytes')
            assert await uploads[field][0].read() == b'updated media bytes'
    asyncio.run(run())


@pytest.mark.parametrize('filename', ['image.png', 'audio.wav', 'audio.mka'])
def test_video_preview_remains_video_only(backend, drive, filename):
    source = make_file(drive, filename)
    selected = descriptor(source, drive)
    selected.pop('index')
    response = backend.client().get('/api/video/h3/server-files/video', params=selected)
    assert response.status_code == 400


@pytest.mark.parametrize('stale_listing', [False, True])
def test_h3_job_snapshots_mixed_local_and_server_companions_in_order(backend, drive, stale_listing):
    server_video = make_file(drive, 'context.mp4', b'context video')
    server_image = make_file(drive, 'reference.png', image())
    server_audio = make_file(drive, 'voice.wav', b'server audio')
    selections = {'reference_videos': [descriptor(server_video, drive, 1)],
                  'reference_images': [descriptor(server_image, drive)],
                  'reference_audio': [descriptor(server_audio, drive, 1)]}
    current_video, current_audio = b'context video', b'server audio'
    if stale_listing:
        current_video, current_audio = b'current context video', b'current server audio'
        server_video.write_bytes(current_video)
        server_audio.write_bytes(current_audio)
        os.utime(server_image, ns=(server_image.stat().st_atime_ns,
                                  server_image.stat().st_mtime_ns + 10_000_000))
    response = backend.client().post('/api/video/h3/jobs',
        data={'config': json.dumps(backend.configs['h3']), 'server_inputs': json.dumps(selections)},
        files=[('reference_videos', ('primary.mp4', b'primary video', 'video/mp4')),
               ('reference_audio', ('phone.wav', b'phone audio', 'audio/wav'))])
    assert response.status_code == 201, response.text
    directory = backend.managers['h3'].directory(response.json()['id'])
    manifest = h3._read(directory / 'manifest.json')
    expected = {'reference_videos': [b'primary video', current_video],
                'reference_images': [image()], 'reference_audio': [b'phone audio', current_audio]}
    assert manifest['input_names']['reference_audio'] == ['phone.wav', 'voice.wav']
    assert manifest['input_names']['reference_videos'] == ['primary.mp4', 'context.mp4']
    for field, values in expected.items():
        assert [Path(path).read_bytes() for path in manifest[field]] == values
        assert all(Path(path).parent == directory for path in manifest[field])
    server_audio.write_bytes(b'changed after submission')
    assert Path(manifest['reference_audio'][1]).read_bytes() == current_audio
    assert len(backend.calls) == 1


def test_bfs_job_accepts_server_identity_image_with_local_source_video(backend, drive):
    identity = make_file(drive, 'identity.png', image())
    response = backend.client().post('/api/video/bfs/jobs',
        data={'config': json.dumps(backend.configs['bfs']),
              'server_inputs': json.dumps({'identity_image': [descriptor(identity, drive)]})},
        files={'source_video': ('source.mp4', b'video', 'video/mp4')})
    assert response.status_code == 201, response.text
    directory = backend.managers['bfs'].directory(response.json()['id'])
    manifest = h3._read(directory / 'manifest.json')
    assert Path(manifest['identity_image']).read_bytes() == image()
    assert Path(manifest['identity_image']).parent == directory
    assert manifest['input_names']['identity_image'] == ['identity.png']
    assert len(backend.calls) == 1


@pytest.mark.parametrize('family,field', [('h3', 'reference_images'), ('bfs', 'identity_image')])
def test_job_route_rejects_wrong_kind_before_queue_publication(backend, drive, family, field):
    source = make_file(drive, 'voice.wav')
    response = backend.client().post(f'/api/video/{family}/jobs',
        data={'config': json.dumps(backend.configs[family]),
              'server_inputs': json.dumps({field: [descriptor(source, drive)]})},
        files={'reference_videos' if family == 'h3' else 'source_video': ('source.mp4', b'video', 'video/mp4')})
    assert response.status_code == 400 and not backend.calls
    root = backend.managers[family].root
    assert not root.exists() or not [path for path in root.iterdir() if h3.JOB_ID.fullmatch(path.name)]


@pytest.fixture
def cpu_audio_backend(request):
    # Import CPU tensor support before the API fixture replaces subprocess.Popen
    # with a no-worker stub (the import may inspect the system's linker cache).
    pytest.importorskip('torch')
    return request.getfixturevalue('backend')


@pytest.mark.parametrize('source_kind', ['local', 'server'])
def test_real_mka_audio_upload_decodes_with_existing_cpu_reader(cpu_audio_backend, drive, source_kind):
    """Exercise Matroska bytes through upload snapshot, demux and stereo resample."""
    av = pytest.importorskip('av')
    np = pytest.importorskip('numpy')
    from scripts.h3_video_worker import read_audio
    backend = cpu_audio_backend
    source = drive / 'voice.mka'
    sample_rate, sample_count = 48000, 108000
    samples = (np.sin(np.arange(sample_count) * .1) * 4000).astype('<i2')[None, :]
    with av.open(str(source), 'w', format='matroska') as output:
        stream = output.add_stream('pcm_s16le', rate=sample_rate)
        stream.layout = 'mono'
        frame = av.AudioFrame.from_ndarray(samples, format='s16', layout='mono')
        frame.sample_rate = sample_rate
        frame.time_base = Fraction(1, sample_rate)
        frame.pts = 0
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    listing = media.list_server_videos(kind='audio')
    assert [(entry['name'], entry['kind']) for entry in listing['entries']] == [('voice.mka', 'audio')]
    data = {'config': json.dumps(backend.configs['h3'])}
    files = [('reference_images', ('reference.png', image(), 'image/png'))]
    if source_kind == 'server':
        data['server_inputs'] = json.dumps({'reference_audio': [descriptor(source, drive)]})
    else:
        files.append(('reference_audio', ('voice.mka', source.read_bytes(), 'audio/x-matroska')))
    response = backend.client().post('/api/video/h3/jobs', data=data, files=files)
    assert response.status_code == 201, response.text
    directory = backend.managers['h3'].directory(response.json()['id'])
    manifest = h3._read(directory / 'manifest.json')
    snapshot = Path(manifest['reference_audio'][0])
    assert snapshot.suffix == '.mka' and snapshot.parent == directory
    assert snapshot.read_bytes() == source.read_bytes()
    assert manifest['input_names']['reference_audio'] == ['voice.mka']
    audio, duration = read_audio(snapshot)
    assert duration == pytest.approx(2.25)
    assert audio['sample_rate'] == 32000
    assert tuple(audio['waveform'].shape) == (1, 2, 72000)
    assert audio['waveform'].device.type == 'cpu'
    assert float(audio['waveform'].abs().max()) > 0
    assert np.allclose(audio['waveform'][0, 0], audio['waveform'][0, 1])
