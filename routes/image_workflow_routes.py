"""Native image recipes; the server owns input files and component paths."""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from core.middleware import require_admin
from src.auth_helpers import storage_owner_for_request
from src.image_workflows import ImageJobManager, validate_config, validate_inputs
from routes.h3_video_routes import _limited_request

MAX_INPUT_BYTES = 64 * 1024 * 1024


def _owner(request):
    require_admin(request)
    owner = storage_owner_for_request(request)
    if not owner:
        raise HTTPException(403, "Sign in to use image workflows")
    return owner


def setup_image_workflow_routes(manager=None):
    manager = manager or ImageJobManager()
    router = APIRouter(prefix="/api/image-workflows", tags=["images"])

    @router.get("/inventory")
    async def inventory(request: Request):
        _owner(request)
        return await asyncio.to_thread(manager.inventory)

    @router.get("/jobs")
    async def jobs(request: Request):
        return {"jobs": await asyncio.to_thread(manager.jobs, _owner(request))}

    @router.post("/jobs")
    async def create(request: Request):
        owner = _owner(request)
        form = await _limited_request(request, MAX_INPUT_BYTES + 65536, "Image inputs exceed 64 MB per job").form(max_files=5, max_fields=5)
        directory = None
        try:
            config = json.loads(str(form.get("config", "{}")))
            # Syntax validation precedes filesystem work. The manager selects
            # and verifies the real default GPU immediately before submission.
            checked = validate_config(config, "0")
            images = form.getlist("images")
            masks = form.getlist("mask")
            if len(masks) > 1 or any(not isinstance(f, UploadFile) for f in images + masks):
                raise ValueError("Invalid image attachments")
            validate_inputs(checked, images, masks[0] if masks else None)
            request_id = request.headers.get("X-Odysseus-Submission-Id") or str(form.get("request_id", ""))
            uuid.UUID(request_id)
            manager.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = Path(tempfile.mkdtemp(prefix=".upload-", dir=manager.root))
            paths, mask_path, total = [], None, 0
            for upload in images + masks:
                path = directory / (uuid.uuid4().hex + ".png")
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    while chunk := await upload.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_INPUT_BYTES:
                            raise HTTPException(413, "Image inputs exceed 64 MB per job")
                        await asyncio.to_thread(stream.write, chunk)
                def verify():
                    from PIL import Image
                    try:
                        with Image.open(path) as image:
                            if image.width * image.height > 32 * 1024 * 1024:
                                raise ValueError("Source image exceeds 32 megapixels")
                            image.verify()
                    except Image.DecompressionBombError as exc:
                        raise ValueError("Source image exceeds 32 megapixels") from exc
                await asyncio.to_thread(verify)
                if upload in masks:
                    mask_path = str(path)
                else:
                    paths.append(str(path))
            return await asyncio.to_thread(manager.submit, owner, config, directory, paths, mask_path, request_id)
        except (ValueError, TypeError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await form.close()
            if directory is not None and directory.exists():
                shutil.rmtree(directory)

    @router.get("/jobs/{job_id}")
    async def job(request: Request, job_id: str):
        try:
            return await asyncio.to_thread(manager.state, job_id, _owner(request))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/jobs/{job_id}/stop")
    async def stop(request: Request, job_id: str):
        try:
            return await asyncio.to_thread(manager.stop, job_id, _owner(request))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/jobs/{job_id}/image")
    async def image(request: Request, job_id: str):
        try:
            path = await asyncio.to_thread(manager.image, job_id, _owner(request))
            return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router
