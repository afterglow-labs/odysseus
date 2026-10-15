"""Server-folder browsing, safe positional inputs and immutable job copies."""
import asyncio
import io
import json
import os
from pathlib import Path
import threading

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.datastructures import FormData, UploadFile

from src import video_server_files as media


VIDEO_FIELDS = {'source_video': media.VIDEO_EXTENSIONS, 'reference_videos': media.VIDEO_EXTENSIONS,
                'first_frame': {'.jpg', '.png'}}


@pytest.fixture
def drive(tmp_path, monkeypatch):
    root = tmp_path / 'drive'
    root.mkdir()
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'e': str(root)}))
    return root


def descriptor(path, drive, index=0):
    info = path.stat()
    return {'root': 'e', 'path': path.relative_to(drive).as_posix(), 'size': info.st_size,
            'mtime_ns': str(info.st_mtime_ns), 'index': index}


def inputs(value):
    return FormData({'server_inputs': json.dumps(value)})


def make_file(drive, name='clip.mp4', content=b'video-content'):
    path = drive / name
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(content)
    return path


def test_browser_lists_only_regular_videos_and_folders_with_stable_selection(drive):
    (drive / 'TV Shows').mkdir()
    video = make_file(drive, 'Z Clip.MP4')
    make_file(drive, 'A Clip.mov')
    make_file(drive, 'password.txt')
    (drive / 'linked.mp4').symlink_to(video)
    listing = media.list_server_videos(limit=2)
    assert listing['roots'] == [{'id': 'e', 'name': 'E:', 'available': True}]
    assert listing['root'] == 'e' and listing['parent_path'] is None
    assert [item['name'] for item in listing['entries']] == ['TV Shows', 'A Clip.mov']
    assert listing['total'] == 3 and listing['next_offset'] == 2 and listing['has_more']
    remaining = media.list_server_videos(offset=2, limit=2)
    selection = remaining['entries'][0]['selection']
    assert selection == {key: value for key, value in descriptor(video, drive).items() if key != 'index'}
    assert isinstance(selection['mtime_ns'], str)
    assert remaining['next_offset'] is None and not remaining['has_more']
    assert str(drive) not in json.dumps(listing)
    assert media.list_server_videos(path='TV Shows')['parent_path'] == ''
    search = media.list_server_videos(search='cLiP')
    assert [item['name'] for item in search['entries']] == ['A Clip.mov', 'Z Clip.MP4']


def test_auth_checked_before_listing(drive, monkeypatch):
    from routes.video_server_file_routes import add_server_video_file_routes
    called = []
    monkeypatch.setattr('routes.video_server_file_routes.list_server_videos', lambda **kwargs: called.append(kwargs) or {})
    router = APIRouter(prefix='/api/video/h3')
    def owner(request):
        if request.headers.get('x-user') != 'admin':
            raise HTTPException(403, 'Admin required')
        return 'admin'
    add_server_video_file_routes(router, owner)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get('/api/video/h3/server-files').status_code == 403
    assert called == []
    response = client.get('/api/video/h3/server-files?path=TV%20Shows&search=clip&offset=3&limit=10', headers={'x-user': 'admin'})
    assert response.status_code == 200 and response.headers['cache-control'] == 'private, no-store'
    assert called == [dict(root='e', path='TV Shows', search='clip', offset=3, limit=10, kind='video')]


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_authenticated_root_discovery_can_find_f_when_e_is_unavailable(tmp_path, monkeypatch, family):
    from routes import video_server_file_routes
    missing_e, drive_f = tmp_path / 'E', tmp_path / 'F'
    make_file(drive_f)
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'e': str(missing_e), 'f': str(drive_f)}))
    called = []
    original = video_server_file_routes.list_server_video_roots
    monkeypatch.setattr(video_server_file_routes, 'list_server_video_roots',
                        lambda: called.append(True) or original())
    router = APIRouter(prefix=f'/api/video/{family}')
    def owner(request):
        if request.headers.get('x-user') != 'admin':
            raise HTTPException(403, 'Admin required')
        return 'admin'
    video_server_file_routes.add_server_video_file_routes(router, owner)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    endpoint = f'/api/video/{family}'
    assert client.get(endpoint + '/server-file-roots').status_code == 403
    assert called == []
    headers = {'x-user': 'admin'}
    response = client.get(endpoint + '/server-file-roots', headers=headers)
    assert response.status_code == 200 and response.headers['cache-control'] == 'private, no-store'
    assert response.json() == {'roots': [{'id': 'e', 'name': 'E:', 'available': False},
                                         {'id': 'f', 'name': 'F:', 'available': True}]}
    assert called == [True] and str(tmp_path) not in response.text
    assert client.get(endpoint + '/server-files?root=e', headers=headers).status_code == 503
    listing = client.get(endpoint + '/server-files?root=f', headers=headers)
    assert listing.status_code == 200 and listing.json()['entries'][0]['name'] == 'clip.mp4'


