"""Admin-only, owner-scoped local MiniMax H3 video generation."""

import asyncio
import json
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from core.middleware import require_admin
from src.auth_helpers import storage_owner_for_request
from src.h3_video import H3JobManager, UPLOAD_EXTENSIONS, validate_config, gpu_telemetry
from src.h3_prompt_enhancement import (
    EnhancementError, IMAGE_FIELDS, enhance_prompt, enhancer_status,
    prepare_prompt_images, validate_request as validate_prompt_request,
)
from src.video_submission import submission, submission_key, launch_job, store_upload_chunk, ensure_upload_space, known_input_bytes, UploadSpaceError
from src.video_server_files import server_video_inputs
from src.upload_limits import get_chat_upload_max_bytes, format_byte_limit


def _owner(request):
    require_admin(request)
    owner = storage_owner_for_request(request)
    if not owner:
        raise HTTPException(403, "Sign in to create or view video jobs")
    return owner


def _limited_request(request, limit, limit_message=None, *, space_directory=None):
    """Cap multipart bytes before Starlette spools any unbounded upload."""
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > limit:
                raise HTTPException(413, limit_message or f"Video inputs exceed {format_byte_limit(limit - 65536)} per job")
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length")
    total = 0

    async def receive():
        nonlocal total
        message = await request.receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > limit:
                raise HTTPException(413, limit_message or f"Video inputs exceed {format_byte_limit(limit - 65536)} per job")
            if space_directory is not None:
                # Multipart spools may reach disk before _store_uploads runs.
                await asyncio.to_thread(ensure_upload_space, space_directory, len(message.get("body", b"")))
        return message

    return Request(request.scope, receive=receive)


async def _store_uploads(directory, uploads, limit):
    saved = {}
    total = 0
    for field, files in uploads.items():
        paths = []
        for upload in files:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in UPLOAD_EXTENSIONS[field]:
                raise HTTPException(400, f"Unsupported file type for {field.replace('_', ' ')}")
            path = directory / (uuid.uuid4().hex + suffix)
            with path.open("xb") as stream:
                path.chmod(0o600)
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise HTTPException(413, f"Video inputs exceed {format_byte_limit(limit)} per job")
                    await store_upload_chunk(stream, directory, chunk)
            if path.stat().st_size == 0:
                raise HTTPException(400, "An attachment is empty")
            if field in {"first_frame", "last_frame", "reference_images"}:
                try:
                    from PIL import Image
                    with Image.open(path) as image:
                        if image.width * image.height > 64 * 1024 * 1024:
                            raise ValueError("Image exceeds 64 megapixels")
                        image.verify()
                except Exception as exc:
                    raise HTTPException(400, "Invalid or excessively large reference image") from exc
            paths.append(str(path.resolve()))
        saved[field] = paths[0] if field in {"first_frame", "last_frame", "source_video"} and paths else paths
    return saved


