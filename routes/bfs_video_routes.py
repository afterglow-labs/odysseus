"""Authenticated BFS uploads and owner-scoped render jobs."""
import asyncio
import json
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from routes.h3_video_routes import _owner, _limited_request
from src.bfs_video import BFSJobManager, UPLOAD_EXTENSIONS, validate_config
from src.video_submission import submission, submission_key, launch_job, store_upload_chunk, known_input_bytes, UploadSpaceError
from src.video_server_files import server_video_inputs
from src.upload_limits import get_chat_upload_max_bytes, format_byte_limit


async def store_uploads(directory, uploads, limit):
    saved, total = {}, 0
    for key, values in uploads.items():
        if not values:
            continue
        upload = values[0]
        extension = Path(upload.filename or '').suffix.lower()
        if extension not in UPLOAD_EXTENSIONS[key]:
            raise ValueError('Unsupported file type for ' + key.replace('_', ' '))
        path = directory / (uuid.uuid4().hex + extension)
        with path.open('xb') as stream:
            path.chmod(0o600)
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, f'BFS inputs exceed {format_byte_limit(limit)} per job')
                await store_upload_chunk(stream, directory, chunk)
        if not path.stat().st_size:
            raise ValueError('An uploaded file is empty')
        if key in {'identity_image', 'last_frame'}:
            from PIL import Image
            try:
                with Image.open(path) as image:
                    if image.width * image.height > 64 * 1024 * 1024 or getattr(image, 'n_frames', 1) != 1:
                        raise ValueError('Invalid image dimensions')
                    image.verify()
            except Exception as exc:
                raise ValueError('Use a valid still image of at most 64 megapixels') from exc
        saved[key] = str(path.resolve())
    return saved


def setup_bfs_video_routes(manager=None):
    manager = manager or BFSJobManager()
    router = APIRouter(prefix='/api/video/bfs', tags=['video'])

    @router.get('/inventory')
    async def inventory(request: Request):
        _owner(request)
        return await asyncio.to_thread(manager.inventory)

    @router.get('/jobs')
    async def jobs(request: Request):
        from src.video_queue_control import queue_status
        owner = _owner(request)
        return {'jobs': await asyncio.to_thread(manager.jobs, owner),
                'queue': await asyncio.to_thread(queue_status, manager, owner)}

    @router.post('/jobs', status_code=201)
    async def create(request: Request):
        owner = _owner(request)
        try:
            async with submission(manager, owner, "bfs", submission_key(request)) as pending:
                existing = await asyncio.to_thread(pending.existing)
                if existing is not None:
                    return existing
                limit = get_chat_upload_max_bytes()
                limited = _limited_request(request, limit + 2 * 65536, f'BFS inputs exceed {format_byte_limit(limit)} per job',
                                           space_directory=manager.root)
                async with limited.form(max_files=4, max_fields=2, max_part_size=65536) as form:
                    if any(key not in {'config', 'server_inputs', *UPLOAD_EXTENSIONS} for key in form):
                        raise ValueError('Unknown BFS upload field')
                    raw = form.get('config')
                    if not isinstance(raw, str):
                        raise ValueError('Missing BFS config JSON')
                    uploads = {key: form.getlist(key) for key in UPLOAD_EXTENSIONS}
                    if any(not isinstance(file, UploadFile) for values in uploads.values() for file in values):
                        raise ValueError('BFS media must be uploaded files')
                    async with server_video_inputs(form, uploads, UPLOAD_EXTENSIONS) as uploads:
                        current = await asyncio.to_thread(manager.submission_inventory)
                        config = validate_config(json.loads(raw), current, uploads)
                        if known_input_bytes(uploads) > limit:
                            raise HTTPException(413, f'BFS inputs exceed {format_byte_limit(limit)} per job')
                        directory = pending.stage()
                        pending.save_metadata(uploads, "bfs")
                        saved = await store_uploads(directory, uploads, limit)
                        return await launch_job(manager, directory, owner, config, saved)
        except ValueError as exc:
            raise HTTPException(400, 'Invalid BFS config JSON' if isinstance(exc, json.JSONDecodeError) else str(exc)) from exc
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete('/jobs/{job_id}')
    async def cancel(job_id: str, request: Request):
        try:
            return await asyncio.to_thread(manager.cancel, job_id, _owner(request))
        except FileNotFoundError as exc:
            raise HTTPException(404, 'Video job not found') from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete('/jobs/{job_id}/record')
    async def delete_job(job_id: str, request: Request):
        try:
            return await asyncio.to_thread(manager.delete, job_id, _owner(request))
        except FileNotFoundError as exc:
            raise HTTPException(404, 'Video job not found') from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get('/jobs/{job_id}/video')
    async def video(job_id: str, request: Request):
        try:
            path, filename = await asyncio.to_thread(manager.video_path, job_id, _owner(request))
        except FileNotFoundError as exc:
            raise HTTPException(404, 'Completed video not found') from exc
        return FileResponse(path, media_type='video/mp4', filename=filename, content_disposition_type='inline',
                            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
    from routes.video_workflow_routes import add_workflow_routes
    add_workflow_routes(router, manager, family='bfs')
    from routes.video_server_file_routes import add_server_video_file_routes
    add_server_video_file_routes(router, owner_callback=_owner)
    from routes.video_job_edit_routes import add_job_edit_routes
    add_job_edit_routes(router, manager, 'bfs', UPLOAD_EXTENSIONS, store_uploads)
    from routes.video_queue_routes import add_queue_routes
    add_queue_routes(router, manager, 'bfs')
    return router
