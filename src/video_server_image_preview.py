"""Small image previews from authorized server handles, without GPU work."""
from collections import OrderedDict
import io
import threading
import time

from fastapi import HTTPException

from src.constants import (
    SERVER_IMAGE_THUMBNAIL_EDGE, SERVER_IMAGE_PREVIEW_EDGE,
    SERVER_IMAGE_PREVIEW_MAX_BYTES, SERVER_IMAGE_PREVIEW_MAX_PIXELS,
    SERVER_IMAGE_PREVIEW_CACHE_BYTES, SERVER_IMAGE_PREVIEW_CACHE_TTL,
    SERVER_IMAGE_PREVIEW_CONCURRENCY,
)
from src.video_server_files import open_server_image_preview

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_DECODERS = threading.BoundedSemaphore(SERVER_IMAGE_PREVIEW_CONCURRENCY)


def _render(upload, edge):
    from PIL import Image, ImageOps, UnidentifiedImageError

    if upload.size > SERVER_IMAGE_PREVIEW_MAX_BYTES:
        raise HTTPException(413, 'This image is too large to preview. You can still select it.')
    try:
        with Image.open(upload.file, formats=['JPEG', 'PNG', 'WEBP']) as image:
            if image.width * image.height > SERVER_IMAGE_PREVIEW_MAX_PIXELS:
                raise HTTPException(413, 'This image is too large to preview. You can still select it.')
            image.draft('RGB', (edge, edge))
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            oriented = ImageOps.exif_transpose(image)
            # Flatten transparency and omit source EXIF/GPS/text metadata.
            rgba = oriented.convert('RGBA')
            output = Image.new('RGB', rgba.size, 'white')
            output.paste(rgba, mask=rgba.getchannel('A'))
            data = io.BytesIO()
            output.save(data, format='JPEG', quality=82)
            upload.check_unchanged()
            return data.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, 'This image could not be previewed. You can still select it.') from exc


def server_image_preview(selection, *, detail=False):
    # Open and authorize on every request, even on cache hits. The actual open
    # handle identifies the cache entry; old directory listing timestamps do not.
    upload = open_server_image_preview(selection)
    edge = SERVER_IMAGE_PREVIEW_EDGE if detail else SERVER_IMAGE_THUMBNAIL_EDGE
    key = (selection['root'], selection['path'], *upload._source_signature, edge)
    try:
        upload.check_unchanged()
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > time.monotonic():
                _CACHE.move_to_end(key)
                return cached[1]
        if not _DECODERS.acquire(timeout=5):
            raise HTTPException(429, 'Image previews are busy. Try again.', headers={'Retry-After': '1'})
        try:
            data = _render(upload, edge)
        finally:
            _DECODERS.release()
        with _LOCK:
            now = time.monotonic()
            for expired in [k for k, (until, _) in _CACHE.items() if until <= now]:
                del _CACHE[expired]
            _CACHE[key] = (now + SERVER_IMAGE_PREVIEW_CACHE_TTL, data)
            _CACHE.move_to_end(key)
            size = sum(len(value[1]) for value in _CACHE.values())
            while size > SERVER_IMAGE_PREVIEW_CACHE_BYTES:
                _, (_, evicted) = _CACHE.popitem(last=False)
                size -= len(evicted)
        return data
    finally:
        upload.file.close()