async def _enhance_until_disconnected(request, owner, config, images=None):
    """Closing/cancelling the editor closes its in-flight upstream request."""
    async def wait_for_disconnect():
        # The request body is consumed before this task starts. Await the ASGI
        # disconnect directly: polling is_disconnected() creates a cancelled
        # AnyIO scope that can swallow task cancellation on early validation.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    rewrite = asyncio.create_task(enhance_prompt(owner, config, images=images))
    disconnected = asyncio.create_task(wait_for_disconnect())
    try:
        await asyncio.wait({rewrite, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if rewrite.done():
            return await rewrite
        raise HTTPException(499, "Prompt enhancement cancelled")
    finally:
        for task in (rewrite, disconnected):
            if not task.done():
                task.cancel()
        await asyncio.gather(rewrite, disconnected, return_exceptions=True)


def setup_h3_video_routes(manager=None):
    router = APIRouter(prefix="/api/video/h3", tags=["video"])
    manager = manager or H3JobManager()

    @router.get("/gpus")
    async def gpus(request: Request):
        _owner(request)
        return await asyncio.to_thread(gpu_telemetry)

    @router.get("/prompt-enhancer")
    async def prompt_enhancer(request: Request):
        return await enhancer_status(_owner(request))

    @router.post("/enhance-prompt")
    async def rewrite_prompt(request: Request):
        owner = _owner(request)
        try:
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "multipart/form-data":
                limit = get_chat_upload_max_bytes()
                limited = _limited_request(request, limit + 96 * 1024,
                                           f"Prompt images exceed {format_byte_limit(limit)} per request")
                async with limited.form(max_files=9, max_fields=1, max_part_size=96 * 1024) as form:
                    if any(key not in {"config", *IMAGE_FIELDS} for key in form):
                        raise HTTPException(400, "Only first frame, last frame and reference images can be inspected by the prompt enhancer")
                    raw = form.get("config")
                    if not isinstance(raw, str) or len(raw) > 96 * 1024:
                        raise HTTPException(400, "Missing prompt enhancement config JSON")
                    config = validate_prompt_request(json.loads(raw))
                    uploads = {key: form.getlist(key) for key in IMAGE_FIELDS}
                    if any(not isinstance(value, UploadFile) for files in uploads.values() for value in files):
                        raise HTTPException(400, "Images must be uploaded files")
                    images = await prepare_prompt_images(config, uploads, limit)
                # Exiting form closes all temporary multipart spools. Only
                # validated image data remains in memory for this request.
                return await _enhance_until_disconnected(request, owner, config, images)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 96 * 1024:
                    raise HTTPException(413, "Prompt enhancement request is too large")
            config = json.loads(body)
            return await _enhance_until_disconnected(request, owner, config)
        except ValueError as exc:
            raise HTTPException(400, str(exc) if not isinstance(exc, json.JSONDecodeError)
                                else "Invalid prompt enhancement JSON") from exc
        except EnhancementError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.get("/inventory")
    async def inventory(request: Request):
        _owner(request)
        return await asyncio.to_thread(manager.inventory)

    @router.get("/jobs")
    async def jobs(request: Request):
        owner = _owner(request)
        from src.video_queue_control import queue_status
        return {"jobs": await asyncio.to_thread(manager.jobs, owner),
                "queue": await asyncio.to_thread(queue_status, manager, owner)}

    @router.post("/jobs", status_code=201)
    async def create_job(request: Request):
        owner = _owner(request)
        limit = get_chat_upload_max_bytes()
        try:
            async with submission(manager, owner, "h3", submission_key(request)) as pending:
                existing = await asyncio.to_thread(pending.existing)
                if existing is not None:
                    return existing
                limited = _limited_request(request, limit + 2 * 65536, space_directory=manager.root)
                async with limited.form(max_files=17, max_fields=3, max_part_size=65536) as form:
                    if any(key not in {"config", "auto_video_length", "server_inputs", *UPLOAD_EXTENSIONS} for key in form):
                        raise HTTPException(400, "Unknown video upload field")
                    auto_lengths = form.getlist("auto_video_length")
                    if len(auto_lengths) > 1 or (auto_lengths and auto_lengths[0] not in {"true", "false"}):
                        raise HTTPException(400, "auto_video_length must be true or false")
                    auto_length = auto_lengths == ["true"]
                    raw_config = form.get("config")
                    if not isinstance(raw_config, str) or len(raw_config) > 65536:
                        raise HTTPException(400, "Missing video config JSON")
                    try:
                        raw_config = json.loads(raw_config)
                    except ValueError as exc:
                        raise HTTPException(400, "Invalid video config JSON") from exc
                    uploads = {key: form.getlist(key) for key in UPLOAD_EXTENSIONS if key in form}
                    if any(not isinstance(value, UploadFile) for values in uploads.values() for value in values):
                        raise HTTPException(400, "Attachments must be uploaded files")
                    async with server_video_inputs(form, uploads, UPLOAD_EXTENSIONS) as uploads:
                        current = await asyncio.to_thread(manager.submission_inventory)
                        config = validate_config(raw_config, current["components"], current["gpus"], uploads)
                        from src.h3_vfx import normalize_vfx_inputs
                        uploads = normalize_vfx_inputs(config, uploads)
                        uploads.setdefault("source_video", [])
                        if not current["runtime_ready"]:
                            raise HTTPException(503, current["runtime_error"])
                        if known_input_bytes(uploads) > limit:
                            raise HTTPException(413, f"Video inputs exceed {format_byte_limit(limit)} per job")
                        directory = pending.stage()
                        pending.save_metadata(uploads, "h3")
                        saved = await _store_uploads(directory, uploads, limit)
                        if auto_length:
                            from src.h3_video_length import select_batch_video_length
                            from src.h3_video import _read, _write
                            config, length = await asyncio.to_thread(select_batch_video_length, config, saved)
                            metadata_path = directory / "submission.json"
                            _write(metadata_path, {**_read(metadata_path), "batch_video_length": length})
                        return await launch_job(manager, directory, owner, config, saved)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete("/jobs/{job_id}")
    async def cancel_job(job_id: str, request: Request):
        owner = _owner(request)
        try:
            return await asyncio.to_thread(manager.cancel, job_id, owner)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Video job not found") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete("/jobs/{job_id}/record")
    async def delete_job(job_id: str, request: Request):
        owner = _owner(request)
        try:
            return await asyncio.to_thread(manager.delete, job_id, owner)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Video job not found") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/jobs/{job_id}/video")
    async def video(job_id: str, request: Request):
        owner = _owner(request)
        try:
            path, filename = await asyncio.to_thread(manager.video_path, job_id, owner)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Completed video not found") from exc
        return FileResponse(path, media_type="video/mp4", filename=filename, content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    from routes.video_workflow_routes import add_workflow_routes
    add_workflow_routes(router, manager, family='h3')
    from routes.video_server_file_routes import add_server_video_file_routes
    add_server_video_file_routes(router, owner_callback=_owner)
    from routes.video_job_edit_routes import add_job_edit_routes
    add_job_edit_routes(router, manager, 'h3', UPLOAD_EXTENSIONS, _store_uploads)
    from routes.video_queue_routes import add_queue_routes
    add_queue_routes(router, manager, 'h3')
    return router