@pytest.mark.parametrize('path', ['../outside', '/etc', 'TV/../elsewhere', 'TV//nested', './TV', 'C:\\Users', 'TV\\file.mp4', 'clip\x00.mp4'])
def test_browser_rejects_traversal_and_cross_platform_absolute_paths(drive, path):
    with pytest.raises(HTTPException) as error:
        media.list_server_videos(path=path)
    assert error.value.status_code == 400


def test_browser_and_submission_reject_symlink_ancestors(drive, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    path = make_file(outside)
    (drive / 'escape').symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        media.list_server_videos(path='escape')
    assert error.value.status_code == 400
    value = descriptor(path, outside)
    value['path'] = 'escape/clip.mp4'
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [value]}), {}, VIDEO_FIELDS):
            pytest.fail('Opened an escaped file')
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 400


@pytest.mark.parametrize('root', ['e', 'f'])
def test_unmounted_drive_is_not_an_empty_linux_folder(monkeypatch, root):
    monkeypatch.delenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', raising=False)
    monkeypatch.setattr(media.os.path, 'ismount', lambda _path: False)
    with pytest.raises(HTTPException) as error:
        media.list_server_videos(root=root)
    assert error.value.status_code == 503 and f'{root.upper()}: is unavailable' in error.value.detail


def test_default_roots_advertise_both_windows_drives(monkeypatch):
    monkeypatch.delenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', raising=False)
    roots = media.media_roots()
    assert [(root.id, root.name) for root in roots] == [('e', 'E:'), ('f', 'F:')]
    assert [str(root.path) for root in roots] == (['E:\\', 'F:\\'] if os.name == 'nt' else ['/mnt/e', '/mnt/f'])


def test_f_drive_selection_copies_from_f_not_the_same_path_on_e(tmp_path, monkeypatch):
    drives = {identity: tmp_path / identity for identity in ('e', 'f')}
    for identity, drive in drives.items():
        make_file(drive, 'TV Shows/Cuts/clip.mp4', f'{identity} video'.encode())
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({key: str(value) for key, value in drives.items()}))
    listing = media.list_server_videos(root='f', path='TV Shows/Cuts')
    assert listing['roots'] == [{'id': 'e', 'name': 'E:', 'available': True},
                                {'id': 'f', 'name': 'F:', 'available': True}]
    assert listing['root'] == 'f' and listing['parent_path'] == 'TV Shows'
    selected = dict(listing['entries'][0]['selection'], index=0)
    assert selected['root'] == 'f' and selected['path'] == 'TV Shows/Cuts/clip.mp4'
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [selected]}), {}, VIDEO_FIELDS) as uploads:
            assert await uploads['source_video'][0].read() == b'f video'
    asyncio.run(run())


@pytest.mark.skipif(os.name == 'nt', reason='WSL drive mount validation')
def test_f_subfolder_requires_its_drive_mount(monkeypatch):
    checked = []
    monkeypatch.setattr(media.os.path, 'ismount', lambda path: checked.append(path) or False)
    monkeypatch.setattr(Path, 'is_dir', lambda _path: True)
    root = media.MediaRoot('f', 'F:', Path('/mnt/f/TV Shows'))
    assert not media._available(root)
    assert checked == [Path('/mnt/f')]


