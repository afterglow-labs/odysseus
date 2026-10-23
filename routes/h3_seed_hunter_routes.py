"""Explicit, idempotent preview selection; viewing a preview never enqueues."""
import asyncio
import json
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from src.video_submission import submission, launch_job, complete_operation, UUID, UploadSpaceError
from src import h3_seed_hunter as hunter


def add_seed_hunter_routes(router, manager, owner_callback):
    @router.post("/auto-refine")
    async def auto_refine(request: Request):
        from src.video_auto_refine import apply_saved
        owner = owner_callback(request)
        try:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 65536:
                    raise HTTPException(413, "Automatic refinement request exceeds 64 KiB")
            return await complete_operation(apply_saved, manager, owner, json.loads(data))
        except FileNotFoundError as exc:
            raise HTTPException(404, "Video job not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/jobs/{job_id}/seed-hunter")
    async def detail(job_id: str, request: Request):
        try:
            return await asyncio.to_thread(hunter.detail, manager, job_id, owner_callback(request))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/jobs/{job_id}/seed-hunter/candidates/{index}/video")
    async def video(job_id: str, index: int, request: Request):
        try:
            manager._state(job_id, owner_callback(request))
            path = hunter.artifact(manager.directory(job_id), index, "video")
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Saved preview not found") from exc
        return FileResponse(path, media_type="video/mp4", content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/jobs/{job_id}/seed-hunter/finalize", status_code=201)
    async def finalize(job_id: str, request: Request):
        owner = owner_callback(request)
        try:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 65536:
                    raise HTTPException(413, "Refinement settings exceed 64 KiB")
            body = json.loads(data)
            key = body.get("request_id") if isinstance(body, dict) else None
            if not isinstance(key, str) or not UUID.fullmatch(key):
                raise ValueError("A request_id UUID is required for safe refinement retries")
            manager.directory(job_id)
            async with submission(manager, owner, "h3-refine-" + job_id, uuid.UUID(key).hex) as pending:
                existing = await asyncio.to_thread(pending.existing)
                if existing is not None:
                    return existing
                manager._state(job_id, owner)
                directory = pending.stage()
                config, inputs = await complete_operation(hunter.prepare_final, manager, job_id, owner,
                                                         body, directory, pending.key)
                return await launch_job(manager, directory, owner, config, inputs)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Seed Hunter job not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc

    @router.get("/jobs/{job_id}/seed-hunter/candidates/{index}/audio")
    async def audio(job_id: str, index: int, request: Request):
        try:
            manager._state(job_id, owner_callback(request))
            path = hunter.artifact(manager.directory(job_id), index, "audio")
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Saved dialogue audio not found") from exc
        return FileResponse(path, media_type="audio/flac", filename=f"dialogue-{index}.flac",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
