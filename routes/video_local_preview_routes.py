"""Disposable compatible video previews for files selected on a client device."""
import asyncio
import errno
from functools import partial
import math

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from src.local_video_previews import LocalVideoPreviewStore
from src.upload_limits import get_video_upload_max_bytes
from src.video_submission import UploadSpaceError, ensure_upload_space
from routes.video_server_file_routes import _byte_range, _VideoPreviewResponse


def add_local_video_preview_routes(router, owner_callback, root):
    store = LocalVideoPreviewStore(root)

    @router.post('/local-previews')
    async def upload_local_preview(request: Request):
        owner = owner_callback(request)
        from routes.h3_video_routes import _limited_request
        limit = get_video_upload_max_bytes()
        try:
            limited = _limited_request(request, None if limit is None else limit + 65536,
                                       'The video exceeds the configured video upload limit', space_directory=store.root)
            async with limited.form(max_files=1, max_fields=0) as form:
                items = list(form.multi_items())
                if len(items) != 1 or items[0][0] != 'video' or not isinstance(items[0][1], UploadFile):
                    raise HTTPException(400, 'Upload exactly one video file in the video field')
                async with store.upload(owner, items[0][1], limit) as value:
                    return JSONResponse(value, status_code=201, headers={'Cache-Control': 'private, no-store'})
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise HTTPException(507, 'Not enough disk space to prepare this video preview') from exc
            raise HTTPException(503, 'Temporary video storage is unavailable') from exc

    @router.api_route('/local-previews/{identity}/video', methods=['GET', 'HEAD'])
    async def play_local_preview(request: Request, identity: str, start_seconds: float = 0,
                                 duration_seconds: float = 30):
        owner = owner_callback(request)
        if not math.isfinite(start_seconds) or start_seconds < 0 or not math.isfinite(duration_seconds) or not .25 <= duration_seconds <= 30:
            raise HTTPException(400, 'Choose a preview start at zero or later and a length up to 30 seconds')
        from src import video_preview_proxy as proxy
        await anyio.to_thread.run_sync(store.check, identity, owner)
        try:
            # The converter separately caps each result. Reserve its maximum
            # temporary output here so preview generation leaves room for jobs.
            await anyio.to_thread.run_sync(ensure_upload_space, proxy.CACHE_ROOT, proxy.MAX_CACHE_BYTES + 1024 * 1024)
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc
        upload = await anyio.to_thread.run_sync(partial(proxy.open_owned_video_proxy,
            lambda: store.open(identity, owner), {'root': 'local-preview', 'path': identity},
            start_seconds=start_seconds, duration_seconds=duration_seconds))
        try:
            await anyio.to_thread.run_sync(store.check, identity, owner)
            value = None if request.headers.get('if-range') else request.headers.get('range')
            start, end, status = _byte_range(value, upload.size)
            response = _VideoPreviewResponse(upload, start=start, end=end, status_code=status,
                                             head=request.method == 'HEAD')
            response.headers['X-Preview-Start-Seconds'] = str(start_seconds)
            return response
        except BaseException:
            await asyncio.shield(upload.close())
            raise

    @router.delete('/local-previews/{identity}')
    async def delete_local_preview(request: Request, identity: str):
        return await anyio.to_thread.run_sync(store.delete, identity, owner_callback(request))
