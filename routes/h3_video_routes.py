"""Admin-only, owner-scoped local MiniMax H3 video generation."""

import asyncio
import json
from pathlib import Path
import shutil
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from core.middleware import require_admin
from src.auth_helpers import storage_owner_for_request
from src.h3_video import H3JobManager, UPLOAD_EXTENSIONS, validate_config
from src.upload_limits import get_chat_upload_max_bytes, format_byte_limit


def _owner(request):
    require_admin(request)
    owner = storage_owner_for_request(request)
    if not owner:
        raise HTTPException(403, "Sign in to create or view video jobs")
    return owner


def _limited_request(request, limit):
    """Cap multipart bytes before Starlette spools any unbounded upload."""
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > limit:
                raise HTTPException(413, f"Video inputs exceed {format_byte_limit(limit - 65536)} per job")
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length")
    total = 0

    async def receive():
        nonlocal total
        message = await request.receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            if total > limit:
                raise HTTPException(413, f"Video inputs exceed {format_byte_limit(limit - 65536)} per job")
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
                    stream.write(chunk)
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
        saved[field] = paths[0] if field in {"first_frame", "last_frame"} and paths else paths
    return saved


def setup_h3_video_routes(manager=None):
    router = APIRouter(prefix="/api/video/h3", tags=["video"])
    manager = manager or H3JobManager()

    @router.get("/inventory")
    async def inventory(request: Request):
        _owner(request)
        return await asyncio.to_thread(manager.inventory)

    @router.get("/jobs")
    async def jobs(request: Request):
        owner = _owner(request)
        return {"jobs": await asyncio.to_thread(manager.jobs, owner)}

    @router.post("/jobs", status_code=201)
    async def create_job(request: Request):
        owner = _owner(request)
        limit = get_chat_upload_max_bytes()
        directory = None
        try:
            limited = _limited_request(request, limit + 65536)
            async with limited.form(max_files=17, max_fields=1, max_part_size=65536) as form:
                if any(key not in {"config", *UPLOAD_EXTENSIONS} for key in form):
                    raise HTTPException(400, "Unknown video upload field")
                raw_config = form.get("config")
                if not isinstance(raw_config, str) or len(raw_config) > 65536:
                    raise HTTPException(400, "Missing video config JSON")
                try:
                    raw_config = json.loads(raw_config)
                except ValueError as exc:
                    raise HTTPException(400, "Invalid video config JSON") from exc
                uploads = {key: form.getlist(key) for key in UPLOAD_EXTENSIONS}
                if any(not isinstance(value, UploadFile) for values in uploads.values() for value in values):
                    raise HTTPException(400, "Attachments must be uploaded files")
                current = await asyncio.to_thread(manager.inventory)
                config = validate_config(raw_config, current["components"], current["gpus"], uploads)
                if not current["runtime_ready"]:
                    raise HTTPException(503, current["runtime_error"])
                directory = manager.stage()
                saved = await _store_uploads(directory, uploads, limit)
                launch = asyncio.create_task(asyncio.to_thread(manager.launch, directory, owner, config, saved))
                try:
                    return await asyncio.shield(launch)
                except asyncio.CancelledError:
                    # Let the short spawn operation publish its receipt before
                    # this request's cleanup can remove the staging directory.
                    await launch
                    raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            # A running/scheduled job owns its directory even if the HTTP client
            # disconnected just after launch. Delete only unlaunched staging.
            if directory is not None and not (directory / "state.json").exists():
                shutil.rmtree(directory, ignore_errors=True)

    @router.delete("/jobs/{job_id}")
    async def cancel_job(job_id: str, request: Request):
        owner = _owner(request)
        try:
            return await asyncio.to_thread(manager.cancel, job_id, owner)
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

    return router