def test_missing_root_and_bad_configuration(drive, monkeypatch):
    with pytest.raises(HTTPException) as error:
        media.list_server_videos(root='private')
    assert error.value.status_code == 400
    drive.rmdir()
    with pytest.raises(HTTPException) as error:
        media.list_server_videos()
    assert error.value.status_code == 503
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', '{bad')
    with pytest.raises(HTTPException) as error:
        media.list_server_videos()
    assert error.value.status_code == 503


def test_mixed_phone_and_server_files_preserve_reference_order_and_close_handles(drive):
    first = make_file(drive, 'first.mp4', b'first')
    third = make_file(drive, 'third.mp4', b'third')
    local = UploadFile(io.BytesIO(b'second'), filename='phone.mp4', size=6)
    selections = {'reference_videos': [descriptor(third, drive, 2), descriptor(first, drive, 0)]}
    opened = []
    async def run():
        async with media.server_video_inputs(inputs(selections), {'reference_videos': [local]}, VIDEO_FIELDS) as uploads:
            assert [item.filename for item in uploads['reference_videos']] == ['first.mp4', 'phone.mp4', 'third.mp4']
            assert [await item.read() for item in uploads['reference_videos']] == [b'first', b'second', b'third']
            opened.extend(item for item in uploads['reference_videos'] if item is not local)
            assert all(not item._in_memory for item in opened)
    asyncio.run(run())
    assert all(item.file.closed for item in opened)
    assert not local.file.closed


def test_store_uploads_creates_independent_job_snapshot(drive, tmp_path):
    from routes.h3_video_routes import _store_uploads
    source = make_file(drive, 'source.mp4')
    selected = descriptor(source, drive)
    stage = tmp_path / 'job'
    stage.mkdir()
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [selected]}), {}, VIDEO_FIELDS) as uploads:
            return await _store_uploads(stage, uploads, 1024)
    saved = asyncio.run(run())
    path = Path(saved['source_video'])
    assert path.parent == stage and path != source and path.read_bytes() == b'video-content'
    assert path.stat().st_ino != source.stat().st_ino
    source.write_bytes(b'edited-later')
    assert path.read_bytes() == b'video-content'


@pytest.mark.parametrize('change', ['larger', 'smaller', 'mtime'])
def test_stale_selection_uses_current_open_file_content_and_size(drive, tmp_path, change):
    from routes.h3_video_routes import _store_uploads
    source = make_file(drive)
    selected = descriptor(source, drive)
    if change == 'larger':
        source.write_bytes(b'the newer file contains more bytes than the selected version')
    elif change == 'smaller':
        source.write_bytes(b'new')
    else:
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 10_000_000))
    expected = source.read_bytes()
    stage = tmp_path / 'job'
    stage.mkdir()
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [selected]}), {}, VIDEO_FIELDS) as uploads:
            assert uploads['source_video'][0].size == len(expected)
            return await _store_uploads(stage, uploads, 1024)
    saved = asyncio.run(run())
    assert Path(saved['source_video']).read_bytes() == expected


@pytest.mark.parametrize('kind', ['nonvideo', 'nonregular', 'symlink', 'missing', 'empty'])
def test_invalid_selection_never_opens_for_copy(drive, kind):
    source = make_file(drive)
    selected = descriptor(source, drive)
    expected = 400
    if kind == 'nonvideo':
        selected['path'] = 'password.txt'
    elif kind == 'nonregular':
        source.unlink()
        os.mkfifo(source)
    elif kind == 'symlink':
        other = make_file(drive, 'other.mp4')
        source.unlink()
        source.symlink_to(other)
    elif kind == 'missing':
        source.unlink()
        expected = 404
    elif kind == 'empty':
        source.write_bytes(b'')
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [selected]}), {}, VIDEO_FIELDS):
            pytest.fail('Invalid selection opened')
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == expected


def test_metadata_changes_while_copying_do_not_reject_selected_file(drive):
    source = make_file(drive, content=b'abc123')
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [descriptor(source, drive)]}), {}, VIDEO_FIELDS) as uploads:
            upload = uploads['source_video'][0]
            assert await upload.read(3) == b'abc'
            os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 10_000_000))
            source.chmod(0o600)
            assert await upload.read(3) == b'123'
            assert await upload.read(3) == b''
    asyncio.run(run())


def test_same_size_edits_while_copying_do_not_fail_version_check(drive):
    chunk_size = 1024 * 1024
    source = make_file(drive, content=b'a' * (2 * chunk_size))
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [descriptor(source, drive)]}), {}, VIDEO_FIELDS) as uploads:
            upload = uploads['source_video'][0]
            assert await upload.read(chunk_size) == b'a' * chunk_size
            source.write_bytes(b'z' * (2 * chunk_size))
            assert await upload.read(chunk_size) == b'z' * chunk_size
            assert await upload.read(chunk_size) == b''
    asyncio.run(run())


