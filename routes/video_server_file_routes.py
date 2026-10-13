"""Authenticated server video browsing shared by H3 and BFS clients."""
import asyncio
from pathlib import Path
import re

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.video_server_files import list_server_video_roots, list_server_videos, open_server_video_preview


def _byte_range(value, size):
    if not value:
        return 0, size - 1, 200
    match = re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})', value.strip())
    if not match or not any(match.groups()):
        raise HTTPException(416, 'Unsupported video byte range', headers={'Content-Range': f'bytes */{size}'})
    first, last = match.groups()
    if not first:
        length = int(last)
        start, end = max(0, size - length), size - 1
        if length == 0:
            start = size
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise HTTPException(416, 'Video byte range is outside the file', headers={'Content-Range': f'bytes */{size}'})
    return start, end, 206


class _VideoPreviewResponse(StreamingResponse):
    """Keep the validated file handle, including while seeking and streaming."""
    def __init__(self, upload, *, start, end, status_code, head=False):
        self.upload = upload
        async def chunks():
            if head:
                return
            await upload.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = await upload.read(min(remaining, 1024 * 1024))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk
        headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'private, no-store',
                   'Content-Length': str(end - start + 1), 'X-Content-Type-Options': 'nosniff'}
        if status_code == 206:
            headers['Content-Range'] = f'bytes {start}-{end}/{upload.size}'
        media_type = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
                      '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo'}[
                          Path(upload.filename).suffix.lower()]
        super().__init__(chunks(), status_code=status_code, headers=headers, media_type=media_type)

    async def __call__(self, scope, receive, send):
        try:
            await anyio.to_thread.run_sync(self.upload.check_unchanged)
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(self.upload.close())


def add_server_video_file_routes(router, owner_callback):
    @router.api_route('/server-files/video', methods=['GET', 'HEAD'])
    async def preview_server_video(request: Request, root: str, path: str, size: int, mtime_ns: str):
        owner_callback(request)
        upload = await anyio.to_thread.run_sync(open_server_video_preview, {
            'root': root, 'path': path, 'size': size, 'mtime_ns': mtime_ns})
        try:
            # Without an entity validator, If-Range requests receive the full
            # current file. The descriptor independently rejects changed files.
            value = None if request.headers.get('if-range') else request.headers.get('range')
            start, end, status = _byte_range(value, upload.size)
            return _VideoPreviewResponse(upload, start=start, end=end, status_code=status,
                                         head=request.method == 'HEAD')
        except BaseException:
            await asyncio.shield(upload.close())
            raise

    @router.get('/server-file-roots')
    async def server_file_roots(request: Request):
        owner_callback(request)
        listing = await asyncio.to_thread(list_server_video_roots)
        return JSONResponse(listing, headers={'Cache-Control': 'private, no-store'})

    @router.get('/server-files')
    async def server_files(request: Request, root: str = 'e', path: str = '',
                           offset: int = 0, limit: int = 200, search: str = ''):
        owner_callback(request)
        listing = await asyncio.to_thread(list_server_videos, root=root, path=path,
                                          offset=offset, limit=limit, search=search)
        return JSONResponse(listing, headers={'Cache-Control': 'private, no-store'})
