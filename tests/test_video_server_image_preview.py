"""Real thumbnail decoding through the authenticated, rooted browser endpoint."""
import io
import json

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from routes.video_server_file_routes import add_server_video_file_routes
from src import video_server_image_preview as previews


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv('ODYSSEUS_VIDEO_BROWSE_ROOTS', json.dumps({'f': str(tmp_path)}))
    previews._CACHE.clear()
    app = FastAPI()
    def owner(request):
        if request.headers.get('x-user') != 'owner':
            raise HTTPException(403, 'Authentication required')
        return 'owner'
    for family in ('h3', 'bfs'):
        router = APIRouter(prefix=f'/api/video/{family}')
        add_server_video_file_routes(router, owner)
        app.include_router(router)
    with TestClient(app) as client:
        yield client, tmp_path


def image_file(root, name='picture + space.png', *, mode='RGB', size=(1600, 800), color='red', **save):
    path = root / name
    Image.new(mode, size, color).save(path, **save)
    return path


def get(browser, source, *, family='h3', user='owner', **params):
    info = source.stat()
    return browser[0].get(f'/api/video/{family}/server-files/image', headers={'x-user': user},
                         params={'root': 'f', 'path': source.name, 'size': info.st_size,
                                 'mtime_ns': str(info.st_mtime_ns), **params})


@pytest.mark.parametrize('family', ['h3', 'bfs'])
@pytest.mark.parametrize('detail,expected', [(False, (256, 128)), (True, (1280, 640))])
def test_bounded_preview_and_detail_preserve_original(browser, family, detail, expected):
    path = image_file(browser[1])
    original = path.read_bytes()
    response = get(browser, path, family=family, detail=str(detail).lower())
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'image/jpeg'
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    with Image.open(io.BytesIO(response.content)) as result:
        assert result.size == expected
        assert result.mode == 'RGB'
    assert path.read_bytes() == original


@pytest.mark.parametrize('extension', ['jpg', 'jpeg', 'png', 'webp'])
def test_supported_formats_and_small_images_do_not_upscale(browser, extension):
    path = image_file(browser[1], name='small.' + extension, size=(42, 24))
    response = get(browser, path)
    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.content)).size == (42, 24)


def test_exif_rotation_and_metadata_removed(browser):
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = 'Private source metadata'
    path = image_file(browser[1], name='rotated.jpg', exif=exif)
    image = Image.open(io.BytesIO(get(browser, path).content))
    assert image.size == (128, 256)
    assert not image.getexif()


def test_transparency_is_flattened(browser):
    path = image_file(browser[1], mode='RGBA', color=(255, 0, 0, 0))
    image = Image.open(io.BytesIO(get(browser, path).content))
    assert image.getpixel((0, 0)) == (255, 255, 255)


def test_authorization_happens_before_open_even_for_cached_images(browser, monkeypatch):
    path = image_file(browser[1])
    assert get(browser, path).status_code == 200
    def forbidden(*args, **kwargs):
        pytest.fail('Unauthorized request tried to open a file')
    monkeypatch.setattr(previews, 'open_server_image_preview', forbidden)
    assert get(browser, path, user='reader').status_code == 403


@pytest.mark.parametrize('relative', ['../picture.png', '/etc/picture.png', 'picture.mp4'])
def test_cannot_escape_roots_or_decode_other_file_types(browser, relative):
    path = image_file(browser[1])
    assert get(browser, path, path=relative).status_code == 400


def test_cache_reopens_validated_handle_and_updates_changed_file(browser, monkeypatch):
    path = image_file(browser[1])
    first = get(browser, path).content
    with monkeypatch.context() as patch:
        patch.setattr(previews, '_render', lambda *_: pytest.fail('Cache should avoid decoding'))
        assert get(browser, path).content == first
        assert get(browser, path, size=1, mtime_ns='1').content == first
    image_file(browser[1], color='blue')
    assert get(browser, path).content != first
    target = browser[1] / 'target.png'
    path.rename(target)
    path.symlink_to(target)
    assert get(browser, path).status_code == 400


def test_decode_failure_and_limits_close_handles_without_breaking_selection(browser, monkeypatch):
    path = image_file(browser[1])
    handles = []
    original = previews.open_server_image_preview
    def record(selection):
        upload = original(selection)
        handles.append(upload.file)
        return upload
    monkeypatch.setattr(previews, 'open_server_image_preview', record)
    monkeypatch.setattr(previews, 'SERVER_IMAGE_PREVIEW_MAX_BYTES', 1)
    assert get(browser, path).status_code == 413
    monkeypatch.setattr(previews, 'SERVER_IMAGE_PREVIEW_MAX_BYTES', 64 * 1024 * 1024)
    monkeypatch.setattr(previews, 'SERVER_IMAGE_PREVIEW_MAX_PIXELS', 1)
    assert get(browser, path).status_code == 413
    path.write_bytes(b'corrupt image')
    assert get(browser, path).status_code == 422
    assert all(file.closed for file in handles)
    listing = browser[0].get('/api/video/h3/server-files', params={'root': 'f', 'kind': 'image'}, headers={'x-user': 'owner'})
    assert listing.status_code == 200 and listing.json()['entries'][0]['selection']['path'] == path.name


def test_cache_is_bounded(browser, monkeypatch):
    monkeypatch.setattr(previews, 'SERVER_IMAGE_PREVIEW_CACHE_BYTES', 1)
    assert get(browser, image_file(browser[1])).status_code == 200
    assert not previews._CACHE