def test_truncated_open_file_still_fails_incomplete_copy(drive):
    chunk_size = 1024 * 1024
    source = make_file(drive, content=b'a' * (2 * chunk_size))
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [descriptor(source, drive)]}), {}, VIDEO_FIELDS) as uploads:
            upload = uploads['source_video'][0]
            assert len(await upload.read(chunk_size)) == chunk_size
            source.write_bytes(b'')
            with pytest.raises(HTTPException) as error:
                await upload.read(chunk_size)
            assert error.value.status_code == 409 and 'copied completely' in error.value.detail
    asyncio.run(run())


def test_stale_smaller_descriptor_cannot_bypass_actual_upload_limit(drive, tmp_path):
    from routes.h3_video_routes import _store_uploads
    source = make_file(drive, content=b'a')
    selected = descriptor(source, drive)
    source.write_bytes(b'z' * 2048)
    stage = tmp_path / 'job'
    stage.mkdir()
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [selected]}), {}, VIDEO_FIELDS) as uploads:
            await _store_uploads(stage, uploads, 1024)
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 413


@pytest.mark.parametrize('change', [
    lambda item: item.update(index=1), lambda item: item.update(index=True),
    lambda item: item.update(size=True), lambda item: item.update(mtime_ns=123),
    lambda item: item.update(mtime_ns='00123'), lambda item: item.update(path='../clip.mp4'),
    lambda item: item.update(path='/etc/secrets.mp4'), lambda item: item.update(extra='value'),
])
def test_submission_descriptor_is_strict(drive, change):
    item = descriptor(make_file(drive), drive)
    change(item)
    async def run():
        async with media.server_video_inputs(inputs({'source_video': [item]}), {}, VIDEO_FIELDS):
            pytest.fail('Invalid descriptor accepted')
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 400


def test_selection_limits_duplicate_fields_and_wrong_media_roles(drive):
    item = descriptor(make_file(drive), drive)
    forms = [
        inputs({'first_frame': [item]}), inputs({'unexpected': [item]}),
        inputs({'reference_videos': [item, item]}),
        inputs({'reference_videos': [dict(item, index=i) for i in range(18)]}),
        FormData([('server_inputs', '{}'), ('server_inputs', '{}')]),
        FormData({'server_inputs': '{"source_video":[],"source_video":[]}'}),
        FormData({'server_inputs': '['}), FormData({'server_inputs': ' ' * 65537}),
    ]
    async def run(form):
        async with media.server_video_inputs(form, {}, VIDEO_FIELDS):
            pytest.fail('Invalid selection accepted')
    for form in forms:
        with pytest.raises(HTTPException) as error:
            asyncio.run(run(form))
        assert error.value.status_code == 400


def test_later_open_failure_closes_all_previous_handles(drive, monkeypatch):
    first = make_file(drive, 'first.mp4')
    second = make_file(drive, 'second.mp4')
    values = [descriptor(first, drive), descriptor(second, drive, 1)]
    second.unlink()
    opened = []
    original = media._open_selection
    def record(*args):
        upload = original(*args)
        opened.append(upload)
        return upload
    monkeypatch.setattr(media, '_open_selection', record)
    async def run():
        async with media.server_video_inputs(inputs({'reference_videos': values}), {}, VIDEO_FIELDS):
            pytest.fail('Missing file accepted')
    with pytest.raises(HTTPException):
        asyncio.run(run())
    assert len(opened) == 1 and opened[0].file.closed


def test_cancelled_acquisition_closes_late_handles(drive, monkeypatch):
    source = make_file(drive)
    started, proceed = threading.Event(), threading.Event()
    original, opened = media._acquire_inputs, []
    def delayed(*args):
        result = original(*args)
        opened.extend(result[1])
        started.set()
        assert proceed.wait(3)
        return result
    monkeypatch.setattr(media, '_acquire_inputs', delayed)
    async def acquire():
        async with media.server_video_inputs(inputs({'source_video': [descriptor(source, drive)]}), {}, VIDEO_FIELDS):
            pytest.fail('Cancelled acquisition yielded')
    async def run():
        task = asyncio.create_task(acquire())
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert opened and all(upload.file.closed for upload in opened)
